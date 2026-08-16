"""The set layer above PinSketch, ported from src/modules/Native/SketchIndex.cpp.

Two identifiers per object, both derived from its hash with no shared key:

  - a 32-bit short ID, the sketch member;
  - a 64-bit domain-separated checksum contribution, XOR-accumulated per bucket.

The short ID is truncated and therefore collides, so it cannot carry correctness. The checksum is
what closes: two sketches that cancel prove nothing, two checksums that match do.
"""

import hashlib

from . import pinsketch

# Domain separation for the checksum half of the pair. Any change to this string changes every
# stored contribution and is a wire break.
CHECKSUM_DOMAIN = b"sfpp-ck-v3"

# A full bucket's sketch is 128 bytes, which leaves room for the advert envelope in one frame.
BUCKET_OBJECTS = 32


def short_id(object_id):
    """The sketch member: first non-zero big-endian word of SHA-256(object_id)."""
    if not object_id:
        return 0
    digest = hashlib.sha256(object_id).digest()
    # Zero is not a representable sketch member, so a zero word falls through to the next one. Both
    # sides run the same walk, so the result stays universally comparable.
    for i in range(0, len(digest) - 3, 4):
        candidate = int.from_bytes(digest[i : i + 4], "big")
        if candidate != 0:
            return candidate
    return 1


def checksum_contribution(object_id):
    """The object's 64-bit contribution to its bucket checksum, domain-separated from the short ID."""
    if not object_id:
        return 0
    return int.from_bytes(
        hashlib.sha256(CHECKSUM_DOMAIN + object_id).digest()[:8], "big"
    )


def bucket_of(chain_counter):
    """Chain counters are 1-based; counter 0 means the object is not on the chain and has no bucket."""
    if chain_counter == 0:
        return None
    return (chain_counter - 1) // BUCKET_OBJECTS


def bucket_range(bucket):
    """The inclusive counter range a bucket covers, matching buildBucketSummary()'s query."""
    first = bucket * BUCKET_OBJECTS + 1
    return first, first + BUCKET_OBJECTS - 1


class BucketSummary:
    """One bucket's summary: what an advert carries, and what a peer's advert is compared against."""

    def __init__(self, capacity=BUCKET_OBJECTS):
        self.members = pinsketch.Sketch(capacity)
        self.checksum = 0
        self.count = 0

    def add_object(self, object_id):
        self.add(short_id(object_id), checksum_contribution(object_id))

    def remove_object(self, object_id):
        self.remove(short_id(object_id), checksum_contribution(object_id))

    def add(self, id_, contribution):
        if not self.members.add(id_):
            return
        self.checksum ^= contribution
        self.count += 1

    def remove(self, id_, contribution):
        # add() is a toggle, so removal is the same operation.
        if self.count == 0 or not self.members.add(id_):
            return
        self.checksum ^= contribution
        self.count -= 1

    def sketch(self):
        return self.members

    def difference(self, peer):
        """The symmetric difference against a peer's sketch of the same bucket.

        None when the two differ by more than the capacity, which is the signal to escalate. A
        non-None return is not proof of anything until the objects arrive and the checksums match.
        """
        merged = self.members.copy()
        if not merged.merge(peer):
            return None
        return merged.decode()

    def clear(self):
        self.members.clear()
        self.checksum = 0
        self.count = 0
