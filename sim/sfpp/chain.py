"""Today's SF++: a broadcast chain tip, and a serialised walk backwards to catch up.

The incumbent, and the thing set reconciliation exists to replace: a difference of `d` objects takes
`d` round trips here against one exchange for a sketch. This module exists so that comparison has a
measured cost on both sides.

The protocol, as StoreForwardPlusPlus.cpp implements it:

  CANON_ANNOUNCE   broadcast, carrying this node's chain tip - a commit hash and a counter. Cheap:
                   a hash and an integer, nothing that scales with divergence.
  LINK_REQUEST     addressed, naming the commit this node wants. One link.
  LINK_PROVIDE     the link, carrying the object plus its parent commit hash.

A node hearing a tip ahead of its own walks backwards: request the tip, learn its parent, request
that, and so on until it reaches a commit it already holds. Each step is a round trip, and the walk
cannot be pipelined because the parent hash only arrives with the child - that serialisation is the
whole cost, and it is what a sketch replaces with one exchange.

`canon_scratch` in the firmware holds links received out of order until the gap closes; the store's
DDL already carries the table. Modelled here as a pending set, since the ordering rule
(`lo.counter == chain_end.counter + 1`) is what decides when a scratch link can be committed.
"""

from .sketchindex import (  # noqa: F401  (parity with the SR path)
    checksum_contribution,
    short_id,
)

# Wire sizes. A tip is small on purpose; the cost of this protocol is round trips, not bytes.
ANNOUNCE_BYTES = 2 + 6 + 16 + 4  # type + scope + commit hash + counter
LINK_REQUEST_BYTES = 2 + 6 + 16  # type + scope + the commit being asked for
LINK_OVERHEAD = 2 + 6 + 16 + 16 + 4  # type + scope + commit + parent + counter


class ChainServer:
    """One node's canon chain: an ordered list of commits and the objects behind them.

    The chain is per-server and its counters are local, exactly as in the SR work - there is no
    official counter here either. What differs is that a chain also carries a *parent link* per
    object, so order is recoverable by walking, which is precisely what makes catch-up serial.
    """

    def __init__(self, index, store):
        self.index = index
        self.store = store
        self.held = {}  # message_hash -> counter
        self.parent = (
            {}
        )  # message_hash -> the hash committed before it, or None at the root
        self.tip = None
        self.counter = 0
        self.pending = (
            {}
        )  # message_hash -> object, received out of order, awaiting its gap
        self.walking = (
            {}
        )  # peer -> the commit currently being requested, so walks do not overlap

    def commit(self, message_hash):
        """Append to the local chain. Returns the counter assigned, or None if already held."""
        if message_hash in self.held:
            return None
        self.counter += 1
        self.held[message_hash] = self.counter
        self.parent[message_hash] = self.tip
        self.tip = message_hash
        return self.counter

    def holds(self, message_hash):
        return message_hash in self.held


class ChainProtocol:
    """The chain half of a campaign: announce a tip, walk to close a gap.

    Deliberately shares the campaign's transport, store and counters so the comparison against SR is
    like for like - same mesh, same traffic, same airtime accounting, same silent-loss gate.
    """

    def __init__(self, campaign):
        self.c = campaign
        self.servers = {}

    def attach(self, index, store):
        self.servers[index] = ChainServer(index, store)
        return self.servers[index]

    # ---- ingest -----------------------------------------------------------------------

    def on_text(self, server, message_hash):
        """A text heard off the air goes straight onto the local chain."""
        counter = server.commit(message_hash)
        if counter is None:
            return
        obj = self.c.generator.objects[message_hash]
        server.store.insert(obj, counter)

    # ---- announce ---------------------------------------------------------------------

    def announce(self, server):
        """Broadcast the local tip. Cheap, and the only unsolicited message in the protocol."""
        if server.tip is None:
            return
        self.c.counters.adverts += 1
        self.c.counters.advert_bytes += ANNOUNCE_BYTES
        self.c._sr_send(
            server.index,
            "chain:announce",
            {
                "src": server.index,
                "dst": None,
                "tip": server.tip,
                "counter": server.counter,
            },
            ANNOUNCE_BYTES,
        )

    def on_announce(self, server, payload):
        """Heard a tip. If it is one we do not hold, start walking backwards from it."""
        self.c.counters.exchanges += 1
        self.c.counters.adverts_heard += 1
        tip = payload["tip"]
        peer = payload["src"]
        if server.holds(tip) or server.walking.get(peer):
            # Already caught up with this peer, or a walk is already in flight. The firmware's
            # single-walk-at-a-time behaviour matters to the cost: two overlapping walks would
            # double the round trips without halving the latency.
            return
        self._request(server, peer, tip)

    # ---- the walk ---------------------------------------------------------------------

    def _request(self, server, peer, commit):
        server.walking[peer] = commit
        self.c.counters.item_requests += 1
        self.c.counters.item_request_bytes += LINK_REQUEST_BYTES
        self.c.counters.chain_round_trips += 1
        self.c._sr_send(
            server.index,
            "chain:link_request",
            {"src": server.index, "dst": peer, "commit": commit},
            LINK_REQUEST_BYTES,
            dst=peer,
        )

    def on_link_request(self, server, payload):
        """Answer with the named link and its parent, so the requester can take one more step."""
        commit = payload["commit"]
        if not server.holds(commit):
            return
        obj = self.c.generator.objects[commit]
        length = min(233, obj.wire_size + LINK_OVERHEAD)
        self.c.counters.provides += 1
        self.c.counters.provide_bytes += length
        self.c._sr_send(
            server.index,
            "chain:link_provide",
            {
                "src": server.index,
                "dst": payload["src"],
                "commit": commit,
                "parent": server.parent.get(commit),
                # Same replay header as the SR path: a bystander can file an overheard link too.
                "hash": commit,
                "heard_ago_s": 0,
            },
            length,
            dst=payload["src"],
        )

    def on_link_provide(self, server, payload):
        """Take the link, then step to its parent - one more round trip, and so on."""
        commit = payload["commit"]
        peer = payload["src"]
        server.walking.pop(peer, None)

        if not server.holds(commit):
            counter = server.commit(commit)
            if counter is not None:
                server.store.insert(self.c.generator.objects[commit], counter)
                self.c.counters.objects_moved += 1

        parent = payload.get("parent")
        if parent is None or server.holds(parent):
            # Reached a commit we already have: the chains have met and the walk is done.
            self.c.counters.chain_walks_completed += 1
            return
        if self.c.counters.chain_round_trips > self.c.opts.chain_walk_cap * len(
            self.c.generator.text_order or [1]
        ):
            # A runaway walk is a real failure mode, not something to hide behind an infinite loop.
            self.c.counters.chain_walks_abandoned += 1
            return
        self._request(server, peer, parent)
