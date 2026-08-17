"""SF++ set reconciliation over a real mesh: server placement, the protocol, and what it costs.

Every message the protocol sends is a packet on the transport in mesh.py. An advert contends for
the channel, is relayed by nodes that gain nothing from it, and is lost when it collides. That is
the point: the three-store simulator settled whether the checksum ever misses a misdecode, and this
one asks what the thing costs and where the servers should go.

Three arms are swept independently:

  trigger   when an advert is worth sending    bucket-close / fixed interval / AIMD
  resolve   how a difference is resolved       sketch-as-request / explicit enumeration / hybrid
  place     where the servers are              spread / routers / every other router / beside a
                                               router / a fixed number of hops apart

Usage, from sim/:
    python3 -m sfpp.campaign --hours 6 --place spread --servers 3 --capacity 32
    python3 -m sfpp.campaign --baseline            # no SF++ at all: what does the mesh alone lose?
"""

import argparse
import json
import math
import os
import random
import shutil
import statistics
import sys
import tempfile
import time

from . import mesh as M
from . import traffic as T
from .sketchindex import BUCKET_OBJECTS, bucket_of, checksum_contribution, short_id
from .store import SfppStore

# Wire sizes from the frozen format. See sfpp-sr-wire-format.md.
SR_ENVELOPE = 18
SR_CHECKSUM = 9
SR_SIGNATURE = 66
OBJECT_OVERHEAD = 14
# Two fields on the outside of a replayed object, clear of the original encryption wrapper, so a node
# that merely overhears the replay can file it in the right place in its own history rather than
# showing it as having arrived now. `heard_ago` is seconds since the archive received it; `replayed`
# marks it as a replay so a client can present it as such instead of as fresh traffic.
# One field, outside the encryption wrapper: heard_ago, in 64-second ticks, 2 bytes.
#
# No separate "replayed" flag is needed, because the field's *presence* is the flag. Fresh traffic
# carries no heard_ago; a replay does. In protobuf terms that is an optional field, so absence costs
# nothing and cannot be confused with a value of zero-seconds-ago.
#
# 64-second ticks because fine resolution is worthless here - the measured spread between two
# archives' accounts of the same message is single-digit seconds - and a full 16 bits of ticks buys
# 48 days of range, comfortably past any archive retention window.
REPLAY_TICK_S = 64
REPLAY_HEADER = 2
REPLAY_MAX_TICKS = (1 << 16) - 1
MAX_PAYLOAD = 233
STORE_FORWARD_PLUSPLUS_APP = 35

# The traffic mix. NodeInfo is every three hours in the firmware's defaults, not hourly.
MIX = (
    T.Class("position", T.POSITION_APP, 4.0, 20, 4, 1.0),
    T.Class("telemetry", T.TELEMETRY_APP, 2.0, 24, 6, 1.0),
    T.Class("nodeinfo", T.NODEINFO_APP, 0.33, 40, 8, 1.0),
    T.Class("text", T.TEXT_MESSAGE_APP, 1.2, 53, 20, 0.4, archived=True),
)


def sketch_bytes(capacity, width_bits):
    return int(math.ceil(capacity * width_bits / 8.0))


def truncated_short_id(message_hash, width_bits):
    """The sketch member at a chosen short-ID width.

    PinSketch here is GF(2^32) because it is a transcription of the firmware's, so the arithmetic
    cannot be re-fielded without breaking the oracle. Narrowing is modelled by masking the ID to `b`
    bits before it enters the sketch, which reproduces exactly what `b` controls - the collision
    rate - while airtime is charged at the real c x b/8. Widths above 32 are charged their real
    airtime and modelled as collision-free, which they effectively are.
    """
    sid = short_id(message_hash)
    if width_bits >= 32:
        return sid
    mask = (1 << width_bits) - 1
    narrowed = sid & mask
    return narrowed or 1  # zero is not a representable member


class Placement:
    """Where the SF++ servers go. Each strategy answers a different version of the question."""

    @staticmethod
    def spread(mesh, count, rng, hops=None):
        """Farthest-point: take the node furthest from everything already chosen."""
        n = len(mesh.nodes)
        chosen = [max(range(n), key=lambda i: mesh.nodes[i].x + mesh.nodes[i].y)]
        while len(chosen) < count:
            chosen.append(
                max(
                    range(n),
                    key=lambda i: min(
                        math.dist(mesh.nodes[i].position(), mesh.nodes[c].position())
                        for c in chosen
                    ),
                )
            )
        return chosen

    @staticmethod
    def routers(mesh, count, rng, hops=None):
        """Every router is a server, best-connected first. `count` caps it."""
        routers = [i for i, node in enumerate(mesh.nodes) if node.role == M.ROUTER]
        routers.sort(key=lambda i: -len(mesh.neighbours[i]))
        return routers[:count] if count else routers

    @staticmethod
    def alternate_routers(mesh, count, rng, hops=None):
        """Every other router, by descending degree, so the servers are not adjacent."""
        routers = [i for i, node in enumerate(mesh.nodes) if node.role == M.ROUTER]
        routers.sort(key=lambda i: -len(mesh.neighbours[i]))
        return routers[::2][:count] if count else routers[::2]

    @staticmethod
    def beside_router(mesh, count, rng, hops=None):
        """A plain client one hop from each router - the 'off to the side of a router' case.

        A server beside a router hears everything the router hears without competing with it for
        the channel, which is the argument for the arrangement. Whether that survives contact with
        the contention window is the thing being measured.
        """
        routers = [i for i, node in enumerate(mesh.nodes) if node.role == M.ROUTER]
        routers.sort(key=lambda i: -len(mesh.neighbours[i]))
        out = []
        for r in routers:
            for peer in sorted(
                mesh.neighbours[r], key=lambda i: -len(mesh.neighbours[i])
            ):
                if mesh.nodes[peer].role != M.ROUTER and peer not in out:
                    out.append(peer)
                    break
            if count and len(out) >= count:
                break
        return out[:count] if count else out

    @staticmethod
    def random_clients(mesh, count, rng, hops=None):
        """Ordinary nodes, chosen at random - the control for every deliberate arrangement."""
        clients = [i for i, node in enumerate(mesh.nodes) if node.role != M.ROUTER]
        return rng.sample(clients, min(count, len(clients)))

    @staticmethod
    def hops_apart(mesh, count, rng, hops=3):
        """Servers whose pairwise separation is as close to `hops` as the graph allows."""
        n = len(mesh.nodes)
        start = max(range(n), key=lambda i: len(mesh.neighbours[i]))
        chosen = [start]
        while len(chosen) < count:
            best, best_error = None, None
            for candidate in range(n):
                if candidate in chosen:
                    continue
                depth = mesh.hops_from([candidate])
                separations = [depth.get(c) for c in chosen]
                if any(s is None for s in separations):
                    continue
                error = sum(abs(s - hops) for s in separations)
                if best_error is None or error < best_error:
                    best, best_error = candidate, error
            if best is None:
                break
            chosen.append(best)
        return chosen

    BY_NAME = {
        "spread": spread.__func__,
        "routers": routers.__func__,
        "alternate-routers": alternate_routers.__func__,
        "beside-router": beside_router.__func__,
        "random-clients": random_clients.__func__,
        "hops-apart": hops_apart.__func__,
    }


