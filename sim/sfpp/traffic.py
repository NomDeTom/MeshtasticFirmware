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
# `commuter` is the two-peak human pattern: a morning bump, a lull, a larger evening peak, and a deep
# overnight trough. `sinusoid` is the naive single-peak version, kept because it is what most models
# reach for and it is worth being able to show the difference. Measured hourly weights from a real
# packet feed would replace `commuter`, which is a shape drawn from how people behave rather than from
# data - see the plan's stretch goal.
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


def congestion_coefficient(node_count, sf, bw_hz, event_mode=False):
    """The firmware's own broadcast-interval scaling, from Default.h:106.

    At or below 40 nodes it is 1.0. Above that, every extra node lengthens device-originated
    broadcast intervals by 2^SF / (BW_kHz * 100) - which on LONG_FAST is 0.08192 per node, so a
    150-node mesh stretches its intervals by a factor of ten. A size sweep that ignores this models
    a mesh nobody running 2.8 would actually have.
    """
    if node_count <= 40:
        return 1.0
    divisor = 25.0 if event_mode else 100.0
    throttling_factor = (2.0**sf) / ((bw_hz / 1000.0) * divisor)
    return 1.0 + (node_count - 40) * throttling_factor


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
        position_throttle=1,
        telemetry_throttle=1,
        online_cap=120,
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
        # nothing in the firmware throttles a person deciding to send a message.
        self.congestion = (
            # getNumOnlineMeshNodes() iterates the hot store, so a node cannot count mesh members it
            # has evicted. The coefficient is bounded by MAX_NUM_NODES, not by mesh size.
            congestion_coefficient(
                min(len(mesh.nodes), online_cap), preset["sf"], preset["bw"]
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

    def _size(self, cls):
        return max(8, int(self.rng.gauss(cls.mean_bytes, cls.sigma_bytes)))

    def schedule(self, duration_ms):
        """Lay every originated packet onto the mesh's event queue."""
        for cls in self.mix:
            if cls.archived:
                rate = cls.per_hour * self.text_scale
            else:
                rate = cls.per_hour / self.congestion / self.throttle.get(cls.name, 1)
            if rate <= 0:
                continue
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
                    if not diurnal:
                        self._schedule_one(node, cls, t)
                    else:
                        hour = (self.start_hour + t / 3600_000.0) % 24
                        if (
                            self.rng.random()
                            < diurnal_weight(self.diurnal, hour) / peak
                        ):
                            self._schedule_one(node, cls, t)
                    t += self.rng.expovariate(1.0 / mean_gap_ms)

    def _schedule_one(self, node, cls, when):
        size = self._size(cls)

        def emit(node=node, cls=cls, size=size):
            if cls.archived:
                packet = self.mesh.originate(
                    node, cls.portnum, size, kind=cls.name, payload=None
                )
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
