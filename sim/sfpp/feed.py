"""Real packets in, SF++ objects out.

Reads a meshtastic-compression-test packet log - one decrypted packet per line, hex bytes, a
16-byte radio header followed by the Data protobuf - and turns the text messages into the objects
an SF++ store would hold. The hashes are computed the way ingestTextPacket() computes them, so the
short IDs and checksum contributions in the simulator are the ones the firmware would derive from
the same traffic.

The capture is default-key traffic only, so it under-represents private channels and DMs. It is
used for the *distribution* of sizes, senders and inter-arrival gaps, not as ground truth about
what a mesh carries.
"""

import gzip
import hashlib
from dataclasses import dataclass

HEADER_LEN = 16
BROADCAST = 0xFFFFFFFF
TEXT_PORTS = (1, 7)  # TEXT_MESSAGE, TEXT_MESSAGE_COMPRESSED
HASH_SIZE = 16  # SFPP_HASH_SIZE


@dataclass
class Message:
    """One object on a chain. Field names follow the channel_messages columns."""

    destination: int
    sender: int
    packet_id: int
    rx_time: int
    root_hash: bytes
    encrypted_bytes: bytes
    message_hash: bytes
    commit_hash: bytes
    payload: str

    @property
    def wire_size(self):
        """Bytes the object costs to move, ignoring the SF++ envelope."""
        return len(self.encrypted_bytes)


def message_hash_of(encrypted_bytes, to, frm, packet_id):
    """SHA-256(encrypted || to || from || id) truncated to 16 bytes.

    Mirrors recalculateMessageHash(): the three integers are appended in their native little-endian
    layout, which is what memcpy of a uint32_t produces on every platform SF++ builds for.
    """
    h = hashlib.sha256()
    h.update(encrypted_bytes)
    h.update(to.to_bytes(4, "little"))
    h.update(frm.to_bytes(4, "little"))
    h.update(packet_id.to_bytes(4, "little"))
    return h.digest()[:HASH_SIZE]


def _read_varint(data, i):
    value, shift = 0, 0
    while i < len(data):
        byte = data[i]
        value |= (byte & 0x7F) << shift
        i += 1
        if not byte & 0x80:
            return value, i
        shift += 7
        if shift > 28:
            break
    return None, i


def parse_line(line):
    try:
        raw = bytes.fromhex(line.replace(" ", ""))
    except ValueError:
        return None
    if len(raw) < HEADER_LEN + 2 or raw[HEADER_LEN] != 0x08:
        return None
    portnum, _ = _read_varint(raw, HEADER_LEN + 1)
    if portnum is None:
        return None
    # The capture stores {from, to, id}, not the on-air PacketHeader order: 0xFFFFFFFF appears at
    # offset 4 in 67.6% of packets and never at offset 0.
    return {
        "src": int.from_bytes(raw[0:4], "little"),
        "dst": int.from_bytes(raw[4:8], "little"),
        "id": int.from_bytes(raw[8:12], "little"),
        "portnum": portnum,
        "raw": raw,
    }


def load(path, root_hash, limit=None, text_only=True, broadcast_only=True):
    """Yield Messages in capture order.

    The capture carries no receive timestamps, so rx_time is the object's position in the stream;
    callers that need a clock impose one. Ordering is what matters here - the chain counter follows
    it, and buckets follow the counter.
    """
    opener = gzip.open if str(path).endswith(".gz") else open
    out = []
    with opener(path, "rt") as f:
        for index, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            rec = parse_line(line)
            if rec is None:
                continue
            if text_only and rec["portnum"] not in TEXT_PORTS:
                continue
            if broadcast_only and rec["dst"] != BROADCAST:
                continue

            # The capture is already decrypted, so the payload stands in for the ciphertext. It is
            # the same length and the same uniqueness, which is all the hash needs.
            encrypted = rec["raw"][HEADER_LEN:]
            out.append(
                Message(
                    destination=rec["dst"],
                    sender=rec["src"],
                    packet_id=rec["id"],
                    rx_time=index,
                    root_hash=root_hash,
                    encrypted_bytes=encrypted,
                    message_hash=message_hash_of(
                        encrypted, rec["dst"], rec["src"], rec["id"]
                    ),
                    commit_hash=b"\x00" * HASH_SIZE,
                    payload="",
                )
            )
            if limit and len(out) >= limit:
                break
    return out


def synthetic(count, root_hash, seed=0, mean_size=53):
    """Seeded stand-in for the capture, for runs that must be reproducible without the file.

    A trace cannot be replayed into a different parameter set and still be the same experiment, so
    sweeps run on this and the capture corroborates.
    """
    import random

    rng = random.Random(seed)
    out = []
    for i in range(count):
        size = max(8, int(rng.gauss(mean_size, 20)))
        encrypted = bytes(rng.getrandbits(8) for _ in range(size))
        dst, src, pid = BROADCAST, rng.getrandbits(32), rng.getrandbits(32)
        out.append(
            Message(
                destination=dst,
                sender=src,
                packet_id=pid,
                rx_time=i,
                root_hash=root_hash,
                encrypted_bytes=encrypted,
                message_hash=message_hash_of(encrypted, dst, src, pid),
                commit_hash=b"\x00" * HASH_SIZE,
                payload="",
            )
        )
    return out
