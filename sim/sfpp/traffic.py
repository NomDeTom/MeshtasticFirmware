"""The offered load: what a Meshtastic channel actually carries, not just the part SF++ archives.

Reconciliation cost only means something against the traffic it shares a channel with. A mesh whose
only packets are text messages would make an advert look expensive; a real one is mostly position
and telemetry, and the archived class is a minority of it. Rates below are per node per hour and
sized so text lands at roughly a seventh of originated packets - what a channel looks like with
telemetry on and a normal share of people talking.

Text is the only archived class. Everything else exists to contend for the channel, to be relayed,
and to be missed - which is what the baseline measures.
"""

import hashlib
import math
from dataclasses import dataclass

# Portnums, from mesh.proto.
TEXT_MESSAGE_APP = 1
POSITION_APP = 3
NODEINFO_APP = 4
TELEMETRY_APP = 67

HASH_SIZE = 16  # SFPP_HASH_SIZE
BROADCAST = 0xFFFFFFFF


@dataclass(frozen=True)
class Class:
    name: str
    portnum: int
    per_hour: float
    mean_bytes: float
    sigma_bytes: float
    node_fraction: float  # share of nodes that emit this class at all
    archived: bool = False


# Payload sizes are the Data protobuf, which is what airtime is charged on after the 16-byte header.
DEFAULT_MIX = (
    Class("position", POSITION_APP, 4.0, 20, 4, 1.0),
    Class("telemetry", TELEMETRY_APP, 2.0, 24, 6, 1.0),
    Class("nodeinfo", NODEINFO_APP, 1.0, 40, 8, 1.0),
    Class("text", TEXT_MESSAGE_APP, 1.2, 53, 20, 0.4, archived=True),
)


def message_hash_of(encrypted_bytes, to, frm, packet_id):
    """SHA-256(encrypted || to || from || id) truncated to 16 bytes - recalculateMessageHash()."""
    h = hashlib.sha256()
    h.update(encrypted_bytes)
    h.update(to.to_bytes(4, "little"))
    h.update(frm.to_bytes(4, "little"))
    h.update(packet_id.to_bytes(4, "little"))
    return h.digest()[:HASH_SIZE]


@dataclass
class TextObject:
    """An archived text broadcast, carrying the fields the SF++ store keeps."""

    destination: int
    sender: int
    packet_id: int
    rx_time: int
    root_hash: bytes
    encrypted_bytes: bytes
    message_hash: bytes
    commit_hash: bytes
    payload: str = ""

    @property
    def wire_size(self):
        return len(self.encrypted_bytes)


# Hourly weights, local time, index 0 = midnight. Normalised by the caller so the daily mean matches
# the configured rate whichever shape is chosen - a diurnal curve should move *when* traffic happens,
# not how much of it there is, or the shapes would not be comparable.
#
# `commuter` is the two-peak human pattern: a morning bump, a lull, a larger evening peak and a deep
# overnight trough. `sinusoid` is the single-peak version, kept for comparison. Both are drawn from
# how people behave rather than from a measured feed.
DIURNAL = {
    "flat": [1.0] * 24,
    "sinusoid": [
        1.0 + 0.7 * math.sin((h - 15) / 24.0 * 2 * math.pi) for h in range(24)
    ],
    "commuter": [
        0.25,
        0.18,
        0.14,
        0.12,
        0.15,
        0.30,  # 00-05 overnight trough
        0.70,
        1.30,
        1.55,
        1.20,
        1.00,
        0.95,  # 06-11 morning peak then settle
        1.05,
        1.00,
        0.95,
        1.00,
        1.35,
        1.85,  # 12-17 afternoon into the evening rise
        2.10,
        1.95,
        1.60,
        1.15,
        0.70,
        0.40,  # 18-23 evening peak and decline
    ],
}


def diurnal_weight(shape, hour_of_day):
    weights = DIURNAL[shape]
    return weights[int(hour_of_day) % 24] / (sum(weights) / 24.0)


# Default.h congestionScalingCoefficient, 2.5 and 2.6: the per-node throttle depends on the modem
# preset, and the two shortest presets switch it off entirely rather than throttling at all.
PRESET_THROTTLING_FACTOR = {
    "MEDIUM_SLOW": 0.04,
    "MEDIUM_FAST": 0.02,
    "SHORT_SLOW": 0.01,
    "SHORT_FAST": None,
    "SHORT_TURBO": None,
}
PRESET_THROTTLING_DEFAULT = 0.075

