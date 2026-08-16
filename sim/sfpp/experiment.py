"""Three SF++ stores, real packets, sketch reconciliation between them.

Each server is a real sqlite database on the firmware's schema. Messages arrive from the capture,
each server hears some of them, and the servers reconcile by exchanging bucket summaries. The run
measures what reconciliation costs and - the gate - whether anything is ever lost *silently*.

Silent loss is the only failure that matters. A decode that fails is fine: it escalates. A decode
that returns the wrong set is fine as long as the checksum refuses to close afterwards. Silent loss
is when both sides believe they agree and they do not, and the design's whole claim is that the
64-bit domain-separated checksum makes that impossible short of a deliberate collision.

Usage, from sim/:
    python3 -m sfpp.experiment --packetlog <packetlog.txt.gz> [--capacity 4] [--loss 0.15]
    python3 -m sfpp.experiment --synthetic 2000
"""

import argparse
import os
import random
import shutil
import tempfile
from dataclasses import dataclass, field

from . import feed
from .sketchindex import BUCKET_OBJECTS, bucket_of, checksum_contribution, short_id
from .store import SfppStore

# Wire sizes, from the frozen format. An advert is the envelope, the checksum and the sketch.
SR_ENVELOPE = 18
SR_CHECKSUM = 9
SHORT_ID = 4
OBJECT_OVERHEAD = 14  # SF++ framing around a moved object


@dataclass
class Counters:
    advert_bytes: int = 0
    request_bytes: int = 0
    provide_bytes: int = 0
    adverts: int = 0
    exchanges: int = 0
    decode_failures: int = 0
    misdecodes: int = 0
    objects_moved: int = 0
    checksum_closed: int = 0
    checksum_open: int = 0
    silent_losses: int = 0
    escalations: int = 0

    @property
    def total_bytes(self):
        return self.advert_bytes + self.request_bytes + self.provide_bytes


@dataclass
class Server:
    node_id: int
    store: SfppStore
    # Counter assignment is the chain's, not the receiver's: two nodes only summarise the same
    # bucket if they agree on the numbering. The simulator hands every server the same counter.
    held: dict = field(default_factory=dict)  # message_hash -> counter

    def ingest(self, message, counter):
        if self.store.insert(message, counter):
            self.held[message.message_hash] = counter
            return True
        return False

    def summary(self, root_hash, bucket, capacity):
        return self.store.build_bucket_summary(root_hash, bucket, capacity=capacity)


def truth_for(bucket, assigned):
    """Every object that belongs in a bucket, by the canonical numbering."""
    return {h for h, c in assigned.items() if bucket_of(c) == bucket}


def reconcile(a, b, root_hash, bucket, capacity, counters, assigned):
    """One push exchange: A adverts, B resolves what it can and answers.

    Push rather than request-by-id: the XOR of two sketches is the symmetric difference, and each
    side splits it by local membership. That removes ITEM_REQUEST from the common path and resolves
    B's shortfall in one round trip instead of two.
    """
    sa = a.summary(root_hash, bucket, capacity)
    sb = b.summary(root_hash, bucket, capacity)
    if sa is None and sb is None:
        return
    counters.exchanges += 1

    # A's advert goes out whether or not B holds anything for this bucket.
    counters.adverts += 1
    counters.advert_bytes += SR_ENVELOPE + SR_CHECKSUM + SHORT_ID * capacity

    if sa is None or sb is None:
        counters.escalations += 1
        return

    difference = sb.difference(sa.sketch())
    if difference is None:
        # Over capacity, or a sketch that is not decodable. Either way the answer is escalation,
        # never a guess - a capacity-c sketch misdecodes at about 1/c! and cannot detect it alone.
        counters.decode_failures += 1
        counters.escalations += 1
        return

    # What the sketch claims. Split it by local membership: what B holds and A lacks, B sends.
    b_has, a_has = [], []
    for sid in difference:
        if b.store.hash_for_short_id(sid):
            b_has.append(sid)
        else:
            a_has.append(sid)

    # B answers with the objects A is missing.
    moved = 0
    for sid in b_has:
        for message_hash in b.store.hash_for_short_id(sid):
            if not a.store.holds(message_hash):
                counter = assigned[message_hash]
                a.ingest(MESSAGES[message_hash], counter)
                counters.provide_bytes += (
                    MESSAGES[message_hash].wire_size + OBJECT_OVERHEAD
                )
                counters.objects_moved += 1
                moved += 1

    # B asks for what A holds and it lacks. In the escalation path this is a real ITEM_REQUEST.
    if a_has:
        counters.request_bytes += SR_ENVELOPE + SHORT_ID * len(a_has)
        for sid in a_has:
            for message_hash in a.store.hash_for_short_id(sid):
                if not b.store.holds(message_hash):
                    b.ingest(MESSAGES[message_hash], assigned[message_hash])
                    counters.provide_bytes += (
                        MESSAGES[message_hash].wire_size + OBJECT_OVERHEAD
                    )
                    counters.objects_moved += 1
                    moved += 1

    # Did it actually work? Re-summarise and compare checksums, then compare the true sets. The
    # pair of those two answers is the whole experiment.
    sa2 = a.summary(root_hash, bucket, capacity)
    sb2 = b.summary(root_hash, bucket, capacity)
    closed = sa2 is not None and sb2 is not None and sa2.checksum == sb2.checksum

    a_set = {h for h, c in a.held.items() if bucket_of(c) == bucket}
    b_set = {h for h, c in b.held.items() if bucket_of(c) == bucket}
    agree = a_set == b_set

    if closed:
        counters.checksum_closed += 1
        if not agree:
            # The failure the design exists to prevent: both sides believe they match, and do not.
            counters.silent_losses += 1
    else:
        counters.checksum_open += 1
        if not agree:
            counters.escalations += 1
        if difference and not agree and moved == 0:
            counters.misdecodes += 1