class Counters:
    FIELDS = (
        "adverts",
        "advert_bytes",
        "item_requests",
        "item_request_bytes",
        "provides",
        "provide_bytes",
        "enum_requests",
        "enum_request_bytes",
        "enum_provides",
        "enum_provide_bytes",
        "exchanges",
        "decode_failures",
        "misdecodes",
        "escalations",
        "checksum_closed",
        "checksum_open",
        "objects_moved",
        "silent_losses",
        "adverts_heard",
        "adverts_lost",
        "window_checksum_closed",
        "bystander_pickups",
        "replays_backfiled",
        "adverts_deferred",
    )

    def __init__(self):
        for f in self.FIELDS:
            setattr(self, f, 0)

    def sr_bytes(self):
        return (
            self.advert_bytes
            + self.item_request_bytes
            + self.provide_bytes
            + self.enum_request_bytes
            + self.enum_provide_bytes
        )

    def as_dict(self):
        d = {f: getattr(self, f) for f in self.FIELDS}
        d["sr_bytes"] = self.sr_bytes()
        return d


class Server:
    """One SF++ node: a store, plus the reconciliation state the protocol needs.

    Bucket membership is deliberately per-server. The firmware assigns a chain counter as
    `chain_end.counter + 1` when it ingests a message that arrived without an official one
    (StoreForwardPlusPlus.cpp:1366), so two servers that each hear the same broadcast off the air
    number it differently. `SketchIndex.h` claims a count boundary is "one both sides derive from the
    data itself"; it is not - it is derived from local arrival order, and that is exactly the thing
    the bucket-mode arm exists to measure.
    """

    def __init__(self, index, store, opts):
        self.index = index
        self.store = store
        self.opts = opts
        self.held = {}  # message_hash -> counter as this server numbered it
        self.bucket = {}  # message_hash -> bucket as this server assigns it
        self.next_counter = 0
        self.interval_ms = opts.advert_interval_s * 1000.0
        self.next_bucket = 0
        self.matched = set()
        self.poisoned = set()
        self.sealed = set()  # buckets this server considers closed
        # (rx_time_ms, bucket) for objects heard directly, in time order. A replay carrying
        # `heard_ago` can be binary-searched into this to land in the bucket it would have been in
        # had it arrived on time, rather than in whatever bucket is current now.
        self.timeline = []
        # message_hash -> (first_heard_ms, [claimed original ms from each replay]). Holding both the
        # directly-heard copy and every replay's claim is what makes drift measurable and what would
        # catch a peer lying about heard_ago.
        self.provenance = {}

    def members(self, bucket):
        return {h for h, b in self.bucket.items() if b == bucket}

    def window(self, size):
        """The N objects this server ingested most recently, by its own numbering.

        A sliding window needs no agreement with anyone. Two servers' windows overlap because they
        heard mostly the same recent traffic, not because they negotiated a boundary, which is the
        whole point: the XOR of two sketches is the symmetric difference of whatever two sets they
        were built over, agreed or not.
        """
        recent = sorted(self.held.items(), key=lambda kv: -kv[1])[:size]
        return {h for h, _ in recent}

    def window_summary(self, size, capacity, width):
        from .sketchindex import BucketSummary

        s = BucketSummary(capacity)
        for message_hash in self.window(size):
            s.add(
                truncated_short_id(message_hash, width),
                checksum_contribution(message_hash),
            )
        return s if s.count > 0 else None

    def note_direct(self, message_hash, when_ms, bucket):
        self.timeline.append((when_ms, bucket))
        self.provenance.setdefault(message_hash, [when_ms, []])

    def note_replay(self, message_hash, claimed_ms):
        record = self.provenance.setdefault(message_hash, [None, []])
        record[1].append(claimed_ms)

    def bucket_at(self, when_ms):
        """The bucket this server was filling at that moment, or None if it has no record there."""
        import bisect

        if not self.timeline:
            return None
        times = [t for t, _ in self.timeline]
        i = bisect.bisect_left(times, when_ms)
        if i >= len(self.timeline):
            return self.timeline[-1][1]
        return self.timeline[i][1]

    def holds(self, message_hash):
        """Membership in the whole store, not just the window.

        Load-bearing for window mode: a short ID in the decoded difference may be something the peer
        holds and has simply aged out of its window. Checking the window would request it back.
        """
        return message_hash in self.held

    def buckets(self):
        return set(self.bucket.values())

    def summary(self, root_hash, bucket, capacity, width):
        """This server's summary of its own bucket, whatever that bucket happens to contain."""
        from .sketchindex import BucketSummary

        s = BucketSummary(capacity)
        for message_hash in self.members(bucket):
            s.add(
                truncated_short_id(message_hash, width),
                checksum_contribution(message_hash),
            )
        return s if s.count > 0 else None