# 2.5 through v2.7.16 shorten intervals on a small mesh instead of leaving them alone. Removed in
# v2.7.17, so the 2.7 profile - which is v2.7.21 - does not have it.
SMALL_MESH_SPEEDUP = ((10, 0.6), (20, 0.7), (30, 0.8))

# `--congestion-input utilisation` has no node count to feed the coefficient, so busy-ness is mapped
# onto the same 40-node pivot the throttle turns at. The firmware has no such mode - this is the
# candidate input round four exists to price, not a reconstruction of anything - and the mapping is a
# stated assumption: 40% busy reads as the 40 nodes at which throttling begins.
UTILISATION_PIVOT_PERCENT = 40.0
UTILISATION_PIVOT_NODES = 40


def congestion_coefficient(
    node_count, sf, bw_hz, event_mode=False, model="sf_bw", preset="LONG_FAST"
):
    """The firmware's own broadcast-interval scaling, Default.h congestionScalingCoefficient.

    A multiplier on every periodic broadcast interval. Three models, one per era:

    - `flat` (2.4): 1.0 to 40 nodes, then 0.075 per extra node whatever the preset.
    - `preset` (2.5, 2.6): a per-preset factor, no throttle at all on SHORT_FAST or SHORT_TURBO, and
      a coefficient *below* 1.0 up to 30 nodes - a small mesh is deliberately made chattier.
    - `sf_bw` (2.7, 2.8): 2^SF / (BW_kHz * divisor), which on LONG_FAST is 0.08192 per node, so a
      150-node mesh stretches its intervals by a factor of ten. The divisor is 100, or 25 in event
      mode.
    """
    if model == "preset":
        for bound, coefficient in SMALL_MESH_SPEEDUP:
            if node_count <= bound:
                return coefficient
    if node_count <= 40:
        return 1.0
    if model == "flat":
        throttling_factor = PRESET_THROTTLING_DEFAULT
    elif model == "preset":
        throttling_factor = PRESET_THROTTLING_FACTOR.get(
            preset, PRESET_THROTTLING_DEFAULT
        )
        if throttling_factor is None:
            return 1.0
    else:
        divisor = 25.0 if event_mode else 100.0
        throttling_factor = (2.0**sf) / ((bw_hz / 1000.0) * divisor)
    return 1.0 + (node_count - 40) * throttling_factor


def observed_senders(mesh, window_ms=7200_000.0):
    """Distinct senders heard recently, mesh-wide.

    What the mesh can see of its own size, as against what a hot store can hold: this is bounded by
    the packet history rather than by MAX_NUM_NODES, so it keeps rising after the hot-store count
    has saturated. Reported next to the congestion coefficient so that saturation is visible in the
    output rather than inferred from it.

    The sender comes from the PacketHistory record. Counting packet ids instead would count packets,
    not senders, since every packet carries its own id.
    """
    now = mesh.now
    senders = set()
    for node in mesh.nodes:
        for packet_id, when in node.seen.items():
            if now - when > window_ms:
                continue
            record = node.history.get(packet_id)
            if record is not None:
                senders.add(record.sender)
    return len(senders)


