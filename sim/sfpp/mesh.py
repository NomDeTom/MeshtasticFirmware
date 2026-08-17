"""The radio underneath the sketch: nodes, links, airtime, collisions, managed flood.

The three-store simulator gave each server an independent chance of hearing each message and let
reconciliation happen by function call. That is the right model for asking whether the checksum ever
misses a misdecode, and the wrong one for every question about cost or placement - an advert is a
packet, it contends for the channel, it collides, and it is relayed by nodes that gain nothing from
it. Two servers behind the same relay also miss the same things, which independent loss cannot say.

So this is a transport. The physics is Meshtasticator's - estimate_path_loss() for who hears whom,
airtime() for how long a packet holds the channel, its empirical SNR-to-PER curve for marginal links
- wrapped in an event loop that implements the firmware's own rules: CAD before transmit, SNR-
weighted rebroadcast delay, duplicate suppression, and cancelling a pending rebroadcast on hearing
someone else do it first.

Everything the SR protocol sends goes through here and is charged for.
"""

import heapq
import math
import os
import random
import sys

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "meshtasticator"
    ),
)

BROADCAST = 0xFFFFFFFF

# Roles, with the firmware's rebroadcast semantics. ROUTER wins the contention window by using the
# floor of it; CLIENT_MUTE never rebroadcasts at all.
CLIENT = "CLIENT"
ROUTER = "ROUTER"
CLIENT_MUTE = "CLIENT_MUTE"

# From RadioInterface: the contention window is sized from SNR so that distant nodes - the ones
# whose rebroadcast actually extends coverage - transmit first.
CW_MIN, CW_MAX = 2, 8
SNR_MIN_DB, SNR_MAX_DB = -20.0, 15.0

# Same-SF LoRa capture: a packet survives an overlap if it is this much stronger than the
# interferer, or loses if the interferer locked the preamble first and is not this much weaker.
CAPTURE_DB = 6.0

# Longest a packet can hold the channel at the slowest preset, so a scan back this far cannot miss
# an overlap. LONG_SLOW at a full payload is about 6 s; the margin is deliberate.
MAX_AIRTIME_MS = 20000.0

# The firmware's TX queue is finite and CAD does not retry forever. Overflow is a real drop and is
# counted as one; it is the honest way for congestion to show up.
QUEUE_DEPTH = 16
MAX_BACKOFFS = 400


class Packet:
    """One transmission's worth of payload, tracked from origin to wherever the flood dies."""

    __slots__ = (
        "id",
        "origin",
        "portnum",
        "length",
        "hop_limit",
        "hop_start",
        "kind",
        "payload",
        "destination",
    )

    def __init__(
        self,
        packet_id,
        origin,
        portnum,
        length,
        hop_limit=3,
        kind=None,
        payload=None,
        destination=BROADCAST,
    ):
        self.id = packet_id
        self.origin = origin
        self.portnum = portnum
        self.length = length
        self.hop_limit = hop_limit
        self.hop_start = hop_limit
        self.kind = kind
        self.payload = payload
        self.destination = destination

    def hops_taken(self):
        return self.hop_start - self.hop_limit


class Node:
    __slots__ = (
        "index",
        "x",
        "y",
        "role",
        "is_server",
        "seen",
        "pending",
        "app",
        "busy_until",
        "queue_depth",
    )

    def __init__(self, index, x, y, role=CLIENT):
        self.index = index
        self.x = x
        self.y = y
        self.role = role
        self.is_server = False
        self.seen = {}  # packet id -> time first heard, for duplicate suppression
        self.pending = (
            {}
        )  # packet id -> cancellation record, so a rebroadcast can be dropped
        self.app = None  # whatever the campaign attaches; the mesh never inspects it
        self.busy_until = 0.0  # a radio transmits one packet at a time
        self.queue_depth = 0

    def position(self):
        return (self.x, self.y)


class Transmission:
    """A packet occupying the channel. Reception is decided when it ends."""

    __slots__ = ("packet", "tx_node", "start", "end", "sender_role")

    def __init__(self, packet, tx_node, start, end, sender_role):
        self.packet = packet
        self.tx_node = tx_node
        self.start = start
        self.end = end
        self.sender_role = sender_role


def place_nodes(count, area, rng, min_dist=300.0):
    """Poisson-disc-ish placement: uniform, rejecting anything too close to an existing node.

    Nodes stacked on top of each other would make the mesh look better connected than any real
    deployment, and the minimum spacing is what stops that.
    """
    points = []
    attempts = 0
    while len(points) < count and attempts < count * 4000:
        attempts += 1
        p = (rng.uniform(0, area), rng.uniform(0, area))
        if all(math.dist(p, q) >= min_dist for q in points):
            points.append(p)
    if len(points) < count:
        raise RuntimeError(f"could not place {count} nodes at {min_dist} m in {area} m")
    return points