class Campaign:
    def __init__(self, opts, seed):
        self.opts = opts
        self.seed = seed
        self.rng = random.Random(seed)
        self.conf = M.make_config(preset=opts.preset, phy_loss=not opts.no_phy_loss)
        # 150 nodes in the same 8 x 8 km as 60 is two and a half times the density, so a size sweep
        # that holds area fixed measures density and calls it size. Scaling the side by sqrt(n/60)
        # keeps nodes per square kilometre constant and lets the two be separated.
        self.area = (
            opts.area * math.sqrt(opts.nodes / 60.0) if opts.scale_area else opts.area
        )
        self.mesh = M.build(
            self.conf,
            opts.nodes,
            self.area,
            self.rng,
            hop_limit=opts.hop_limit,
            router_fraction=opts.router_fraction,
            extra_loss=opts.extra_loss,
            burst_loss=opts.burst_loss,
            burst_ms=opts.burst_ms,
            hop_spread=opts.hop_spread,
        )
        self.root_hash = bytes(range(16))
        self.generator = T.Generator(
            self.mesh,
            self.rng,
            self.root_hash,
            mix=MIX,
            congestion_scaling=not opts.no_congestion_scaling,
            diurnal=opts.diurnal,
            start_hour=opts.start_hour,
            position_throttle=opts.position_throttle,
            telemetry_throttle=opts.telemetry_throttle,
        )
        self.counters = Counters()
        self.duration_ms = opts.hours * 3600_000.0

        self.catch_up = None
        if opts.catch_up_hours:
            start, end = opts.catch_up_hours.split("-")
            self.catch_up = (float(start), float(end))
        self.counter_of = {}  # message_hash -> canonical chain counter
        self._counted = 0
        self.heard_text = {i: set() for i in range(opts.nodes)}
        self.servers = {}
        self.db_dir = tempfile.mkdtemp(prefix="sfpp-campaign-")
        self.bucket_closed_at = {}
        self.width = opts.short_id_bits

        # Intermediate nodes wired for telemetry. They run no archive and change no behaviour; they
        # only record what an ordinary node in the middle of the mesh actually ends up with, split by
        # how it got there. Without this the bystander benefit of a broadcast replay is invisible.
        self.observers = {}
        if not opts.baseline:
            self._place_servers()
            self._place_observers()
        self.mesh.on_receive = self._on_receive

    # ---- setup ------------------------------------------------------------------------

    def _place_servers(self):
        strategy = Placement.BY_NAME[self.opts.place]
        indexes = strategy(self.mesh, self.opts.servers, self.rng, self.opts.hops_apart)
        for i in indexes:
            self.mesh.nodes[i].is_server = True
            self.servers[i] = Server(
                i, SfppStore(os.path.join(self.db_dir, f"s{i}.db"), i), self.opts
            )

    def _place_observers(self):
        """Pick ordinary nodes spread through the mesh, preferring the middle over the fringe."""
        candidates = [
            i
            for i in range(self.opts.nodes)
            if i not in self.servers and self.mesh.neighbours[i]
        ]
        if not candidates or self.opts.observers <= 0:
            return
        depth = self.mesh.hops_from(sorted(self.servers)) if self.servers else {}
        candidates.sort(key=lambda i: (depth.get(i, 99), -len(self.mesh.neighbours[i])))
        step = max(1, len(candidates) // self.opts.observers)
        for i in candidates[::step][: self.opts.observers]:
            self.observers[i] = {
                "direct": set(),
                "overheard": set(),
                "placement_error_s": [],
                "hops_to_server": depth.get(i, -1),
                "hop_limit": self.mesh.hop_limit_for(i),
                "degree": len(self.mesh.neighbours[i]),
            }

    def server_separation(self):
        """Pairwise hop distances between servers - the topology arm's independent variable."""
        out = []
        keys = sorted(self.servers)
        for a in keys:
            depth = self.mesh.hops_from([a])
            for b in keys:
                if b > a:
                    out.append(depth.get(b, -1))
        return out

    # ---- ingest -----------------------------------------------------------------------

    def _on_receive(self, node, packet, rssi, snr):
        if packet.kind == "text":
            self._on_text(node, packet)
        elif packet.kind == "sr:item_provide" and packet.payload is not None:
            # A replayed object is useful to whoever hears it, not only to whoever asked. A server
            # files it in its store; any other node records it for its own history. Both paths run -
            # intercepting here and returning was a bug that stopped servers ingesting broadcast
            # replays at all, and it cost a third of their holdings.
            if node.index in self.servers:
                self._on_sr(node, packet)
            else:
                self._on_overheard_replay(node, packet)
        elif packet.kind and packet.kind.startswith("sr:"):
            self._on_sr(node, packet)

    def _global_counter(self, message_hash):
        """Origination order across the whole mesh. A FICTION, kept only as an upper bound.

        No canonical counter exists. The firmware comment at StoreForwardPlusPlus.cpp:1364 reads "if
        we get an official counter, use it. Otherwise, just increment" - and there is no official
        counter to get. Every counter is a local increment off the local chain tip, so this mode
        describes a mesh nobody can build. It is here to bound what bucket agreement would be worth
        if it existed, and for no other purpose.
        """
        counter = self.counter_of.get(message_hash)
        if counter is None:
            order = self.generator.text_order
            while self._counted < len(order):
                self._counted += 1
                self.counter_of.setdefault(order[self._counted - 1], self._counted)
            counter = self.counter_of.get(message_hash)
        return counter

    def _assign(self, server, message_hash):
        """Give this object a counter and a bucket from this server's point of view.

        Returns (counter, bucket, sealed_bucket_or_None).
        """
        mode = self.opts.bucket_mode
        if mode == "global":
            counter = self._global_counter(message_hash)
            if counter is None:
                return None, None, None
            bucket = bucket_of(counter)
            # Sealing follows the shared counter: an object past the boundary closes the one below.
            return counter, bucket, bucket - 1 if bucket > 0 else None

        if mode == "local":
            # What the firmware does, and the only mode that describes a real mesh: chain_end.counter
            # + 1, every time, because no official counter is ever supplied. Both the bucket a
            # message lands in and the moment a bucket fills are therefore per-server and effectively
            # random - each server hears a different subset in a different order.
            server.next_counter += 1
            counter = server.next_counter
            bucket = bucket_of(counter)
            sealed = bucket - 1 if bucket > 0 else None
            return counter, bucket, sealed

        if mode == "window":
            # No boundaries at all. The counter still numbers arrival order so the window knows what
            # "most recent" means, and an advert is due every time the window has fully turned over.
            server.next_counter += 1
            due = server.next_counter % max(1, self.opts.window_size) == 0
            return server.next_counter, 0, 0 if due else None

        # Time buckets: quantise the receive clock. Both servers heard the packet within a second or
        # two, so any window wider than that agrees except for objects near a boundary - which is a
        # bounded disagreement rather than a total one.
        window = self.opts.time_bucket_s * 1000.0
        bucket = int(self.mesh.now // window)
        server.next_counter += 1
        return server.next_counter, bucket, bucket - 1 if bucket > 0 else None

    def _on_text(self, node, packet):
        message_hash = packet.payload
        self.heard_text[node.index].add(message_hash)
        watch = self.observers.get(node.index)
        if watch is not None:
            watch["direct"].add(message_hash)
        server = self.servers.get(node.index)
        if server is None:
            return
        counter, bucket, sealed = self._assign(server, message_hash)
        if counter is None:
            return
        obj = self.generator.objects[message_hash]
        if server.store.insert(obj, counter):
            server.held[message_hash] = counter
            server.bucket[message_hash] = bucket
            server.note_direct(message_hash, self.mesh.now, bucket)
            if sealed is not None and self.opts.trigger in (
                "bucket",
                "bucket+interval",
            ):
                if self.opts.bucket_mode == "window":
                    # A window has no boundary to seal once; it turns over repeatedly, so this
                    # schedules a fresh advert each time rather than deduplicating on bucket index.
                    jitter = self.rng.uniform(0, self.opts.advert_jitter_s * 1000.0)
                    self.mesh.at(
                        self.mesh.now + jitter,
                        lambda s=server: self._advertise_window(s),
                    )
                else:
                    self._maybe_advertise_on_close(server, sealed)

    def _on_overheard_replay(self, node, packet):
        """A non-server node overhearing a replayed object gets to keep it.

        This only works because the replay header sits outside the encryption wrapper: without
        `heard_ago` the node could store the message but not place it, and without `replayed` it would
        present an hour-old message as having just arrived.
        """
        message_hash = packet.payload.get("hash")
        if message_hash is None:
            return
        watch = self.observers.get(node.index)
        if message_hash not in self.heard_text[node.index]:
            self.heard_text[node.index].add(message_hash)
            self.counters.bystander_pickups += 1
            if watch is not None:
                watch["overheard"].add(message_hash)
                # How far out is the replay header's account of when this was first heard? The
                # claim is the archive's receive time, not the originator's, so some error is
                # expected; this is whether it is small enough to file the message correctly.
                claimed = self.mesh.now - packet.payload.get("heard_ago_s", 0) * 1000.0
                true_ms = self.generator.objects[message_hash].rx_time
                watch["placement_error_s"].append(abs(claimed - true_ms) / 1000.0)

    def _maybe_advertise_on_close(self, server, bucket):
        """A bucket this server considers sealed is worth stating once."""
        if bucket < 0 or bucket in server.sealed:
            return
        server.sealed.add(bucket)
        jitter = self.rng.uniform(0, self.opts.advert_jitter_s * 1000.0)
        self.mesh.at(self.mesh.now + jitter, lambda: self._advertise(server, bucket))

    # ---- the protocol -----------------------------------------------------------------

    def _sr_send(self, src, kind, payload, length, dst=None):
        """Put one SR message on the air. Broadcast floods; addressed traffic takes the DM path."""
        if dst is None:
            self.mesh.originate(
                src, STORE_FORWARD_PLUSPLUS_APP, length, kind=kind, payload=payload
            )
        else:
            self._unicast(src, dst, kind, payload, length)

    def _unicast(self, src, dst, kind, payload, length, attempt=0):
        """Hop-by-hop along the shortest path, the way next-hop routing moves a DM.

        Flooding an addressed reply would charge the whole neighbourhood for a conversation between
        two nodes and would badly overstate what reconciliation costs on a modern firmware. Each hop
        is still a real transmission that contends and can be lost, with a bounded retry.
        """
        path = self._path(src, dst)
        if path is None or len(path) < 2:
            return
        self._unicast_hop(path, 0, kind, payload, length, attempt)

    def _unicast_hop(self, path, i, kind, payload, length, attempt):
        if i >= len(path) - 1:
            self._deliver_sr(path[-1], kind, payload)
            return
        a, b = path[i], path[i + 1]
        packet = M.Packet(
            self.mesh.new_packet_id(),
            a,
            STORE_FORWARD_PLUSPLUS_APP,
            length,
            hop_limit=0,  # addressed: the next hop is named, so nobody else repeats it
            kind=kind,
            payload=payload,
            destination=path[-1],
        )
        self.mesh.nodes[a].seen[packet.id] = self.mesh.now
        self.mesh.send(a, packet)
        # Reception on this hop is the transport's own draw, applied here because an addressed
        # packet has one intended receiver rather than everyone in earshot.
        rssi = self.mesh.rssi[a][b]
        lost = self.mesh._lost_to_phy(rssi, length) or (
            self.mesh.extra_loss and self.rng.random() < self.mesh.extra_loss
        )
        delay = self.mesh.airtime_ms(length) + self.mesh.slot_time_ms() * 2

        def onward():
            if lost:
                if attempt < 2:
                    self._unicast_hop(path, i, kind, payload, length, attempt + 1)
                return
            self._unicast_hop(path, i + 1, kind, payload, length, attempt)

        self.mesh.at(self.mesh.now + delay, onward)

    def _path(self, src, dst):
        if not hasattr(self, "_paths"):
            self._paths = {}
        if src not in self._paths:
            previous = {src: None}
            frontier = [src]
            while frontier:
                nxt = []
                for node in frontier:
                    for peer in self.mesh.neighbours[node]:
                        if peer not in previous:
                            previous[peer] = node
                            nxt.append(peer)
                frontier = nxt
            self._paths[src] = previous
        previous = self._paths[src]
        if dst not in previous:
            return None
        path, cursor = [], dst
        while cursor is not None:
            path.append(cursor)
            cursor = previous[cursor]
        return path[::-1]

    def _deliver_sr(self, node_index, kind, payload):
        """An addressed SR message that arrived. Broadcasts land through the mesh's own callback."""
        node = self.mesh.nodes[node_index]
        self._handle_sr(node, kind, payload)

    def _on_sr(self, node, packet):
        self._handle_sr(node, packet.kind, packet.payload)

    def _handle_sr(self, node, kind, payload):
        server = self.servers.get(node.index)
        if server is None or payload is None:
            return
        if (
            payload.get("dst") is not None
            and payload["dst"] != node.index
            and kind != "sr:item_provide"
        ):
            # Everything except a replayed object is a two-party conversation. A replay is not: a
            # server that overhears one addressed to a different peer should keep it, which is most
            # of the argument for broadcasting them at all.
            return
        if payload["src"] == node.index:
            return
        handler = {
            "sr:advert": self._recv_advert,
            "sr:item_request": self._recv_item_request,
            "sr:item_provide": self._recv_item_provide,
            "sr:enum_request": self._recv_enum_request,
            "sr:enum_provide": self._recv_enum_provide,
        }[kind]
        handler(server, payload)

    def _in_catch_up_window(self):
        """Is now inside the configured quiet period?

        The argument for a catch-up window is that reconciliation is delay-tolerant and contention is
        not: an archive that waits for the small hours pays for its airtime when the channel is cheap
        and nobody is waiting on a text message. The cost is latency - a message missed at the evening
        peak is not replicated until the small hours - which is why it is an arm and not a default.
        """
        if not self.catch_up:
            return True
        start, end = self.catch_up
        hour = (self.opts.start_hour + self.mesh.now / 3600_000.0) % 24
        return start <= hour < end if start <= end else (hour >= start or hour < end)

    def _advertise_window(self, server):
        """One sketch over this server's most recent N objects, addressed to nobody in particular."""
        if not self._in_catch_up_window():
            self.counters.adverts_deferred += 1
            return
        size = self.opts.window_size
        summary = server.window_summary(size, self.opts.capacity, self.width)
        if summary is None:
            return
        length = (
            SR_ENVELOPE + SR_CHECKSUM + sketch_bytes(self.opts.capacity, self.width)
        )
        if self.opts.signed:
            length += SR_SIGNATURE
        payload = {
            "src": server.index,
            "dst": None,
            "bucket": 0,
            "window": True,
            "sketch": summary,
            "checksum": summary.checksum,
            "count": summary.count,
            "members": server.window(size),
        }
        if self.opts.advert_transport == "dm":
            for peer in [i for i in self.servers if i != server.index]:
                self.counters.adverts += 1
                self.counters.advert_bytes += length
                self._sr_send(
                    server.index, "sr:advert", dict(payload, dst=peer), length, dst=peer
                )
            return
        self.counters.adverts += 1
        self.counters.advert_bytes += length
        self._sr_send(server.index, "sr:advert", payload, length)

    def _recv_advert_window(self, server, payload):
        """XOR two windows that were never agreed, then split the difference by whole-store holdings."""
        size = self.opts.window_size
        local = server.window_summary(size, self.opts.capacity, self.width)
        self.counters.exchanges += 1
        self.counters.adverts_heard += 1
        if local is None:
            return

        if local.checksum == payload["checksum"]:
            # Both windows are byte-identical. This is the only case the bucket design's safety
            # mechanism can still fire on, and counting how often it happens is the point.
            self.counters.window_checksum_closed += 1
            if server.window(size) != payload["members"]:
                self.counters.silent_losses += 1
            return

        difference = local.difference(payload["sketch"].sketch())
        if difference is None:
            self.counters.decode_failures += 1
            self.counters.escalations += 1
            return

        peer = self.servers[payload["src"]]
        moved = 0
        wanted = []
        for sid in difference:
            mine = [h for h in server.held if truncated_short_id(h, self.width) == sid]
            if mine:
                for message_hash in mine:
                    # Only send what the peer genuinely lacks. Without the whole-store check this
                    # would ship back everything that had merely aged out of the peer's window.
                    if not peer.holds(message_hash):
                        self._send_object(server, payload["src"], message_hash)
                        moved += 1
            else:
                wanted.append(sid)
        if wanted:
            length = SR_ENVELOPE + 4 * len(wanted)
            self.counters.item_requests += 1
            self.counters.item_request_bytes += length
            self._sr_send(
                server.index,
                "sr:item_request",
                {
                    "src": server.index,
                    "dst": payload["src"],
                    "bucket": 0,
                    "window": True,
                    "ids": wanted,
                },
                length,
                dst=payload["src"],
            )
        if difference and moved == 0 and not wanted:
            self.counters.misdecodes += 1

    def _advertise(self, server, bucket):
        """Broadcast one bucket's summary. This is the only unsolicited message in the protocol."""
        if not self._in_catch_up_window():
            # Held rather than dropped: the bucket is still sealed and still worth advertising, just
            # not now. It will be picked up by the next interval tick inside the window.
            self.counters.adverts_deferred += 1
            return
        capacity = self.opts.capacity
        summary = server.summary(self.root_hash, bucket, capacity, self.width)
        if summary is None:
            return
        if self.opts.resolve == "enum":
            # The explicit arm advertises a checksum and a count and nothing else, so a peer
            # learns that it differs but not how. Resolution is a round trip longer by design.
            length = SR_ENVELOPE + SR_CHECKSUM
            body = None
        else:
            length = SR_ENVELOPE + SR_CHECKSUM + sketch_bytes(capacity, self.width)
            body = summary
        if self.opts.signed:
            length += SR_SIGNATURE
        # A broadcast advert is relayed by every node in earshot and is the reason adverts dominate
        # the byte budget. Once a server knows its peers - and an advert is itself the discovery
        # mechanism, so it does after the first one - the same information can go as a DM to each,
        # paying per peer instead of per neighbourhood.
        if self.opts.advert_transport == "dm":
            peers = [i for i in self.servers if i != server.index]
            for peer in peers:
                self.counters.adverts += 1
                self.counters.advert_bytes += length
                self._sr_send(
                    server.index,
                    "sr:advert",
                    {
                        "src": server.index,
                        "dst": peer,
                        "bucket": bucket,
                        "sketch": body,
                        "checksum": summary.checksum,
                        "count": summary.count,
                        "members": server.members(bucket),
                    },
                    length,
                    dst=peer,
                )
            return

        self.counters.adverts += 1
        self.counters.advert_bytes += length
        self._sr_send(
            server.index,
            "sr:advert",
            {
                "src": server.index,
                "dst": None,
                "bucket": bucket,
                "sketch": body,
                "checksum": summary.checksum,
                "count": summary.count,
                # Ground truth for the safety gate only, never read by the protocol. An advert is
                # a snapshot: the sender keeps ingesting while it is in flight, so a checksum has
                # to be judged against the set it was computed over, not the sender's later state.
                "members": server.members(bucket),
            },
            length,
        )

    def _recv_advert(self, server, payload):
        if payload.get("window"):
            self._recv_advert_window(server, payload)
            return
        self.counters.adverts_heard += 1
        bucket = payload["bucket"]
        local = server.summary(self.root_hash, bucket, self.opts.capacity, self.width)
        self.counters.exchanges += 1

        if local is not None and local.checksum == payload["checksum"]:
            server.matched.add((payload["src"], bucket))
            self.counters.checksum_closed += 1
            self._verify(server, payload, bucket)
            return
        self.counters.checksum_open += 1

        if local is None or payload["sketch"] is None or bucket in server.poisoned:
            # Nothing to XOR against, or an arm that never sends a sketch, or a bucket whose
            # verdict is already cached. All three go to enumeration.
            self._escalate(server, payload["src"], bucket)
            return

        difference = local.difference(payload["sketch"].sketch())
        if difference is None:
            self.counters.decode_failures += 1
            self._escalate(server, payload["src"], bucket)
            return

        self._resolve(server, payload["src"], bucket, difference)

    def _resolve(self, server, peer_index, bucket, difference):
        """Split the decoded difference by local membership and answer both halves."""
        peer = self.servers[peer_index]
        mine, theirs = [], []
        for sid in difference:
            if any(h in server.held for h in self._hashes_for(server, sid, bucket)):
                mine.append(sid)
            else:
                theirs.append(sid)

        moved = 0
        for sid in mine:
            for message_hash in self._hashes_for(server, sid, bucket):
                if message_hash in server.held and message_hash not in peer.held:
                    self._send_object(server, peer_index, message_hash)
                    moved += 1
        if theirs:
            length = SR_ENVELOPE + 4 * len(theirs)
            self.counters.item_requests += 1
            self.counters.item_request_bytes += length
            self._sr_send(
                server.index,
                "sr:item_request",
                {
                    "src": server.index,
                    "dst": peer_index,
                    "bucket": bucket,
                    "ids": theirs,
                },
                length,
                dst=peer_index,
            )
        if difference and moved == 0 and not theirs:
            # Decoded to something, moved nothing: the sketch named objects neither side lacks,
            # which is a misdecode. The checksum is what refuses it.
            self.counters.misdecodes += 1
            self._escalate(server, peer_index, bucket)

    def _hashes_for(self, server, sid, bucket):
        return [
            h
            for h in server.members(bucket)
            if truncated_short_id(h, self.width) == sid
        ]

    def _send_object(self, server, peer_index, message_hash):
        """Replay one object to a peer, carrying the outside-the-wrapper replay header.

        The header is what makes a broadcast replay useful to anyone other than the addressee: a node
        overhearing it learns when the archive first heard the message and that this is a replay, so
        it can file it in its own history in the right place instead of at the current time.
        """
        obj = self.generator.objects[message_hash]
        length = min(MAX_PAYLOAD, obj.wire_size + OBJECT_OVERHEAD + REPLAY_HEADER)
        self.counters.provides += 1
        self.counters.provide_bytes += length
        payload = {
            "src": server.index,
            "dst": peer_index,
            "hash": message_hash,
            # Quantised on the way out, the way the wire field would be, so the simulator carries
            # the coarseness rather than assuming it away.
            "heard_ago_s": min(
                REPLAY_MAX_TICKS,
                int((self.mesh.now - obj.rx_time) / 1000.0 / REPLAY_TICK_S),
            )
            * REPLAY_TICK_S,
            "replayed": True,
        }
        if self.opts.provide_transport == "broadcast":
            # Broadcast costs the neighbourhood a relay, and pays it back: every node in earshot that
            # lacks the message can file it correctly off the replay header. Whether that trade is
            # worth it is what the bystander counters measure.
            self._sr_send(server.index, "sr:item_provide", payload, length)
            return
        self._sr_send(server.index, "sr:item_provide", payload, length, dst=peer_index)

    def _recv_item_request(self, server, payload):
        if payload.get("window"):
            peer = self.servers[payload["src"]]
            for sid in payload["ids"]:
                for message_hash in [
                    h for h in server.held if truncated_short_id(h, self.width) == sid
                ]:
                    if not peer.holds(message_hash):
                        self._send_object(server, payload["src"], message_hash)
            return
        for sid in payload["ids"]:
            for message_hash in self._hashes_for(server, sid, payload["bucket"]):
                peer = self.servers[payload["src"]]
                if message_hash not in peer.held:
                    self._send_object(server, payload["src"], message_hash)

    def _recv_item_provide(self, server, payload):
        message_hash = payload["hash"]
        # Record the replay's account of when this was first heard *before* deciding whether to store
        # the object. A server that already holds the message is exactly the case worth keeping both
        # for: its own receive time and a peer's claim about the same message are what make drift
        # between archives measurable, and what would expose a peer lying about heard_ago.
        claimed_ms = self.mesh.now - payload.get("heard_ago_s", 0) * 1000.0
        server.note_replay(message_hash, claimed_ms)
        if message_hash in server.held:
            return

        counter, bucket, _ = self._assign(server, message_hash)
        if counter is None:
            return
        if self.opts.replay_ordering == "heard":
            # File the replay where it belongs in this server's own stream rather than at the tip.
            # This is what lets an old bucket converge: numbered at the tip, a transferred object
            # lands in the newest bucket and the bucket it came from can never agree with the peer's.
            placed = server.bucket_at(claimed_ms)
            if placed is not None:
                bucket = placed
                self.counters.replays_backfiled += 1
        if server.store.insert(self.generator.objects[message_hash], counter):
            server.held[message_hash] = counter
            server.bucket[message_hash] = bucket
            self.counters.objects_moved += 1

    def _escalate(self, server, peer_index, bucket):
        """Ask for the whole short-ID list. Correct, and priced by the bucket rather than the gap."""
        if self.opts.resolve == "sketch":
            # The pure-sketch arm has nowhere to escalate to; a failed decode simply waits for the
            # next advert. Keeping the arm honest means counting that, not quietly enumerating.
            self.counters.escalations += 1
            return
        self.counters.escalations += 1
        server.poisoned.add(bucket)
        length = SR_ENVELOPE
        self.counters.enum_requests += 1
        self.counters.enum_request_bytes += length
        self._sr_send(
            server.index,
            "sr:enum_request",
            {"src": server.index, "dst": peer_index, "bucket": bucket},
            length,
            dst=peer_index,
        )

    def _recv_enum_request(self, server, payload):
        bucket = payload["bucket"]
        members = sorted(server.members(bucket))
        ids = [truncated_short_id(h, self.width) for h in members]
        per_frame = max(1, (MAX_PAYLOAD - SR_ENVELOPE) // 4)
        for start in range(0, max(1, len(ids)), per_frame):
            chunk = ids[start : start + per_frame]
            length = SR_ENVELOPE + 4 * len(chunk)
            self.counters.enum_provides += 1
            self.counters.enum_provide_bytes += length
            self._sr_send(
                server.index,
                "sr:enum_provide",
                {
                    "src": server.index,
                    "dst": payload["src"],
                    "bucket": bucket,
                    "ids": chunk,
                    "hashes": [members[start + k] for k in range(len(chunk))],
                },
                length,
                dst=payload["src"],
            )

    def _recv_enum_provide(self, server, payload):
        """Explicit resolution: name what the peer has that we do not, and ask for exactly that."""
        bucket = payload["bucket"]
        wanted = [h for h in payload["hashes"] if h not in server.held]
        if not wanted:
            return
        ids = [truncated_short_id(h, self.width) for h in wanted]
        length = SR_ENVELOPE + 4 * len(ids)
        self.counters.item_requests += 1
        self.counters.item_request_bytes += length
        self._sr_send(
            server.index,
            "sr:item_request",
            {"src": server.index, "dst": payload["src"], "bucket": bucket, "ids": ids},
            length,
            dst=payload["src"],
        )

    def _verify(self, server, payload, bucket):
        """The gate. A checksum that closes must mean the two sets really were identical."""
        if server.members(bucket) != payload["members"]:
            self.counters.silent_losses += 1

    def _final_audit(self):
        """Every server pair, every bucket, at rest: does checksum equality imply set equality?

        The in-flight check can only judge the exchanges that happened. This one judges the end
        state, where nothing is in flight and no snapshot is stale, so a disagreement here is
        unambiguous. It is the same claim the three-store simulator made, restated over a mesh.
        """
        if self.opts.bucket_mode == "window":
            # There is no shared bucket to audit: the windows were never agreed. The in-flight gate
            # on identical windows is the only checksum test window mode admits, and it is counted.
            return 0
        keys = sorted(self.servers)
        buckets = set()
        for server in self.servers.values():
            buckets |= server.buckets()
        agree_and_differ = 0
        for i, a in enumerate(keys):
            for b in keys[i + 1 :]:
                for bucket in buckets:
                    sa = self.servers[a].summary(
                        self.root_hash, bucket, self.opts.capacity, self.width
                    )
                    sb = self.servers[b].summary(
                        self.root_hash, bucket, self.opts.capacity, self.width
                    )
                    if sa is None or sb is None:
                        continue
                    if sa.checksum == sb.checksum and self.servers[a].members(
                        bucket
                    ) != self.servers[b].members(bucket):
                        agree_and_differ += 1
        return agree_and_differ

    # ---- interval and AIMD triggers ---------------------------------------------------

    def _tick(self, server):
        """Fixed-interval or AIMD advertising: pick a bucket and state it."""
        if self.mesh.now > self.duration_ms:
            return
        if self.opts.bucket_mode == "window":
            before = self.counters.objects_moved
            self._advertise_window(server)
            if self.opts.trigger == "aimd":
                if self.counters.objects_moved > before:
                    server.interval_ms = self.opts.advert_interval_s * 1000.0
                else:
                    server.interval_ms = min(
                        server.interval_ms * 1.5,
                        self.opts.advert_max_interval_s * 1000.0,
                    )
            delay = server.interval_ms * self.rng.uniform(0.8, 1.2)
            self.mesh.at(self.mesh.now + delay, lambda: self._tick(server))
            return

        # Round-robin over the buckets this server actually has, whatever numbering produced them.
        own = sorted(server.buckets())
        if own:
            before = self.counters.objects_moved
            bucket = own[server.next_bucket % len(own)]
            server.next_bucket += 1
            self._advertise(server, bucket)
            if self.opts.trigger == "aimd":
                # Probe found nothing -> back off; found something -> straight back to the floor.
                found = self.counters.objects_moved > before
                if found:
                    server.interval_ms = self.opts.advert_interval_s * 1000.0
                else:
                    server.interval_ms = min(
                        server.interval_ms * 1.5,
                        self.opts.advert_max_interval_s * 1000.0,
                    )
        delay = server.interval_ms * self.rng.uniform(0.8, 1.2)
        self.mesh.at(self.mesh.now + delay, lambda: self._tick(server))

    # ---- run --------------------------------------------------------------------------

    def run(self):
        started = time.time()
        self.generator.schedule(self.duration_ms)
        if not self.opts.baseline and self.opts.trigger in (
            "interval",
            "aimd",
            "bucket+interval",
        ):
            for server in self.servers.values():
                start = self.rng.uniform(0, server.interval_ms)
                self.mesh.at(start, lambda s=server: self._tick(s))

        self.mesh.run(self.duration_ms + 900_000)
        self.final_audit_failures = 0 if self.opts.baseline else self._final_audit()
        return self._report(time.time() - started)

    def _report(self, wall_seconds):
        total = len(self.generator.text_order)
        depth_all = {}
        for i in range(self.opts.nodes):
            depth_all[i] = None

        # Baseline reception: what each node actually heard, and why it missed the rest.
        rates = (
            [len(self.heard_text[i]) / total for i in range(self.opts.nodes)]
            if total
            else []
        )
        reach = self._reach_ceiling()

        report = {
            "seed": self.seed,
            "wall_seconds": round(wall_seconds, 1),
            "opts": {
                k: v
                for k, v in vars(self.opts).items()
                if not k.startswith("_") and k not in ("func",)
            },
            "mesh": {
                **self.mesh.link_stats(),
                "nodes": self.opts.nodes,
                "area_km": round(self.area / 1000.0, 2),
                "nodes_per_km2": round(self.opts.nodes / (self.area / 1000.0) ** 2, 2),
                "hop_limit": self.opts.hop_limit,
                "routers": sum(1 for n in self.mesh.nodes if n.role == M.ROUTER),
            },
            "traffic": {
                "originated": dict(self.generator.originated),
                "congestion_coefficient": round(self.generator.congestion, 3),
                "text_objects": total,
                "airtime_ms": round(self.mesh.stats["airtime_ms"], 1),
                "channel_utilisation": round(
                    self.mesh.stats["airtime_ms"] / self.duration_ms, 3
                ),
                "airtime_by_kind": {
                    str(k): round(v / 1000.0, 1)
                    for k, v in self.mesh.airtime_by_kind.items()
                },
                **{k: v for k, v in self.mesh.stats.items() if k != "airtime_ms"},
            },
            "baseline": {
                "text_reception_mean": round(statistics.mean(rates), 4) if rates else 0,
                "text_reception_median": (
                    round(statistics.median(rates), 4) if rates else 0
                ),
                "text_reception_min": round(min(rates), 4) if rates else 0,
                "text_reception_max": round(max(rates), 4) if rates else 0,
                "reach_ceiling_mean": round(statistics.mean(reach), 4) if reach else 0,
                "missed_beyond_hop_limit": (
                    round(statistics.mean([1 - r for r in reach]), 4) if reach else 0
                ),
                "missed_within_reach": (
                    round(
                        statistics.mean(
                            [max(0.0, reach[i] - rates[i]) for i in range(len(rates))]
                        ),
                        4,
                    )
                    if rates
                    else 0
                ),
            },
        }

        if self.observers:
            rows = []
            for index, w in sorted(self.observers.items()):
                direct = len(w["direct"])
                extra = len(w["overheard"] - w["direct"])
                errors = w["placement_error_s"]
                rows.append(
                    {
                        "node": index,
                        "degree": w["degree"],
                        "hop_limit": w["hop_limit"],
                        "hops_to_nearest_server": w["hops_to_server"],
                        "direct": direct,
                        "overheard_new": extra,
                        "direct_fraction": round(direct / total, 4) if total else 0,
                        "with_overheard_fraction": (
                            round((direct + extra) / total, 4) if total else 0
                        ),
                        "median_placement_error_s": (
                            round(statistics.median(errors), 1) if errors else None
                        ),
                        "max_placement_error_s": (
                            round(max(errors), 1) if errors else None
                        ),
                    }
                )
            report["observers"] = rows

        if not self.opts.baseline:
            union = set()
            for server in self.servers.values():
                union |= set(server.held)
            per_server = (
                [len(s.held) / total for s in self.servers.values()] if total else []
            )
            report["sfpp"] = {
                "servers": sorted(self.servers),
                "separation_hops": self.server_separation(),
                "held_per_server": [len(s.held) for s in self.servers.values()],
                "held_fraction_mean": (
                    round(statistics.mean(per_server), 4) if per_server else 0
                ),
                "held_fraction_min": round(min(per_server), 4) if per_server else 0,
                "union_fraction": round(len(union) / total, 4) if total else 0,
                "sr_airtime_ms": round(
                    sum(
                        v
                        for k, v in self.mesh.airtime_by_kind.items()
                        if str(k).startswith("sr:")
                    ),
                    1,
                ),
                "sr_airtime_share": round(
                    sum(
                        v
                        for k, v in self.mesh.airtime_by_kind.items()
                        if str(k).startswith("sr:")
                    )
                    / max(1.0, self.mesh.stats["airtime_ms"]),
                    4,
                ),
                **self.counters.as_dict(),
                "audit_checksum_agrees_sets_differ": self.final_audit_failures,
                **self._drift_report(),
            }
        return report

    def _drift_report(self):
        """What holding both copies buys: how far apart two accounts of the same message are.

        A server that keeps its own receive time *and* every replay's claim can measure the spread
        between them. That spread is the useful telemetry - it is the drift between archives, and a
        claim far outside it is the signature of a peer lying about heard_ago.
        """
        spreads = []
        disagreements = 0
        for server in self.servers.values():
            for first_heard, claims in server.provenance.values():
                if not claims:
                    continue
                if first_heard is not None:
                    spreads.append(abs(min(claims) - first_heard) / 1000.0)
                if len(set(int(c / 1000) for c in claims)) > 1:
                    disagreements += 1
        return {
            "replay_claim_spread_median_s": (
                round(statistics.median(spreads), 1) if spreads else None
            ),
            "replay_claim_spread_p90_s": (
                round(sorted(spreads)[int(0.9 * (len(spreads) - 1))], 1)
                if spreads
                else None
            ),
            "replays_with_conflicting_claims": disagreements,
        }

    def _reach_ceiling(self):
        """The best any node could do: the share of the mesh within the hop limit of it.

        A message that originated five hops away was never going to arrive, and counting it as a
        loss would blame the radio for the routing. Separating the two is the point of the baseline.
        """
        out = []
        n = self.opts.nodes
        for i in range(n):
            depth = self.mesh.hops_from([i])
            within = sum(1 for k, v in depth.items() if 0 < v <= self.opts.hop_limit)
            out.append(within / (n - 1))
        return out

    def close(self):
        for server in self.servers.values():
            server.store.close()
        shutil.rmtree(self.db_dir, ignore_errors=True)


def build_parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=6.0)
    ap.add_argument("--nodes", type=int, default=60)
    ap.add_argument("--area", type=float, default=8000.0)
    ap.add_argument("--hop-limit", type=int, default=3)
    ap.add_argument("--router-fraction", type=float, default=0.1)
    ap.add_argument(
        "--diurnal",
        default="flat",
        choices=("flat", "sinusoid", "commuter"),
        help="time-of-day shape for human-driven traffic; device timers stay flat",
    )
    ap.add_argument("--start-hour", type=float, default=8.0)
    ap.add_argument(
        "--catch-up-hours",
        default="",
        help='defer reconciliation to the quiet hours, e.g. "02-06". Empty means reconcile any time',
    )
    ap.add_argument(
        "--no-congestion-scaling",
        action="store_true",
        help="disable the firmware's node-count broadcast scaling (Default.h congestionScalingCoefficient)",
    )
    ap.add_argument("--position-throttle", type=int, default=1)
    ap.add_argument("--telemetry-throttle", type=int, default=1)
    ap.add_argument(
        "--scale-area",
        action="store_true",
        help="grow the area with the node count so density is held constant, not conflated with size",
    )
    ap.add_argument(
        "--hop-spread",
        action="store_true",
        help="per-node hop limits 3-7 by centrality, instead of one value for everyone",
    )
    ap.add_argument(
        "--replay-ordering",
        default="tip",
        choices=("tip", "heard"),
        help="number a replayed object at the receiving tip, or file it by its heard_ago",
    )
    ap.add_argument(
        "--observers",
        type=int,
        default=6,
        help="ordinary nodes instrumented to report the bystander view",
    )
    ap.add_argument(
        "--provide-transport",
        default="dm",
        choices=("dm", "broadcast"),
        help="replay objects to the requester only, or broadcast so bystanders can file them too",
    )
    ap.add_argument(
        "--advert-transport",
        default="broadcast",
        choices=("broadcast", "dm"),
        help="broadcast adverts and let anyone hear them, or DM each known peer",
    )
    ap.add_argument("--preset", default="LONG_FAST")
    ap.add_argument("--no-phy-loss", action="store_true")
    ap.add_argument(
        "--extra-loss",
        type=float,
        default=0.0,
        help="flat loss floor on every reception, on top of the modelled physics",
    )
    ap.add_argument(
        "--burst-loss",
        type=float,
        default=0.0,
        help="chance a node is deaf for a whole burst window, instead of losing packets evenly",
    )
    ap.add_argument(
        "--burst-ms",
        type=float,
        default=60000.0,
        help="length of one deafness window; only bites a bucket when it approaches the fill time",
    )

    ap.add_argument("--baseline", action="store_true", help="no SF++ servers at all")
    ap.add_argument("--servers", type=int, default=3)
    ap.add_argument("--place", default="spread", choices=sorted(Placement.BY_NAME))
    ap.add_argument("--hops-apart", type=int, default=3)

    ap.add_argument(
        "--bucket-mode",
        default="local",
        choices=("global", "local", "time", "window"),
        help="how a server numbers objects into buckets; `local` is the only real one",
    )
    ap.add_argument(
        "--window-size",
        type=int,
        default=32,
        help="objects in the sliding window for --bucket-mode window",
    )
    ap.add_argument(
        "--time-bucket-s",
        type=float,
        default=1800.0,
        help="window width for --bucket-mode time",
    )
    ap.add_argument("--capacity", type=int, default=32)
    ap.add_argument("--short-id-bits", type=int, default=32)
    ap.add_argument("--signed", action="store_true")
    ap.add_argument(
        "--trigger",
        default="bucket",
        choices=("bucket", "interval", "aimd", "bucket+interval"),
    )
    ap.add_argument("--resolve", default="hybrid", choices=("sketch", "enum", "hybrid"))
    ap.add_argument("--advert-interval-s", type=float, default=300.0)
    ap.add_argument("--advert-max-interval-s", type=float, default=3600.0)
    ap.add_argument("--advert-jitter-s", type=float, default=30.0)

    ap.add_argument("--seed", type=int, help="omit to draw one at random and record it")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--out", help="write the report JSON here")
    ap.add_argument("--label", default="")
    return ap


def run_once(opts, seed):
    campaign = Campaign(opts, seed)
    try:
        return campaign.run()
    finally:
        campaign.close()


def main(argv=None):
    opts = build_parser().parse_args(argv)
    reports = []
    for repeat in range(opts.repeats):
        seed = (
            opts.seed
            if opts.seed is not None
            else random.SystemRandom().randrange(1 << 31)
        )
        if opts.seed is not None and opts.repeats > 1:
            seed = opts.seed + repeat
        report = run_once(opts, seed)
        report["label"] = opts.label
        reports.append(report)
        summarise(report)
    if opts.out:
        os.makedirs(os.path.dirname(os.path.abspath(opts.out)), exist_ok=True)
        with open(opts.out, "w") as f:
            json.dump(reports if len(reports) > 1 else reports[0], f, indent=2)
        print(f"wrote {opts.out}")
    return 0


def summarise(report):
    base, traffic = report["baseline"], report["traffic"]
    print(
        f"seed {report['seed']}  {report['mesh']['nodes']} nodes  deg "
        f"{report['mesh']['mean_degree']:.1f}  util {traffic['channel_utilisation']:.0%}  "
        f"{traffic['text_objects']} texts  {report['wall_seconds']}s"
    )
    print(
        f"  baseline reception  mean {base['text_reception_mean']:.3f}  "
        f"median {base['text_reception_median']:.3f}  "
        f"ceiling {base['reach_ceiling_mean']:.3f}  "
        f"(beyond hops {base['missed_beyond_hop_limit']:.3f}, "
        f"lost within reach {base['missed_within_reach']:.3f})"
    )
    if "sfpp" in report:
        s = report["sfpp"]
        print(
            f"  servers {s['servers']}  separation {s['separation_hops']}  "
            f"held {s['held_fraction_mean']:.3f} (min {s['held_fraction_min']:.3f})  "
            f"union {s['union_fraction']:.3f}"
        )
        print(
            f"  adverts {s['adverts']} ({s['advert_bytes']} B)  moved {s['objects_moved']}  "
            f"decode fail {s['decode_failures']}  misdecode {s['misdecodes']}  "
            f"escalations {s['escalations']}  SR airtime {s['sr_airtime_share']:.1%}"
        )
        print(
            f"  SILENT LOSSES {s['silent_losses']}  "
            f"final audit {s['audit_checksum_agrees_sets_differ']}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