class Generator:
    """Schedules every node's originated traffic across the run, then hands it to the mesh.

    Emission is a Poisson process per class per node - exponential gaps rather than a fixed period,
    because synchronised senders would understate collisions and every node in a real mesh has its
    own phase.
    """

    def __init__(
        self,
        mesh,
        rng,
        root_hash,
        mix=DEFAULT_MIX,
        text_scale=1.0,
        congestion_scaling=True,
        congestion_mode="adaptive",
        position_throttle=1,
        telemetry_throttle=1,
        online_cap=120,
        congestion_input="hotstore",
        broadcast_interval_s=None,
        diurnal="flat",
        start_hour=8.0,
    ):
        self.mesh = mesh
        self.rng = rng
        self.root_hash = root_hash
        self.mix = mix
        self.text_scale = text_scale
        preset = mesh.conf.current_preset
        # Device-originated broadcasts stretch with mesh size; user-typed text does not, because
        # nothing in the firmware throttles a person deciding to send a message. Which era's
        # throttle applies comes from the mesh's own default profile, so a 2.5 mesh gets the
        # per-preset table and its small-mesh speedup rather than 2.8's SF/BW curve.
        #
        # `adaptive` recomputes the coefficient per node from that node's own online count at the
        # moment it would send, which is what Default::getConfiguredOrDefaultMsScaled does on every
        # interval. `static` computes one mesh-wide coefficient up front - what this generator used
        # to do, and what earlier runs were measured under.
        profile = mesh.nodes[0].profile if mesh.nodes else None
        self.congestion_mode = congestion_mode if congestion_scaling else "off"
        self.congestion_model = (
            profile.congestion_model if profile is not None else "sf_bw"
        )
        self.preset_name = getattr(mesh.conf, "MODEM_PRESET", "LONG_FAST")
        self.sf = preset["sf"]
        self.bw = preset["bw"]
        self.online_cap = online_cap
        # Which quantity drives the throttle. `hotstore` is what the firmware does and is bounded by
        # MAX_NUM_NODES, so it saturates on a mesh larger than the store - the pathology round four
        # exists to price. `truesize` is the unbounded ideal, the upper bound on what a corrected
        # input could buy. `utilisation` scales on measured channel busy-ness instead of a node count,
        # which is what the throttle actually cares about and cannot be bounded by memory.
        self.congestion_input = congestion_input
        # The coefficient falls below 1 on the 2.5 and 2.6 models, which speed a small mesh up.
        # Thinning needs a candidate rate at least as high as anything later selected from it, so
        # candidates are generated against the most permissive coefficient the model can produce.
        # Across every profile present, not just node zero's. On a mixed mesh a node whose own
        # model is more permissive than the sampled floor can never be thinned - floor/coefficient
        # exceeds 1 - while its candidates were generated at the stricter rate, so it silently sends
        # less than its own model calls for. That is exactly the --legacy-fraction arm the parameter
        # exists to characterise.
        models = {node.profile.congestion_model for node in mesh.nodes} or {
            self.congestion_model
        }
        self.congestion_floor = (
            min(c for _, c in SMALL_MESH_SPEEDUP) if "preset" in models else 1.0
        )
        self.congestion = (
            # getNumOnlineMeshNodes() iterates the hot store, so a node cannot count mesh members it
            # has evicted. The coefficient is bounded by MAX_NUM_NODES, not by mesh size.
            congestion_coefficient(
                (
                    len(mesh.nodes)
                    if congestion_input == "truesize"
                    else min(len(mesh.nodes), online_cap)
                ),
                self.sf,
                self.bw,
                model=self.congestion_model,
                preset=self.preset_name,
            )
            if congestion_scaling
            else 1.0
        )
        # Region profile multipliers, RegionProfile::positionThrottle / telemetryThrottle. Integer,
        # 1 is neutral, and applied on top of the congestion coefficient.
        self.throttle = {
            "position": max(1, position_throttle),
            "telemetry": max(1, telemetry_throttle),
        }
        # Text follows the clock because a person sends it. Telemetry and nodeinfo do not - a device
        # reports on a timer regardless of the hour. Position sits in between and is treated as
        # human-driven, since a node only has a new position when someone has moved it.
        # One interval for every device-originated class, the way an operator sets it. None keeps the
        # per-class mix. This is the denominator every SF++ airtime share is quoted against, so it
        # deserves to be a knob rather than an assumption.
        self.broadcast_interval_s = broadcast_interval_s
        self.diurnal = diurnal
        self.diurnal_classes = {"text", "position"}
        self.start_hour = start_hour
        self.emitters = {}
        self.objects = (
            {}
        )  # message_hash -> TextObject, the ground truth for the archive
        self.text_order = (
            []
        )  # message_hash in origination order; the chain counter follows it
        self.originated = {c.name: 0 for c in mix}

        node_count = len(mesh.nodes)
        for cls in mix:
            count = max(1, int(round(node_count * cls.node_fraction)))
            chosen = rng.sample(range(node_count), count)
            self.emitters[cls.name] = set(chosen)

    def node_congestion(self, node_index):
        """The coefficient this node would apply right now, from its own view of the mesh.

        `congestion_input` chooses what that view is, which is the whole point of the arm:

        - `hotstore` is what the firmware does. getNumOnlineMeshNodes() walks the hot store, so the
          input is bounded twice - by the store, which cannot hold more than MAX_NUM_NODES, and by
          the two-hour NUM_ONLINE_SECS window - and therefore saturates on a mesh larger than the
          store, which is exactly where the throttle is most needed. The node counts itself, as the
          firmware does by iterating a table that contains its own record.
        - `truesize` is the unbounded ideal: the mesh as it really is, which no device can see. The
          upper bound on what a corrected input could buy.
        - `utilisation` drops the node count entirely and scales on measured channel busy-ness,
          which is what the throttle actually cares about and is the one input memory cannot bound.
          The busy share is mapped onto the same 40-node pivot so the three are comparable: 0% busy
          reads as an empty mesh, 100% as one saturated well past the pivot.
        """
        if self.congestion_mode != "adaptive":
            return self.congestion
        node = self.mesh.nodes[node_index]
        if self.congestion_input == "truesize":
            online = len(self.mesh.nodes)
        elif self.congestion_input == "utilisation":
            busy = node.channel_utilization_percent(self.mesh.now)
            online = int(UTILISATION_PIVOT_NODES * busy / UTILISATION_PIVOT_PERCENT)
        else:
            online = min(node.num_online(self.mesh.now) + 1, self.online_cap)
        return congestion_coefficient(
            online,
            self.sf,
            self.bw,
            model=node.profile.congestion_model,
            preset=self.preset_name,
            event_mode=node.profile.event_relay_hop_limit is not None,
        )

    def _size(self, cls):
        return max(8, int(self.rng.gauss(cls.mean_bytes, cls.sigma_bytes)))

    def schedule(self, duration_ms):
        """Lay every originated packet onto the mesh's event queue."""
        for cls in self.mix:
            if cls.archived:
                rate = cls.per_hour * self.text_scale
            else:
                # One operator-set interval for every device class when given, otherwise the per-class
                # mix. Congestion scaling and the region throttle apply on top either way, so this
                # measures the operator's choice rather than replacing the firmware's own scaling.
                base = (
                    3600.0 / self.broadcast_interval_s
                    if self.broadcast_interval_s
                    else cls.per_hour
                )
                # Under `adaptive` the coefficient is not known until the moment of sending, so
                # candidates are laid down at the most permissive rate the model allows and thinned
                # at emit time against each node's own view. Under `static` the coefficient is a
                # constant and divides the rate directly.
                rate = base / self.throttle.get(cls.name, 1)
                rate = (
                    rate / self.congestion_floor
                    if self.congestion_mode == "adaptive"
                    else rate / self.congestion
                )
            if rate <= 0:
                continue
            adaptive = self.congestion_mode == "adaptive" and not cls.archived
            diurnal = self.diurnal != "flat" and cls.name in self.diurnal_classes
            peak = (
                max(DIURNAL[self.diurnal]) / (sum(DIURNAL[self.diurnal]) / 24.0)
                if diurnal
                else 1.0
            )
            # Non-homogeneous Poisson by thinning: generate at the peak rate and keep each candidate
            # with probability weight(t)/peak. Simpler and less error-prone than integrating the rate
            # curve, and it produces the right arrival process rather than a rescaled uniform one.
            mean_gap_ms = 3600_000.0 / (rate * peak)
            for node in self.emitters[cls.name]:
                t = self.rng.expovariate(1.0 / mean_gap_ms)
                while t < duration_ms:
                    keep = True
                    if diurnal:
                        hour = (self.start_hour + t / 3600_000.0) % 24
                        keep = self.rng.random() < diurnal_weight(self.diurnal, hour) / peak
                    if keep:
                        self._schedule_one(node, cls, t, adaptive=adaptive)
                    t += self.rng.expovariate(1.0 / mean_gap_ms)

    def _schedule_one(self, node, cls, when, adaptive=False):
        size = self._size(cls)

        def emit(node=node, cls=cls, size=size):
            if adaptive:
                # Thinning against the node's live coefficient: a candidate survives with
                # probability floor/coefficient, so a node whose store says the mesh is large sends
                # proportionally less often, and one on a small 2.5 mesh sends more often.
                if self.rng.random() > self.congestion_floor / self.node_congestion(node):
                    return
            if cls.archived:
                packet = self.mesh.originate(
                    node, cls.portnum, size, kind=cls.name, payload=None
                )
                if packet is None:
                    return  # the node is offline; nothing was composed, so nothing is archived
                obj = self._make_object(node, packet.id, size)
                packet.payload = obj.message_hash
                self.objects[obj.message_hash] = obj
                self.text_order.append(obj.message_hash)
            else:
                self.mesh.originate(node, cls.portnum, size, kind=cls.name)
            self.originated[cls.name] += 1

        self.mesh.at(when, emit)

    def _make_object(self, node, packet_id, size):
        """The object the archive would hold: ciphertext stands in at the same length.

        The capture the earlier runs used was already decrypted, so it did the same thing. Length
        and uniqueness are all the hash needs, and using the real derivation keeps the short IDs and
        checksum contributions identical to the ones the firmware would compute.
        """
        encrypted = self.rng.randbytes(size)
        return TextObject(
            destination=BROADCAST,
            sender=node,
            packet_id=packet_id,
            rx_time=int(self.mesh.now),
            root_hash=self.root_hash,
            encrypted_bytes=encrypted,
            message_hash=message_hash_of(encrypted, BROADCAST, node, packet_id),
            commit_hash=b"\x00" * HASH_SIZE,
        )