def make_config(preset="LONG_FAST", model=5, phy_loss=True):
    from lib.config import Config

    conf = Config()
    conf.MODEM_PRESET = preset
    conf.MODEL = model
    conf.PHY_LOSS_MODEL_ENABLED = phy_loss
    # FREQ is derived from the preset's bandwidth at construction, so changing the preset afterwards
    # leaves it stale. phy.py binds a module-level config at import, which must be this same object.
    conf.FREQ = conf.REGION["freq_start"] + conf.current_preset["bw"] * conf.CHANNEL_NUM
    import lib.config
    import lib.phy as phy

    lib.config.CONFIG = conf
    phy.conf = conf
    return conf


class Mesh:
    """Event-driven flood over a fixed set of nodes.

    Time is milliseconds. The queue holds (time, sequence, callable) so ties break deterministically
    on insertion order rather than on dict iteration.
    """

    def __init__(self, conf, nodes, rng, hop_limit=3, area=8000.0, extra_loss=0.0):
        self.conf = conf
        self.nodes = nodes
        self.rng = rng
        self.hop_limit = hop_limit
        self.area = area
        # A flat loss floor on every reception, on top of the physics. It stands in for the things
        # the model does not carry - interference from outside the mesh, fading, a receiver busy
        # elsewhere - and is the knob the capacity-against-loss sweep turns.
        self.extra_loss = extra_loss
        self.now = 0.0
        self._queue = []
        self._seq = 0
        self._next_packet_id = 1
        self.transmissions = []  # chronological; pruned as the window moves
        self.stats = {
            "transmissions": 0,
            "airtime_ms": 0.0,
            "deferrals": 0,
            "queue_drops": 0,
            "rebroadcasts": 0,
            "rebroadcasts_cancelled": 0,
            "receptions": 0,
            "lost_to_collision": 0,
            "lost_to_phy": 0,
            "bytes_on_air": 0,
        }
        self.airtime_by_kind = {}
        self.on_receive = (
            None  # callback(node, packet, rssi, snr) for the campaign's app layer
        )
        self._build_links()

    # ---- link layer -------------------------------------------------------------------

    def _build_links(self):
        """RSSI for every ordered pair, once. 60 nodes is 3540 path-loss calls; it is not the cost."""
        import lib.phy as phy

        conf = self.conf
        n = len(self.nodes)
        self.rssi = [[-999.0] * n for _ in range(n)]
        self.neighbours = [[] for _ in range(n)]
        sensitivity = conf.current_preset["sensitivity"]

        for i in range(n):
            for j in range(i + 1, n):
                d = max(
                    1.0, math.dist(self.nodes[i].position(), self.nodes[j].position())
                )
                loss = phy.estimate_path_loss(conf, d, conf.FREQ)
                base = conf.PTX + 2 * conf.GL - loss
                # Real links are not reciprocal - antennas, height, local clutter. One draw per
                # pair, applied with opposite sign, so the asymmetry is a property of the link.
                skew = (
                    self.rng.gauss(
                        conf.MODEL_ASYMMETRIC_LINKS_MEAN,
                        conf.MODEL_ASYMMETRIC_LINKS_STDDEV,
                    )
                    if conf.MODEL_ASYMMETRIC_LINKS
                    else 0.0
                )
                self.rssi[i][j] = base + skew
                self.rssi[j][i] = base - skew

        for i in range(n):
            for j in range(n):
                if i != j and self.rssi[i][j] >= sensitivity:
                    self.neighbours[i].append(j)

    def link_stats(self):
        degrees = [len(v) for v in self.neighbours]
        return {
            "links": sum(degrees) // 2,
            "mean_degree": sum(degrees) / len(degrees),
            "isolated": sum(1 for d in degrees if d == 0),
        }

    def hops_from(self, sources):
        """BFS over the link graph. Used for topology placement and for reporting depth."""
        depth = {s: 0 for s in sources}
        frontier = list(sources)
        while frontier:
            nxt = []
            for node in frontier:
                for peer in self.neighbours[node]:
                    if peer not in depth:
                        depth[peer] = depth[node] + 1
                        nxt.append(peer)
            frontier = nxt
        return depth

    # ---- event loop -------------------------------------------------------------------

    def at(self, time, fn):
        self._seq += 1
        token = [False]  # mutable so cancel() can reach it after scheduling
        heapq.heappush(self._queue, (time, self._seq, fn, token))
        return token

    @staticmethod
    def cancel(token):
        if token is not None:
            token[0] = True

    def run(self, until):
        while self._queue and self._queue[0][0] <= until:
            time, _, fn, token = heapq.heappop(self._queue)
            self.now = time
            if not token[0]:
                fn()
        self.now = until

    def new_packet_id(self):
        # Meshtastic IDs are random, not sequential - that is exactly why a receiver cannot detect a
        # gap, which is the reason set reconciliation exists at all.
        self._next_packet_id += 1
        return self._next_packet_id

    # ---- transmission -----------------------------------------------------------------

    def airtime_ms(self, length):
        import lib.phy as phy

        p = self.conf.current_preset
        return phy.airtime(self.conf, p["sf"], p["cr"], length, p["bw"])

    def slot_time_ms(self):
        p = self.conf.current_preset
        symbol_ms = (2.0 ** p["sf"]) / (p["bw"] / 1000.0)
        return max(2.25, 2 + 0.5) * symbol_ms + 7.6

    def _cw_delay(self, node, snr):
        """Firmware's SNR-weighted contention window, in ms."""
        if self.nodes[node].role == ROUTER:
            cw = CW_MIN
        else:
            span = SNR_MAX_DB - SNR_MIN_DB
            frac = min(1.0, max(0.0, (snr - SNR_MIN_DB) / span))
            cw = int(CW_MIN + frac * (CW_MAX - CW_MIN))
        return self.rng.uniform(0, 2**cw) * self.slot_time_ms()

    def _recent(self, since):
        """Transmissions that could still be on air, newest first. Starts are monotonic, ends are
        not, so the scan is bounded by start time rather than stopping at the first one that ended.
        """
        for t in reversed(self.transmissions):
            if t.start < since:
                return
            yield t

    def _channel_busy(self, node):
        """CAD: is anything audible at this node on the air right now?"""
        threshold = self.conf.current_preset["sensitivity"] - 3
        for t in self._recent(self.now - MAX_AIRTIME_MS):
            if t.end <= self.now:
                continue
            if t.tx_node != node and self.rssi[t.tx_node][node] >= threshold:
                return True
        return False

    def send(self, node, packet, attempts=0, token=None):
        """Take the channel, or wait for it. The radio holds the packet; it does not discard it.

        Firmware's TX queue waits on CAD rather than dropping, so a congested mesh shows up as
        latency and as a full queue, not as packets that quietly evaporate. The one drop is queue
        overflow, which is what the firmware does too.
        """
        radio = self.nodes[node]
        if self._channel_busy(node) or radio.busy_until > self.now:
            if attempts == 0:
                if radio.queue_depth >= QUEUE_DEPTH:
                    self.stats["queue_drops"] += 1
                    return
                radio.queue_depth += 1
            if attempts >= MAX_BACKOFFS:
                radio.queue_depth -= 1
                self.stats["queue_drops"] += 1
                return
            self.stats["deferrals"] += 1
            retry = self.at(
                self.now + self.slot_time_ms() * self.rng.uniform(1, 4),
                lambda: self.send(node, packet, attempts + 1, token),
            )
            if token is not None:
                token["event"] = retry
            return
        if attempts > 0:
            radio.queue_depth -= 1
        if token is not None:
            token["sent"] = True

        duration = self.airtime_ms(packet.length)
        radio.busy_until = self.now + duration
        tx = Transmission(
            packet, node, self.now, self.now + duration, self.nodes[node].role
        )
        self.transmissions.append(tx)
        self.stats["transmissions"] += 1
        self.stats["airtime_ms"] += duration
        self.stats["bytes_on_air"] += packet.length
        key = packet.kind or packet.portnum
        self.airtime_by_kind[key] = self.airtime_by_kind.get(key, 0.0) + duration
        self.at(tx.end, lambda: self._deliver(tx))

    def _overlapping(self, tx):
        """Every other transmission sharing air with this one. All of them started before it ended."""
        return [
            o
            for o in self._recent(tx.start - MAX_AIRTIME_MS)
            if o is not tx and o.start < tx.end and o.end > tx.start
        ]

    def _deliver(self, tx):
        """Decide, at end of transmission, who actually received it."""
        packet = tx.packet
        sensitivity = self.conf.current_preset["sensitivity"]
        interferers = self._overlapping(tx)
        self._prune()

        for rx in self.neighbours[tx.tx_node]:
            rssi = self.rssi[tx.tx_node][rx]
            if rssi < sensitivity:
                continue
            if not self._survives_capture(tx, rx, rssi, interferers, sensitivity):
                self.stats["lost_to_collision"] += 1
                continue
            if self._lost_to_phy(rssi, packet.length) or (
                self.extra_loss and self.rng.random() < self.extra_loss
            ):
                self.stats["lost_to_phy"] += 1
                continue
            self.stats["receptions"] += 1
            self._receive(rx, packet, rssi)

    def _survives_capture(self, tx, rx, rssi, interferers, sensitivity):
        audible = [
            o
            for o in interferers
            if o.tx_node != rx and self.rssi[o.tx_node][rx] >= sensitivity - 3
        ]
        if not audible:
            return True
        # Whichever preamble arrived first holds the receiver. A later packet needs the capture
        # margin to break that lock; an earlier one keeps it unless something much louder arrives.
        earliest = min(audible, key=lambda o: o.start)
        if earliest.start < tx.start:
            return rssi >= self.rssi[earliest.tx_node][rx] + CAPTURE_DB
        return all(rssi >= self.rssi[o.tx_node][rx] + CAPTURE_DB for o in audible)

    def _lost_to_phy(self, rssi, length):
        import lib.radio_loss as radio_loss

        if not self.conf.PHY_LOSS_MODEL_ENABLED:
            return False
        return radio_loss.payload_is_lost(
            self.conf, rssi, self.conf.current_preset["cr"], length, self.rng.random()
        )

    def _receive(self, rx, packet, rssi):
        node = self.nodes[rx]
        snr = rssi - self.conf.NOISE_LEVEL
        duplicate = packet.id in node.seen

        if duplicate:
            # Someone else got there first. Firmware drops its own queued rebroadcast rather than
            # adding a second copy, and that cancellation is a large part of why flooding is
            # survivable at all - without it every node in earshot repeats every packet. A packet
            # already keying up cannot be recalled, so only a still-waiting one is cancelled.
            record = node.pending.pop(packet.id, None)
            if record is not None and not record["sent"]:
                self.cancel(record["event"])
                self.stats["rebroadcasts_cancelled"] += 1
            return

        node.seen[packet.id] = self.now
        if self.on_receive is not None:
            self.on_receive(node, packet, rssi, snr)

        if packet.hop_limit > 0 and node.role != CLIENT_MUTE and rx != packet.origin:
            relayed = Packet(
                packet.id,
                packet.origin,
                packet.portnum,
                packet.length,
                hop_limit=packet.hop_limit - 1,
                kind=packet.kind,
                payload=packet.payload,
                destination=packet.destination,
            )
            relayed.hop_start = packet.hop_start
            delay = self._cw_delay(rx, snr)
            record = {"sent": False, "event": None}

            def do_relay(rx=rx, relayed=relayed, record=record):
                self.stats["rebroadcasts"] += 1
                self.send(rx, relayed, token=record)

            record["event"] = self.at(self.now + delay, do_relay)
            node.pending[packet.id] = record

    def _prune(self):
        """Keep the transmission list bounded; nothing this old can overlap anything new."""
        if len(self.transmissions) < 4000:
            return
        cutoff = self.now - MAX_AIRTIME_MS
        self.transmissions = [t for t in self.transmissions if t.start > cutoff]

    def originate(self, node, portnum, length, kind=None, payload=None, hop_limit=None):
        """Inject a packet from a node's application layer, as if it had composed it."""
        packet = Packet(
            self.new_packet_id(),
            node,
            portnum,
            length,
            hop_limit=self.hop_limit if hop_limit is None else hop_limit,
            kind=kind,
            payload=payload,
        )
        self.nodes[node].seen[packet.id] = self.now
        self.send(node, packet)
        return packet


def build(
    conf, node_count, area, rng, hop_limit=3, min_dist=300.0, router_fraction=0.0
):
    """A mesh with positions drawn from `rng` and a share of the nodes promoted to ROUTER.

    Routers are chosen by degree rather than at random: a deployment puts the repeater on the hill,
    and choosing them randomly would understate how much a flood depends on a few well-sited nodes.
    """
    points = place_nodes(node_count, area, rng, min_dist)
    nodes = [Node(i, x, y) for i, (x, y) in enumerate(points)]
    mesh = Mesh(conf, nodes, rng, hop_limit=hop_limit, area=area, extra_loss=extra_loss)

    if router_fraction > 0:
        want = max(1, int(round(node_count * router_fraction)))
        by_degree = sorted(range(node_count), key=lambda i: -len(mesh.neighbours[i]))
        for i in by_degree[:want]:
            nodes[i].role = ROUTER
    return mesh
