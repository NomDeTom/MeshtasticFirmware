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


class Generator:
    """Schedules every node's originated traffic across the run, then hands it to the mesh.

    Emission is a Poisson process per class per node - exponential gaps rather than a fixed period,
    because synchronised senders would understate collisions and every node in a real mesh has its
    own phase.
    """

    def __init__(self, mesh, rng, root_hash, mix=DEFAULT_MIX, text_scale=1.0):
        self.mesh = mesh
        self.rng = rng
        self.root_hash = root_hash
        self.mix = mix
        self.text_scale = text_scale
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
            rate = cls.per_hour * (self.text_scale if cls.archived else 1.0)
            if rate <= 0:
                continue
            mean_gap_ms = 3600_000.0 / rate
            for node in self.emitters[cls.name]:
                t = self.rng.expovariate(1.0 / mean_gap_ms)
                while t < duration_ms:
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