MESSAGES = {}


def run(
    messages,
    servers=3,
    capacity=4,
    loss=0.15,
    seed=20260816,
    cadence=8,
    db_dir=None,
    trace=None,
):
    """Feed every message to every server with independent loss, reconciling as buckets fill."""
    rng = random.Random(seed)
    temp = db_dir is None
    db_dir = db_dir or tempfile.mkdtemp(prefix="sfpp-sim-")

    nodes = [
        Server(node_id=i, store=SfppStore(os.path.join(db_dir, f"server{i}.db"), i))
        for i in range(servers)
    ]
    counters = Counters()
    assigned = {}
    root_hash = messages[0].root_hash
    next_counter = 0

    try:
        for index, message in enumerate(messages):
            if message.message_hash in assigned:
                continue  # the chain holds one copy; a rebroadcast is not a new object
            next_counter += 1
            assigned[message.message_hash] = next_counter
            MESSAGES[message.message_hash] = message

            for node in nodes:
                if rng.random() >= loss:
                    node.ingest(message, next_counter)

            # Reconcile every `cadence` objects, over the bucket that is currently filling and the
            # one before it - a node that missed the tail of a closed bucket still catches up.
            if next_counter % cadence == 0:
                bucket = bucket_of(next_counter)
                for b in {bucket, max(0, bucket - 1)}:
                    for i in range(len(nodes)):
                        for j in range(len(nodes)):
                            if i != j:
                                reconcile(
                                    nodes[i],
                                    nodes[j],
                                    root_hash,
                                    b,
                                    capacity,
                                    counters,
                                    assigned,
                                )

            # Sampled after any reconciliation, so the trace shows divergence growing between
            # exchanges and snapping back at one - the sawtooth is the thing worth looking at.
            if trace is not None:
                trace.append(
                    (next_counter, [len(n.held) for n in nodes], counters.total_bytes)
                )

        # Final state: how much of the chain does each server actually hold?
        held = [len(n.held) for n in nodes]
        total = len(assigned)
        missing = [total - h for h in held]
        return counters, total, held, missing
    finally:
        for n in nodes:
            n.store.close()
        if temp:
            shutil.rmtree(db_dir, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--packetlog")
    ap.add_argument("--synthetic", type=int, default=0)
    ap.add_argument("--capacity", type=int, default=4)
    ap.add_argument("--loss", type=float, default=0.15)
    ap.add_argument("--cadence", type=int, default=8)
    ap.add_argument("--servers", type=int, default=3)
    ap.add_argument("--limit", type=int)
    ap.add_argument(
        "--db-dir", help="keep the three databases here instead of a temp dir"
    )
    args = ap.parse_args()

    root_hash = bytes(range(16))
    if args.packetlog:
        messages = feed.load(args.packetlog, root_hash, limit=args.limit)
        source = f"{len(messages)} real text broadcasts"
    else:
        count = args.synthetic or 1000
        messages = feed.synthetic(count, root_hash)
        source = f"{count} synthetic messages"

    counters, total, held, missing = run(
        messages,
        servers=args.servers,
        capacity=args.capacity,
        loss=args.loss,
        cadence=args.cadence,
        db_dir=args.db_dir,
    )

    print(
        f"{source}, {total} distinct objects, {args.servers} servers, capacity {args.capacity}, loss {args.loss:.0%}"
    )
    print(f"  held per server      {held}  (missing {missing})")
    print(f"  exchanges            {counters.exchanges}")
    print(f"  adverts              {counters.adverts}  ({counters.advert_bytes} B)")
    print(
        f"  objects moved        {counters.objects_moved}  ({counters.provide_bytes} B)"
    )
    print(f"  requests             {counters.request_bytes} B")
    print(f"  total                {counters.total_bytes} B")
    print(f"  decode failures      {counters.decode_failures}")
    print(
        f"  misdecodes           {counters.misdecodes}  (decoded, wrong, caught by the checksum)"
    )
    print(f"  escalations          {counters.escalations}")
    print(f"  checksum closed/open {counters.checksum_closed}/{counters.checksum_open}")
    print(f"  SILENT LOSSES        {counters.silent_losses}")
    return 1 if counters.silent_losses else 0


if __name__ == "__main__":
    raise SystemExit(main())
