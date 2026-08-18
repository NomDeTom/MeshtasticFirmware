"""SF++ set reconciliation over a real mesh: server placement, the protocol, and what it costs.

Every message the protocol sends is a packet on the transport in mesh.py: an advert contends for the
channel, is relayed by nodes that gain nothing from it, and is lost when it collides. What this
measures is what the protocol costs and where the servers should go.

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

from . import autochart as AC
from . import report as R
from . import chain as CH
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

        The argument for it: such a server hears most of what the router hears without competing
        with it for the channel.
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
        "chain_round_trips",
        "chain_walks_completed",
        "chain_walks_abandoned",
        "dm_via_transport",
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

    Bucket membership is per-server. The firmware assigns a chain counter as `chain_end.counter + 1`
    when it ingests a message that arrived without an official one (StoreForwardPlusPlus.cpp:1366),
    so two servers hearing the same broadcast off the air number it differently. `SketchIndex.h`
    describes a count boundary as one both sides derive from the data itself; it is derived from
    local arrival order instead, which is what the bucket-mode arm measures.
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

        A sliding window needs no agreement. Two servers' windows overlap because they heard mostly
        the same recent traffic, not because they negotiated a boundary: the XOR of two sketches is
        the symmetric difference of whatever sets they were built over.
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
            hop_assign=opts.hop_assign,
            topology=opts.topology,
            profile=_profile_for(opts),
            old_profile=getattr(opts, "old_profile", "legacy"),
            legacy_fraction=getattr(opts, "legacy_fraction", 0.0),
            role_mix=getattr(opts, "role_mix", None) or None,
            router_late_fraction=getattr(opts, "router_late_fraction", 0.0),
            client_base_fraction=getattr(opts, "client_base_fraction", 0.0),
            favourite_routers=getattr(opts, "favourite_routers", False),
            rebroadcast_mode=getattr(opts, "rebroadcast_mode", M.REBROADCAST_ALL),
            max_num_nodes=_hot_store_size(opts),
            warm_num_nodes=getattr(opts, "warm_num_nodes", None),
            signature_policy=getattr(
                opts, "signature_policy", M.SIGNATURE_POLICY_COMPATIBLE
            ),
            platform_mix=getattr(opts, "platform_mix", "uniform"),
            siting_mix=getattr(opts, "siting_mix", "uniform"),
        )
        self.root_hash = bytes(range(16))
        self.generator = T.Generator(
            self.mesh,
            self.rng,
            self.root_hash,
            mix=MIX,
            congestion_scaling=not opts.no_congestion_scaling,
            congestion_mode=getattr(opts, "congestion_mode", "adaptive"),
            online_cap=opts.max_num_nodes,
            congestion_input=opts.congestion_input,
            broadcast_interval_s=opts.broadcast_interval_s,
            diurnal=opts.diurnal,
            start_hour=opts.start_hour,
            position_throttle=opts.position_throttle,
            telemetry_throttle=opts.telemetry_throttle,
        )
        self.counters = Counters()
        # Which route an addressed SR message takes. `hop-by-hop` is the fiction every published
        # chain cost was measured under; `transport` is the real one.
        self.dm_transport = getattr(opts, "dm_transport", "hop-by-hop")
        self.duration_ms = opts.hours * 3600_000.0

        self.catch_up = None
        if opts.catch_up_hours:
            start, end = opts.catch_up_hours.split("-")
            self.catch_up = (float(start), float(end))
        self.counter_of = {}  # message_hash -> canonical chain counter
        self._counted = 0
        self.heard_text = {i: set() for i in range(opts.nodes)}
        # Every class, not just the archived one. Position, telemetry and nodeinfo are generated,
        # flooded and charged airtime, so any airtime share quoted against them needs their
        # receptions measured too.
        self.heard_by_class = {}
        self.hop_stats = {}
        # Per node, how many hops each text it received had actually travelled. The firmware keeps the
        # same quantity per peer as NodeInfoLite.hops_away, so this is the simulator's view of the field
        # rather than an invented metric - and a histogram rather than a mean, because the interesting
        # nodes are the ones whose traffic all arrives at 4+ hops.
        self.hops_away_hist = {i: {} for i in range(opts.nodes)}
        self.servers = {}
        self.db_dir = tempfile.mkdtemp(prefix="sfpp-campaign-")
        self.bucket_closed_at = {}
        self.width = opts.short_id_bits

        # Intermediate nodes wired for telemetry. They run no archive and change no behaviour; they
        # only record what an ordinary node in the middle of the mesh actually ends up with, split by
        # how it got there. Without this the bystander benefit of a broadcast replay is invisible.
        self.observers = {}
        # `none` is the paired baseline: same seed, same topology, same traffic schedule, no archive.
        # Making it a cell of the protocol arm rather than a separate run turns every other cell into
        # a difference instead of a comparison.
        self.chain = CH.ChainProtocol(self) if opts.protocol == "chain" else None
        # The same nodes are chosen whatever the protocol, including `none`. Under `none` they run no
        # archive and behave as ordinary nodes - which is the control that separates two things this
        # campaign had been conflating: what being a server *costs* a node in its own reception
        # (a server transmits more, so contention and half duplex charge it), and what
        # reconciliation then *adds* on top. Without it, "held 0.966" could not be split into
        # "heard anyway" and "recovered".
        self.designated = []
        if not opts.baseline:
            self._place_servers(archive=opts.protocol != "none")
            self._place_observers()
        self.mesh.on_receive = self._on_receive

    # ---- setup ------------------------------------------------------------------------

    def _place_servers(self, archive=True):
        """Choose the archive positions. With `archive=False` they are marked and instrumented but
        run nothing, so the same nodes in the same places can be measured as ordinary nodes.
        """
        strategy = Placement.BY_NAME[self.opts.place]
        indexes = strategy(self.mesh, self.opts.servers, self.rng, self.opts.hops_apart)
        self.designated = sorted(indexes)
        if not archive:
            return
        for i in indexes:
            self.mesh.nodes[i].is_server = True
            store = SfppStore(os.path.join(self.db_dir, f"s{i}.db"), i)
            self.servers[i] = Server(i, store, self.opts)
            if self.chain is not None:
                self.chain.attach(i, store)

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

    def _note_class_reception(self, node, packet):
        kind = packet.kind or str(packet.portnum)
        seen = self.heard_by_class.setdefault(kind, {})
        key = (node.index, packet.id)
        if key in seen:
            return
        seen[key] = True
        # Split by the receiver's own hop limit, so "does turning it up help me" is answerable
        # separately from "does it help everyone else".
        limit = self.mesh.hop_limit_for(node.index)
        stats = self.hop_stats.setdefault(
            limit, {"nodes": set(), "received": 0, "hops": []}
        )
        stats["nodes"].add(node.index)
        stats["received"] += 1
        hops = packet.hops_taken()
        stats["hops"].append(hops)
        if packet.kind == "text":
            h = self.hops_away_hist[node.index]
            h[hops] = h.get(hops, 0) + 1

    def _on_receive(self, node, packet, rssi, snr):
        self._note_class_reception(node, packet)
        if (
            self.dm_transport == "transport"
            and packet.destination != M.BROADCAST
            and node.index != packet.destination
        ):
            # A relay on the path, not the addressee. It carried the packet; it does not read it.
            return
        if packet.kind == "text":
            self._on_text(node, packet)
        elif packet.kind == "sr:item_provide" and packet.payload is not None:
            # A replayed object is useful to whoever hears it, not only to whoever asked. A server
            # files it in its store and any other node records it for its own history, so both paths
            # run: returning here instead would stop servers ingesting broadcast replays.
            if node.index in self.servers:
                self._on_sr(node, packet)
            else:
                self._on_overheard_replay(node, packet)
        elif packet.kind and packet.kind.startswith("chain:"):
            if node.index in self.servers:
                self._on_sr(node, packet)
        elif packet.kind and packet.kind.startswith("sr:"):
            self._on_sr(node, packet)

    def _global_counter(self, message_hash):
        """Origination order across the whole mesh. A FICTION, kept only as an upper bound.

        No canonical counter exists. StoreForwardPlusPlus.cpp:1364 reads "if we get an official
        counter, use it. Otherwise, just increment", and there is no official counter to get: every
        counter is a local increment off the local chain tip. This mode therefore describes a mesh
        that cannot be built, and exists only to bound what bucket agreement would be worth.
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
        if self.chain is not None:
            cs = self.chain.servers[node.index]
            self.chain.on_text(cs, message_hash)
            server.held = cs.held
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
        """An addressed SR message, by one of two routes.

        `transport` hands it to the transport as a real DM: NextHopRouter picks the next hop from
        what the sender has actually learned, falls back to flooding when it has learned nothing,
        and runs the retry ladder. Costs are then whatever routing really costs, including being
        wrong.

        `hop-by-hop` walks a precomputed shortest path outside the transport, one addressed hop at a
        time with a hand-written delay and no contention for the route decision itself. Every
        published chain-arm cost was measured this way, so it stays the default until those numbers
        are re-measured.
        """
        if self.dm_transport == "transport":
            packet = self.mesh.originate(
                src,
                STORE_FORWARD_PLUSPLUS_APP,
                length,
                kind=kind,
                payload=payload,
                destination=dst,
                want_ack=True,
            )
            if packet is not None:
                self.counters.dm_via_transport += 1
            return
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
                if attempt < self.opts.sr_retries:
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
        if kind.startswith("chain:"):
            cs = self.chain.servers[node.index]
            {
                "chain:announce": self.chain.on_announce,
                "chain:link_request": self.chain.on_link_request,
                "chain:link_provide": self.chain.on_link_provide,
            }[kind](cs, payload)
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
            # Both windows are byte-identical: the only case in which the bucket design's safety
            # mechanism can still fire, so it is counted.
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

        The in-flight check can only judge the exchanges that happened. This judges the end state,
        where nothing is in flight and no snapshot is stale, so a disagreement here is unambiguous.
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

    def _chain_tick(self, server):
        """The chain has no bucket to seal, so its cadence is a timer. A tip is ~28 bytes."""
        if self.mesh.now > self.duration_ms:
            return
        if self._in_catch_up_window():
            self.chain.announce(self.chain.servers[server.index])
        else:
            self.counters.adverts_deferred += 1
        delay = server.interval_ms * self.rng.uniform(0.8, 1.2)
        self.mesh.at(self.mesh.now + delay, lambda: self._chain_tick(server))

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

    def _schedule_traceroutes(self):
        """Route discovery on a Poisson timer per node, to whoever it has heard of.

        A traceroute is what seeds next-hop routing on a mesh that has not been talking: its reply
        teaches a route for every node beyond the learner, where an ACK teaches one hop. It is not
        free - the request grows five bytes per hop it records - so the rate is a swept parameter.
        """
        rate = getattr(self.opts, "traceroute_per_hour", 0.0)
        if rate <= 0:
            return
        count = self.opts.nodes
        mean_gap_ms = 3600_000.0 / rate
        for node in range(count):
            when = self.rng.expovariate(1.0 / mean_gap_ms)
            while when < self.duration_ms:

                def probe(src=node, at=when):
                    known = [
                        peer
                        for peer in self.mesh.nodes[src].nodedb
                        if peer != src and self.mesh.nodes[peer].online
                    ]
                    if known:
                        self.mesh.send_traceroute(src, self.rng.choice(known))

                self.mesh.at(when, probe)
                when += self.rng.expovariate(1.0 / mean_gap_ms)

    def run(self):
        started = time.time()
        self.generator.schedule(self.duration_ms)
        self._schedule_traceroutes()
        self.mesh.start_hop_scaling()
        if getattr(self.opts, "trace_interval_s", 0):
            self.mesh.start_adaptive_trace(
                interval_ms=self.opts.trace_interval_s * 1000.0,
                generator=self.generator,
            )
        if self.chain is not None:
            for server in self.servers.values():
                start = self.rng.uniform(0, server.interval_ms)
                self.mesh.at(start, lambda s=server: self._chain_tick(s))
        elif not self.opts.baseline and self.opts.trigger in (
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
                "routers_late": sum(
                    1 for n in self.mesh.nodes if n.role == M.ROUTER_LATE
                ),
                "client_base": sum(
                    1 for n in self.mesh.nodes if n.role == M.CLIENT_BASE
                ),
                # Which firmware's rules produced this row. Without it a JSON from before the 2.8
                # fold-in is indistinguishable from one after it.
                "firmware_profile": self.mesh.profile.name,
                "topology": getattr(self.mesh, "topology", "uniform"),
                "diameter": self.mesh.diameter(),
            },
            "by_class": self._class_report(),
            "by_hop_limit": self._hop_report(),
            "hops_away": self._hops_away_report(),
            "hop_scaling": self._hop_scaling_report(),
            "adaptive": self._adaptive_report(),
            "traffic": {
                "originated": dict(self.generator.originated),
                "congestion_coefficient": round(self.generator.congestion, 3),
                "congestion_input": self.generator.congestion_input,
                # What the mesh can see of its own size, unbounded by any hot store. The gap
                # between this and the coefficient's input is the saturation round four prices.
                "observed_senders": T.observed_senders(self.mesh),
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

        if self.designated and total:
            direct = [len(self.heard_text[i]) / total for i in self.designated]
            others = [
                len(self.heard_text[i]) / total
                for i in range(self.opts.nodes)
                if i not in self.designated
            ]
            report["designated"] = {
                "nodes": self.designated,
                "running_archive": bool(self.servers),
                # What these nodes heard off the air themselves. Under --protocol none this is the
                # control: the same nodes, same places, no archive.
                "direct_reception_mean": round(statistics.mean(direct), 4),
                "direct_reception_each": [round(x, 4) for x in direct],
                "rest_of_mesh_reception_mean": (
                    round(statistics.mean(others), 4) if others else 0
                ),
                "rest_of_mesh_reception": self._dist(others),
            }
            if self.servers:
                held = [len(s.held) / total for s in self.servers.values()]
                report["designated"]["held_mean"] = round(statistics.mean(held), 4)
                report["designated"]["reconciled_gain_mean"] = round(
                    statistics.mean(held) - statistics.mean(direct), 4
                )

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
                        if str(k).startswith(("sr:", "chain:"))
                    ),
                    1,
                ),
                "sr_airtime_share": round(
                    sum(
                        v
                        for k, v in self.mesh.airtime_by_kind.items()
                        if str(k).startswith(("sr:", "chain:"))
                    )
                    / max(1.0, self.mesh.stats["airtime_ms"]),
                    4,
                ),
                **self.counters.as_dict(),
                "audit_checksum_agrees_sets_differ": self.final_audit_failures,
                **self._drift_report(),
                **self._stretch_report(total),
            }
        return report

    @staticmethod
    def _dist(values):
        """Min, p10, median, mean, p90, max. A mean alone hides the tail, and on a stretched mesh the
        worst-served node is the one the archive exists for - so it gets reported, not averaged away.
        """
        if not values:
            return {}
        v = sorted(values)
        pick = lambda q: v[min(len(v) - 1, int(q * (len(v) - 1)))]  # noqa: E731
        return {
            "min": round(v[0], 4),
            "p10": round(pick(0.10), 4),
            "median": round(statistics.median(v), 4),
            "mean": round(statistics.mean(v), 4),
            "p90": round(pick(0.90), 4),
            "max": round(v[-1], 4),
        }

    def _stretch_report(self, total):
        """On a mesh wider than any hop limit, what could only ever arrive via an archive?

        This is the measurement a stretched mesh exists to make. For each node, three quantities:

          heard          - it received the message off the air
          unreachable    - no sender-to-node path within the *sender's* hop limit exists, so no amount
                           of retry or luck would ever have delivered it
          recoverable    - unreachable, but held by an archive that node can itself reach

        `recoverable` is the addressable value of the design: text that is structurally impossible to
        receive directly and is nonetheless sitting on a server within reach. On a mesh narrower than
        the hop limit it is near zero by construction, which is why every earlier run understated the
        case for an archive.
        """
        if not total or not self.servers:
            return {}
        n = self.opts.nodes
        # Who can reach whom, bounded by the sender's own hop limit.
        reach = {}
        for sender in range(n):
            depth = self.mesh.hops_from([sender])
            limit = self.mesh.hop_limit_for(sender)
            reach[sender] = {t for t, h in depth.items() if 0 < h <= limit}

        server_holds = {i: set(sv.held) for i, sv in self.servers.items()}
        recoverable, unreachable, delivered = [], [], []
        for i in range(n):
            if i in self.servers:
                continue
            # Servers this node could query: within its own hop limit.
            depth_i = self.mesh.hops_from([i])
            limit_i = self.mesh.hop_limit_for(i)
            mine = [s for s in self.servers if 0 < depth_i.get(s, 999) <= limit_i]
            in_reach = set()
            for s in mine:
                in_reach |= server_holds[s]
            unreach = 0
            rec = 0
            got = 0
            held_here = self.heard_text[i]
            for h in self.generator.objects:
                sender = self.generator.objects[h].sender
                if i in reach.get(sender, ()):
                    continue  # was reachable directly
                unreach += 1
                if h in in_reach:
                    rec += 1
                if h in held_here:
                    # Proof of archive-delivered coverage. No path within the sender's hop limit
                    # reaches this node, so no retry, no luck and no rebroadcast would ever have
                    # delivered it - and the node has it. It can only have arrived as a replay.
                    got += 1
            unreachable.append(unreach / total)
            recoverable.append(rec / total)
            delivered.append(got / total)
        if not unreachable:
            return {}
        # Per-node share of that node's own unreachable text which actually arrived. The worst node
        # here is the honest headline: the mean is dragged up by nodes that had little to recover.
        per_node_share = [
            (delivered[k] / unreachable[k]) if unreachable[k] > 0 else None
            for k in range(len(unreachable))
        ]
        share_vals = [x for x in per_node_share if x is not None]
        return {
            "structurally_unreachable": self._dist(unreachable),
            "recoverable_from_reachable_archive": self._dist(recoverable),
            "delivered_though_unreachable": self._dist(delivered),
            "per_node_share_of_unreachable_delivered": self._dist(share_vals),
            "nodes_with_zero_delivered": sum(
                1
                for k in range(len(delivered))
                if unreachable[k] > 0 and delivered[k] == 0
            ),
            "nodes_measured": len(unreachable),
            "structurally_unreachable_mean": round(statistics.mean(unreachable), 4),
            "recoverable_from_reachable_archive_mean": round(
                statistics.mean(recoverable), 4
            ),
            "share_of_unreachable_recoverable": round(
                statistics.mean(recoverable) / max(1e-9, statistics.mean(unreachable)),
                4,
            ),
            # The one unambiguous figure: text this node holds that nothing but a replay could have
            # brought it. Not an upper bound, not an inference from what a server holds - delivered.
            "delivered_though_unreachable_mean": round(statistics.mean(delivered), 4),
            "share_of_unreachable_delivered": round(
                statistics.mean(delivered) / max(1e-9, statistics.mean(unreachable)), 4
            ),
        }

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

    def _class_report(self):
        """Sent against heard, per class, with per-node distributions and airtime actually spent.

        Distributions rather than means because a mean receiver-count hides the node that hears almost
        nothing, and that node is the one every archive argument is about. `text` is the archived class
        and is the one to read first; the rest are here because they set the contention text competes
        with, and because a per-class table is the only way to see whether SF++ displaces text or
        telemetry.
        """
        out = {}
        for name, sent in self.generator.originated.items():
            seen = self.heard_by_class.get(name, {})
            receipts = len(seen)
            per_node = [0] * self.opts.nodes
            for node_index, _pid in seen:
                per_node[node_index] += 1
            shares = [c / sent for c in per_node] if sent else []
            air = self.mesh.airtime_by_kind.get(name, 0.0)
            out[name] = {
                "originated": sent,
                "receptions": receipts,
                # Mean nodes that heard each originated packet - the useful figure, since a broadcast
                # has many intended receivers rather than one.
                "mean_receivers_per_packet": round(receipts / sent, 2) if sent else 0,
                "reception_rate": (
                    round(receipts / (sent * max(1, self.opts.nodes - 1)), 4)
                    if sent
                    else 0
                ),
                "airtime_s": round(air / 1000.0, 1),
                "airtime_share": round(
                    air / max(1.0, self.mesh.stats["airtime_ms"]), 4
                ),
                # Per-node share of this class each node received, as a distribution. A mean hides the
                # node that heard almost none of it, which is the node the archive exists for.
                "per_node_reception": self._dist(shares),
                "nodes_receiving_none": sum(1 for c in per_node if c == 0),
                "archived": name == "text",
            }
        # One more row across every class, because a per-class table answers "did text get through"
        # but not "did this node hear the mesh at all". A node can sit at a healthy text share while
        # missing most of the position and telemetry around it, and an arm that trades one for the
        # other is invisible in any single class.
        total_sent = sum(self.generator.originated.values())
        if total_sent:
            per_node = [0] * self.opts.nodes
            for name in self.generator.originated:
                for node_index, _pid in self.heard_by_class.get(name, {}):
                    per_node[node_index] += 1
            shares = [c / total_sent for c in per_node]
            out["all"] = {
                "originated": total_sent,
                "receptions": sum(
                    len(self.heard_by_class.get(n, {})) for n in self.generator.originated
                ),
                "mean_receivers_per_packet": round(
                    sum(len(self.heard_by_class.get(n, {})) for n in self.generator.originated)
                    / total_sent,
                    2,
                ),
                "reception_rate": round(
                    sum(len(self.heard_by_class.get(n, {})) for n in self.generator.originated)
                    / (total_sent * max(1, self.opts.nodes - 1)),
                    4,
                ),
                "airtime_s": round(self.mesh.stats["airtime_ms"] / 1000.0, 1),
                "airtime_share": 1.0,
                "per_node_reception": self._dist(shares),
                "nodes_receiving_none": sum(1 for c in per_node if c == 0),
                "archived": False,
            }
        return out

    def _hops_away_report(self):
        """Per-node histogram of how far the text it received had travelled, plus the topology's own.

        Two distinct things, and conflating them is easy:

          observed  - hops actually traversed by text this node received. What NodeInfoLite.hops_away
                      records, and what a client would display.
          topology  - shortest-path distance to every other node, whether or not anything arrived.
                      The bound the observed histogram is drawn from.

        A node whose observed histogram is empty above 2 hops while its topological one runs to 6 is
        not well connected - it is deaf beyond 2 hops, and that is the node an archive is for.
        """
        observed, topo = {}, {}
        for i in range(self.opts.nodes):
            h = self.hops_away_hist.get(i) or {}
            if h:
                total = sum(h.values())
                observed[str(i)] = {
                    "counts": {str(k): v for k, v in sorted(h.items())},
                    "mean_hops": round(sum(k * v for k, v in h.items()) / total, 2),
                    "max_hops": max(h),
                }
            depth = self.mesh.hops_from([i])
            d = {}
            for target, hops in depth.items():
                if hops > 0:
                    d[hops] = d.get(hops, 0) + 1
            if d:
                topo[str(i)] = {str(k): v for k, v in sorted(d.items())}

        # Mesh-wide rollups, so a sweep has something scalar to compare without unpacking 60 nodes.
        agg = {}
        for h in self.hops_away_hist.values():
            for k, v in h.items():
                agg[k] = agg.get(k, 0) + v
        total = sum(agg.values()) or 1
        return {
            "observed_per_node": observed,
            "topology_per_node": topo,
            "mesh_observed_counts": {str(k): v for k, v in sorted(agg.items())},
            "mesh_observed_share": {
                str(k): round(v / total, 4) for k, v in sorted(agg.items())
            },
            "mesh_mean_hops": round(sum(k * v for k, v in agg.items()) / total, 2),
        }

    def _hop_report(self):
        """Reception and traversal split by the node's own configured hop limit."""
        out = {}
        for limit, st in sorted(self.hop_stats.items()):
            count = len(st["nodes"])
            out[str(limit)] = {
                "nodes": count,
                "receptions_per_node": round(st["received"] / count, 1) if count else 0,
                "mean_hops_traversed": (
                    round(sum(st["hops"]) / len(st["hops"]), 2) if st["hops"] else 0
                ),
            }
        return out

    def _adaptive_report(self):
        """What the adaptive quantities did over time, not just where they ended.

        `settled` is the share of nodes whose hop recommendation stopped changing over the second
        half of the run; `reversals` counts how often it changed direction. A converged mesh and an
        oscillating one have the same mean, and differ here.
        """
        trace = self.mesh.adaptive_trace
        if not trace:
            return {"samples": 0}
        by_node = {}
        for row in trace:
            by_node.setdefault(row["node"], []).append(row)
        settled, reversals, ranges = 0, 0, []
        for rows in by_node.values():
            series = [r["required_hop"] for r in rows]
            half = series[len(series) // 2 :]
            if half and len(set(half)) == 1:
                settled += 1
            ranges.append(max(series) - min(series))
            direction = 0
            for before, after in zip(series, series[1:]):
                step = (after > before) - (after < before)
                if step and direction and step != direction:
                    reversals += 1
                if step:
                    direction = step
        return {
            "samples": len(trace),
            "interval_s": self.opts.trace_interval_s,
            "nodes": len(by_node),
            "settled_share": round(settled / len(by_node), 3),
            "reversals": reversals,
            "hop_range_mean": round(statistics.mean(ranges), 3),
            "hop_range_max": max(ranges),
            "series": trace,
        }

    def _hop_scaling_report(self):
        """Truth, observation and estimate for the mesh, averaged over the nodes that have rolled.

        The three differ by construction. `truth` is the topological distance nothing on a device
        can see; `observed` is every hop count the node actually heard, exhaustive; `estimated` is
        what HopScalingModule would report after sampling, capping at 128 entries, colliding hashes
        and scaling the survivors back up. The gap between the last two is what the estimator costs.
        """
        reports = [self.mesh.hop_report(i) for i in range(self.opts.nodes)]
        rolled = [r for r in reports if r.get("rolls")]
        if not rolled:
            return {"nodes_rolled": 0}
        hops = M.HopScaling.MAX_HOP + 1
        mean = lambda key: [
            round(statistics.mean(r[key][h] for r in rolled), 2) for h in range(hops)
        ]
        suggested = [r["suggested_hop"] for r in rolled]
        return {
            "nodes_rolled": len(rolled),
            "truth_per_hop": mean("truth"),
            "observed_per_hop": mean("observed"),
            "estimated_per_hop": mean("estimated"),
            "truth_total_mean": round(
                statistics.mean(r["truth_total"] for r in rolled), 2
            ),
            "observed_total_mean": round(
                statistics.mean(r["observed_total"] for r in rolled), 2
            ),
            "estimated_total_mean": round(
                statistics.mean(r["estimated_total"] for r in rolled), 2
            ),
            "entries_mean": round(statistics.mean(r["entries"] for r in rolled), 2),
            "sampling_denominator_max": max(
                r["sampling_denominator"] for r in rolled
            ),
            "suggested_hop_mean": round(statistics.mean(suggested), 2),
            "suggested_hop_spread": [min(suggested), max(suggested)],
            "dropped_full": sum(r["dropped_full"] for r in rolled),
        }

    def _reach_ceiling(self):
        """The best a node could do: the share of senders whose packets can physically reach it.

        A message that originated five hops away under a hop limit of three was never going to
        arrive, and counting it as a loss would blame the radio for the routing.

        The bound is the sender's own hop limit, taken per sender. Under `--hop-spread` every node
        has its own limit of 3 to 7, so a single global value would compute a ceiling below the
        reception actually measured. `reach_ceiling_mean`, `missed_beyond_hop_limit` and
        `missed_within_reach` all derive from this; measured reception does not.
        """
        out = []
        n = self.opts.nodes
        reachable_from = {}
        for sender in range(n):
            depth = self.mesh.hops_from([sender])
            limit = self.mesh.hop_limit_for(sender)
            reachable_from[sender] = {
                target for target, hops in depth.items() if 0 < hops <= limit
            }
        for i in range(n):
            within = sum(1 for j in range(n) if j != i and i in reachable_from[j])
            out.append(within / (n - 1))
        return out

    def close(self):
        for server in self.servers.values():
            server.store.close()
        shutil.rmtree(self.db_dir, ignore_errors=True)


def _profile_for(opts):
    """The rule set, with the branch-only and compiled-out mechanisms the flags asked for.

    --profile-flag stays alongside them so a specific pre-2.5 pathology can be simulated on its own
    without pretending a whole-version reconstruction exists.
    """
    name = getattr(opts, "profile", "2.8")
    dm_mode = getattr(opts, "dm_mode", "directed-with-late-flood")
    overrides = {}
    for item in getattr(opts, "profile_flag", []) or []:
        key, _, raw = item.partition("=")
        val = raw.strip().lower()
        if val in ("true", "1", "yes", "on"):
            parsed = True
        elif val in ("false", "0", "no", "off"):
            parsed = False
        elif val in ("none", ""):
            parsed = None
        else:
            parsed = float(val) if "." in val else int(val)
        overrides[key.strip()] = parsed
    if getattr(opts, "extra_repeats", False):
        overrides["extra_repeats"] = True
    if getattr(opts, "coding_rate_ladder", False):
        overrides["coding_rate_ladder"] = True
    if getattr(opts, "no_adopt_hop_recommendation", False):
        overrides["adopt_hop_recommendation"] = False
    if dm_mode == "flood-only":
        overrides["next_hop_routing"] = False
    elif dm_mode == "m4-early-flood":
        overrides["early_flood_on_unverified"] = True
    return M.Profile(name, **overrides) if overrides else name


def _hot_store_size(opts):
    """The hot-store cap to hand every node, or None to let the platform mix decide.

    --max-num-nodes and MAX_NUM_NODES are the same constant, so one flag drives both the congestion
    input and the store. A platform mix asks for nodes that differ, so it overrides the flag.
    """
    if getattr(opts, "platform_mix", "uniform") != "uniform":
        return None
    return getattr(opts, "max_num_nodes", None)


def build_parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=72.0)
    ap.add_argument("--nodes", type=int, default=60)
    ap.add_argument("--area", type=float, default=8000.0)
    ap.add_argument("--hop-limit", type=int, default=3)
    ap.add_argument("--router-fraction", type=float, default=0.1)
    ap.add_argument("--router-late-fraction", type=float, default=0.0)
    ap.add_argument("--client-base-fraction", type=float, default=0.0)
    ap.add_argument(
        "--favourite-routers",
        action="store_true",
        help="router-like nodes favourite each other, so relays between them keep their hop limit",
    )
    ap.add_argument(
        "--rebroadcast-mode",
        default=M.REBROADCAST_ALL,
        choices=(
            M.REBROADCAST_ALL,
            M.REBROADCAST_ALL_SKIP_DECODING,
            M.REBROADCAST_LOCAL_ONLY,
            M.REBROADCAST_KNOWN_ONLY,
            M.REBROADCAST_CORE_PORTNUMS_ONLY,
            M.REBROADCAST_NONE,
        ),
    )
    ap.add_argument(
        "--profile",
        default="2.8",
        choices=M.VERSIONS + ("legacy",),
        help="which firmware release series' MAC and routing rules to obey, each taken at the "
        "final release of that series; legacy is this transport's own pre-fold-in model, not a "
        "firmware version",
    )
    ap.add_argument(
        "--old-profile",
        default="legacy",
        choices=M.VERSIONS + ("legacy",),
        help="the rules the --legacy-fraction share of nodes runs instead of --profile",
    )
    ap.add_argument(
        "--trace-interval-s",
        type=int,
        default=0,
        help="sample every adaptive quantity per node this often, and keep the series. A "
        "converged mean and an oscillating one look identical at the end of a run; 0 disables it",
    )
    ap.add_argument(
        "--no-adopt-hop-recommendation",
        action="store_true",
        help="compute the hop recommendation but do not apply it, holding the feedback loop open "
        "as the control against a mesh whose nodes all adopt it",
    )
    ap.add_argument(
        "--traceroute-per-hour",
        type=float,
        default=0.0,
        help="route discoveries per node per hour. A reply teaches a next hop for every node "
        "beyond the learner in the route, so this is what next-hop routing costs to seed",
    )
    ap.add_argument(
        "--dm-transport",
        default="hop-by-hop",
        choices=("hop-by-hop", "transport"),
        help="route an addressed SR message through the transport, so next-hop routing and its "
        "retry ladder decide what it costs, or walk a precomputed shortest path outside the "
        "transport as every published chain-arm cost was measured",
    )
    ap.add_argument(
        "--dm-mode",
        default="directed-with-late-flood",
        choices=("flood-only", "directed-with-late-flood", "m4-early-flood"),
        help="how a DM escalates: never directed, directed until the last retry (the shipped "
        "behaviour), or flooding one retry sooner whenever the route is not verified (M4, which "
        "is written and compiled out)",
    )
    ap.add_argument(
        "--coding-rate-ladder",
        action="store_true",
        help="raise the coding rate on each retransmission - base, base+1, then 4/8 (branch "
        "CRCRRCRRR, no release has it)",
    )
    ap.add_argument(
        "--extra-repeats",
        action="store_true",
        help="RepeatScalingModule: tolerate a second heard copy of a text before cancelling our "
        "own queued rebroadcast, unless the mesh is busy (branch extra-repeats, no release has it)",
    )
    ap.add_argument(
        "--congestion-mode",
        default="adaptive",
        choices=("adaptive", "static"),
        help="adaptive recomputes the broadcast throttle per node from that node's own online "
        "count at the moment it sends, as the firmware does; static applies one mesh-wide "
        "coefficient for the whole run",
    )
    ap.add_argument(
        "--signature-policy",
        default=M.SIGNATURE_POLICY_COMPATIBLE,
        choices=(
            M.SIGNATURE_POLICY_COMPATIBLE,
            M.SIGNATURE_POLICY_BALANCED,
            M.SIGNATURE_POLICY_STRICT,
        ),
        help="config.security.packet_signature_policy, applied on receive: what a node does with "
        "an unsigned or unverifiable packet",
    )
    ap.add_argument(
        "--warm-num-nodes",
        type=int,
        default=None,
        help="WARM_NODE_COUNT: identities kept for peers evicted from the hot store, so a DM to "
        "them still encrypts. Omit to size it from each node's board; 0 disables the tier",
    )
    ap.add_argument(
        "--legacy-fraction",
        type=float,
        default=0.0,
        help="share of nodes running --old-profile rather than --profile, for a mixed-version mesh",
    )
    ap.add_argument(
        "--profile-flag",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="override one rule, repeatable. Specific pre-2.5 pathologies live here rather than as "
        "a profile, e.g. --profile-flag clamp_cw=true for the unclamped Arduino map() window",
    )
    ap.add_argument(
        "--diurnal",
        default="commuter",
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
    ap.add_argument(
        "--congestion-input",
        default="hotstore",
        choices=("hotstore", "truesize", "utilisation"),
        help="what drives the throttle: the hot store (what the firmware does, and saturates), "
        "true mesh size (the unbounded ideal), or measured channel utilisation",
    )
    ap.add_argument(
        "--sr-retries",
        type=int,
        default=2,
        help="retries per addressed hop before a reconciliation message is given up on",
    )
    ap.add_argument(
        "--broadcast-interval-s",
        type=float,
        default=None,
        help="one interval for every device class; 3600 is the firmware client default",
    )
    ap.add_argument(
        "--max-num-nodes",
        type=int,
        default=120,
        help="MAX_NUM_NODES. Bounds the congestion input as getNumOnlineMeshNodes does, and "
        "sizes every node's NodeDB and packet-history ring unless --platform-mix gives them "
        "boards of their own. 10 = STM32WL, 120 = nRF52840, 250 = 16MB ESP32-S3",
    )
    ap.add_argument(
        "--role-mix",
        default="",
        choices=[""] + sorted(M.ROLE_MIXES),
        help="assign roles from a measured census instead of --router-fraction. Empty keeps the "
        "old behaviour; baymesh-2026-08 is 1769 real nodes (60%% CLIENT, 18%% CLIENT_MUTE, "
        "16%% CLIENT_BASE, 4%% ROUTER, 3%% ROUTER_LATE)",
    )
    ap.add_argument(
        "--platform-mix",
        default="uniform",
        choices=sorted(set(M.PLATFORM_MIXES) | set(M.PLATFORM_HOT_STORE)),
        help="board mix, which sets each node's hot store: a named mix, or one board for all. "
        "uniform = every node at the 120-entry default, as before platforms were modelled",
    )
    ap.add_argument(
        "--siting-mix",
        default="uniform",
        choices=sorted(set(M.SITING_MIXES) | set(M.SITINGS)),
        help="where the nodes physically are, as a gain offset on every link: a named mix, or one "
        "siting for all. Not from the firmware and not measured - a stated assumption, and a large "
        "one, since roof and basement differ by 26 dB",
    )
    ap.add_argument("--position-throttle", type=int, default=1)
    ap.add_argument("--telemetry-throttle", type=int, default=1)
    ap.add_argument(
        "--scale-area",
        action="store_true",
        help="grow the area with the node count so density is held constant, not conflated with size",
    )
    ap.add_argument(
        "--topology",
        default="uniform",
        choices=("uniform", "clustered", "corridor", "hub", "chain", "mixed"),
        help="mesh shape; `mixed` draws the generator from the seed so a sweep samples shapes",
    )
    ap.add_argument(
        "--hop-assign",
        default="centrality",
        choices=("centrality", "random"),
        help="centrality is realistic but confounds hop limit with position; random isolates it",
    )
    ap.add_argument(
        "--no-hop-spread",
        dest="hop_spread",
        action="store_false",
        help="one hop limit for everyone; the control against --hop-spread",
    )
    ap.add_argument(
        "--hop-spread",
        dest="hop_spread",
        default=True,
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

    ap.add_argument(
        "--protocol",
        default="sr",
        choices=("none", "chain", "sr"),
        help="none is the paired baseline; chain is today's SF++ announce-and-walk; sr is the sketch",
    )
    ap.add_argument(
        "--chain-walk-cap",
        type=float,
        default=4.0,
        help="abandon a walk after this many round trips per object, so a runaway is visible",
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
    ap.add_argument(
        "--no-charts",
        action="store_true",
        help="skip the charts a run renders beside its JSON",
    )
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
        # The statistics report is written by the run that produced the numbers, not left as a
        # post-processing step: an unattended run has to leave a readable result behind on its own.
        text = "\n\n".join(R.report_one(r) for r in reports)
        text_path = os.path.join(
            os.path.dirname(os.path.abspath(opts.out)),
            "reports",
            os.path.basename(opts.out).replace(".json", "") + ".txt",
        )
        os.makedirs(os.path.dirname(text_path), exist_ok=True)
        with open(text_path, "w") as f:
            f.write(text + "\n")
        print(f"wrote {text_path}")
        if not opts.no_charts:
            # Rendered by the same call that produced the JSON, so a figure cannot lag a withdrawn
            # number.
            path = AC.auto(reports, opts.out, kind="run")
            if path:
                print(f"wrote {path}")
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
