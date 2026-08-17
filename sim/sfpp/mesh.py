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

The rules are 2.8's, read off this tree rather than remembered: RadioInterface for the contention
window and the retransmission timer, MeshPacketQueue and RadioLibInterface for queue order and the
deferred `tx_after` window, FloodingRouter for who may cancel a dupe, Router::shouldDecrementHopLimit
for when a hop is free, NextHopRouter for directed delivery and its fallback to flooding. Profile
selects the rule set; `legacy` keeps the pre-2.8 approximation so runs made under it still reproduce.

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
NO_NEXT_HOP_PREFERENCE = 0

# Roles, with the firmware's rebroadcast semantics. ROUTER rebroadcasts early - it draws from the
# bottom of the contention window while everyone else waits behind a fixed router offset. ROUTER_LATE
# is the mirror image: it relays like a router but is pushed to the back of the window the moment it
# hears someone else do the job. CLIENT_BASE behaves as a router for traffic to or from its
# favourites and as a client for everything else. CLIENT_MUTE never rebroadcasts at all.
CLIENT = "CLIENT"
ROUTER = "ROUTER"
ROUTER_LATE = "ROUTER_LATE"
CLIENT_BASE = "CLIENT_BASE"
CLIENT_MUTE = "CLIENT_MUTE"
REPEATER = "REPEATER"

# Roles the firmware treats as router-like for hop preservation (Router::shouldDecrementHopLimit)
# and for refusing to cancel a dupe (FloodingRouter::roleAllowsCancelingDupe).
ROUTER_LIKE = (ROUTER, ROUTER_LATE, CLIENT_BASE)

# config.device.rebroadcast_mode. Only the ones that change what gets relayed are modelled.
REBROADCAST_ALL = "ALL"
REBROADCAST_ALL_SKIP_DECODING = "ALL_SKIP_DECODING"
REBROADCAST_LOCAL_ONLY = "LOCAL_ONLY"
REBROADCAST_KNOWN_ONLY = "KNOWN_ONLY"
REBROADCAST_NONE = "NONE"
REBROADCAST_CORE_PORTNUMS_ONLY = "CORE_PORTNUMS_ONLY"

# The portnums CORE_PORTNUMS_ONLY lets through. Anything the SR protocol invents is not among them,
# which is the point of modelling this mode at all.
CORE_PORTNUMS = frozenset({1, 3, 4, 5, 67, 70})

# From RadioInterface.h: the contention window is sized from SNR so that distant nodes - the ones
# whose rebroadcast actually extends coverage - transmit first.
CW_MIN, CW_MAX = 3, 8
SNR_MIN_DB, SNR_MAX_DB = -20.0, 10.0

# RadioInterface::getRetransmissionMsec - time to construct, process and reconstruct a packet.
PROCESSING_TIME_MSEC = 4500.0

# NextHopRouter.h. Broadcasts get three attempts, a directed delivery five.
NUM_RELIABLE_RETX = 3
NUM_RELIABLE_UNICAST_ATTEMPTS = 5

# NextHopRouter.h: a learned route not confirmed for this long, or failing this many directed
# deliveries in a row, decays back to flooding rather than being trusted on the next DM.
ROUTE_TTL_MSEC = 30 * 60 * 1000.0
ROUTE_FAILURE_THRESHOLD = 3
ROUTE_HEALTH_MAX = 32

# PacketHistory keeps up to three relayers per record; next-hop learning reads them.
MAX_RELAYERS = 3

# mesh-pb-constants.h. The hot store is small and platform-dependent, and everything routing knows
# is bounded by it. A node cannot resolve, route to, or count a peer it has evicted.
#
# The spread is the interesting part: a real mesh is a mix of these, so "how well does next-hop
# routing work at this scale" has no single answer - it has one answer per platform, and the STM32WL
# in the corner is having a completely different experience from the 16 MB S3 on the hill.
PLATFORM_HOT_STORE = {
    "stm32wl": 10,  # ARCH_STM32WL
    "esp32s3_4mb": 100,  # CONFIG_IDF_TARGET_ESP32S3, flash < 7 MB
    "nrf52840": 120,  # nRF52840 and generic ESP32, the compile-time default
    "esp32s3_8mb": 200,  # flash 7-15 MB
    "esp32s3_16mb": 250,  # flash >= 15 MB
}
MAX_NUM_NODES = PLATFORM_HOT_STORE["nrf52840"]

# Which board every declared hardware model actually is, as a hot-store size. Generated from this
# tree's own variants - each variant's platformio.ini declares custom_meshtastic_hw_model_slug,
# custom_meshtastic_architecture and custom_meshtastic_partition_scheme, and mesh-pb-constants.h
# turns those into MAX_NUM_NODES. So this table is derived, not guessed, and regenerating it after a
# firmware bump is a script rather than an argument.
#
# The one that matters: HELTEC_V3, the most widely deployed board there is, is an 8 MB ESP32-S3 and
# therefore gets **200** slots, not the 120 an "nRF52840-ish default" assumption would give it.
HARDWARE_STORE = {
    # 100 slots
    "CDEBYTE_EORA_S3": 100,
    "MINI_EPAPER_S3": 100,
    "THINKNODE_M2": 100,
    "THINKNODE_M5": 100,
    "TLORA_T3_S3": 100,
    # 120 slots
    "CANARYONE": 120,
    "DIY_V1": 120,
    "DR_DEV": 120,
    "HELTEC_HT62": 120,
    "HELTEC_MESH_NODE_T096": 120,
    "HELTEC_MESH_NODE_T1": 120,
    "HELTEC_MESH_NODE_T114": 120,
    "HELTEC_MESH_POCKET": 120,
    "HELTEC_MESH_SOLAR": 120,
    "HELTEC_MESH_TOWER_V2": 120,
    "HELTEC_V1": 120,
    "HELTEC_V2_0": 120,
    "HELTEC_V2_1": 120,
    "HELTEC_WIRELESS_TRACKER_V2": 120,
    "HYDRA": 120,
    "M5STACK": 120,
    "M5STACK_C6L": 120,
    "MESH_TRACKER_X1": 120,
    "MUZI_BASE": 120,
    "MUZI_R1_NEO": 120,
    "NANO_G1": 120,
    "NANO_G1_EXPLORER": 120,
    "NANO_G2_ULTRA": 120,
    "NOMADSTAR_METEOR_PRO": 120,
    "NRF52_PROMICRO_DIY": 120,
    "NRF54L15_DK": 120,
    "RADIOMASTER_900_BANDIT_NANO": 120,
    "RAK11200": 120,
    "RAK11310": 120,
    "RAK3401": 120,
    "RAK4631": 120,
    "RP2040_LORA": 120,
    "RPI_PICO": 120,
    "SEEED_SOLAR_NODE": 120,
    "SEEED_WIO_TRACKER_L1": 120,
    "SEEED_WIO_TRACKER_L1_EINK": 120,
    "STATION_G1": 120,
    "TBEAM": 120,
    "TBEAM_1_WATT": 120,
    "TBEAM_BPF": 120,
    "TBEAM_V0P7": 120,
    "THINKNODE_M1": 120,
    "THINKNODE_M3": 120,
    "THINKNODE_M6": 120,
    "THINKNODE_M8": 120,
    "TLORA_C6": 120,
    "TLORA_V1": 120,
    "TLORA_V2": 120,
    "TLORA_V2_1_1P6": 120,
    "TLORA_V2_1_1P8": 120,
    "TRACKER_T1000_E": 120,
    "T_ECHO": 120,
    "T_ECHO_LITE": 120,
    "T_ECHO_PLUS": 120,
    "T_IMPULSE_PLUS": 120,
    "WIO_WM1110": 120,
    "WISMESH_HUB": 120,
    "WISMESH_TAG": 120,
    "WISMESH_TAP": 120,
    "XIAO_NRF52_KIT": 120,
    # 200 slots
    "HELTEC_V3": 200,
    "HELTEC_VISION_MASTER_E213": 200,
    "HELTEC_VISION_MASTER_E290": 200,
    "HELTEC_VISION_MASTER_T190": 200,
    "HELTEC_WIRELESS_PAPER": 200,
    "HELTEC_WIRELESS_PAPER_V1_0": 200,
    "HELTEC_WIRELESS_TRACKER": 200,
    "HELTEC_WIRELESS_TRACKER_V1_0": 200,
    "HELTEC_WSL_V3": 200,
    "LILYGO_TBEAM_S3_CORE": 200,
    "PICOMPUTER_S3": 200,
    "SEEED_XIAO_S3": 200,
    "SENSECAP_INDICATOR": 200,
    "T_WATCH_S3": 200,
    "UNPHONE": 200,
    # 250 slots
    "CROWPANEL": 250,
    "HELTEC_V4": 250,
    "HELTEC_V4_R8": 250,
    "MESHNOLOGY_W10": 250,
    "MESHNOLOGY_W12": 250,
    "RAK3312": 250,
    "STATION_G2": 250,
    "STATION_G3": 250,
    "T_DECK": 250,
    "T_DECK_PRO": 250,
    "T_LORA_PAGER": 250,
    "WISMESH_TAP_V2": 250,
}

# No declared hardware model maps to the 10-slot STM32WL tier: the stm32 variants in this tree
# (wio-e5, rak3172, nucleo_wl55jc and friends) do not declare a hw_model_slug, so they cannot be
# named in a census by slug. The `constrained` mix below reaches that tier directly, and is a
# stress test rather than a deployment.


def census_to_mix(census):
    """Turn a real hardware census into a platform mix the sim can draw from.

    `census` maps hardware model slugs - the names the firmware puts on the wire, and the names a
    network dashboard reports - to counts or shares. Unknown slugs raise rather than being silently
    dropped into a default bucket, because a census that is 30% "unrecognised" quietly becomes a
    census of whatever the default happens to be.

    Returns weights over the platform names in PLATFORM_HOT_STORE, normalised to sum to one.
    """
    by_store = {}
    total = 0.0
    for model, count in census.items():
        slug = model.upper().replace("-", "_").replace(" ", "_")
        if slug not in HARDWARE_STORE:
            raise ValueError(
                f"unknown hardware model {model!r}; add it to HARDWARE_STORE or drop it from "
                "the census deliberately"
            )
        store = HARDWARE_STORE[slug]
        by_store[store] = by_store.get(store, 0.0) + float(count)
        total += float(count)
    if total <= 0:
        raise ValueError("census has no nodes in it")

    store_to_platform = {size: name for name, size in PLATFORM_HOT_STORE.items()}
    return {
        store_to_platform[store]: weight / total for store, weight in by_store.items()
    }


# Named mixes. `uniform` is every node on the 120-slot default - not a real deployment, but it is
# what the transport did before boards existed, so it keeps an old comparison honest.
#
# `baymesh-2026-08` is a real census: 1769 nodes on the Bay Area mesh, exported from
# meshview.bayme.sh/stats on 2026-08-17, run through census_to_mix(). 87% of it mapped to a board in
# this tree; the 13% that did not is PORTDUINO (operator-set cap, no fixed tier), one unknown model
# id, and the long tail reported only as "Other". The weights below are over the mapped share.
#
# It is one regional mesh at one moment, not the population of all meshes, and it should be cited
# that way. What it is emphatically not is a guess - the guess this replaced had the 200-slot tier
# leading on the reasoning that Heltec V3 is the most popular board. Both halves were wrong: RAK4631
# leads at 24%, Heltec V3 is second at 13%, and RAK4631 is a 120.
PLATFORM_MIXES = {
    "uniform": {"nrf52840": 1.0},
    "baymesh-2026-08": {
        "nrf52840": 0.616,  # RAK4631 24%, T1000-E 8%, WIO Tracker L1 4%, T114 4%, T-Echo 2%, ...
        "esp32s3_8mb": 0.192,  # Heltec V3 13%, T-Beam S3 Core, XIAO S3, Wireless Tracker
        "esp32s3_16mb": 0.192,  # Heltec V4 10%, Station G2 5%, T-Deck 2%
    },
    # Every node on the smallest store there is. **No node in the census is on this tier** - the
    # STM32WL boards are a rounding error in the field - so this is a stress test rather than a
    # deployment: what routing does when almost nothing fits, which is the regime a very large mesh
    # eventually puts a 120 in anyway.
    "constrained": {"stm32wl": 1.0},
}

# Role shares from the same census (1769 nodes). This matters more to a flood than the board mix
# does, and the simulator's old default - 10% ROUTER and nothing else - was wrong in both
# directions at once: two and a half times too many routers, and **no CLIENT_MUTE at all** where
# nearly a fifth of the real mesh never rebroadcasts. Overstating the number of nodes willing to
# relay is exactly the error that flatters a flood.
#
# TRACKER, CLIENT_HIDDEN, TAK and SENSOR together are ~1% and fold into CLIENT: none of them changes
# a rebroadcast decision in 2.8, which is all this model reads a role for.
ROLE_MIXES = {
    "baymesh-2026-08": {
        CLIENT: 0.60,
        CLIENT_MUTE: 0.18,
        CLIENT_BASE: 0.16,
        ROUTER: 0.04,
        ROUTER_LATE: 0.03,
    },
    # The pre-census default, kept so earlier runs can be reproduced and compared against.
    "legacy-default": {CLIENT: 0.90, ROUTER: 0.10},
}


def packet_history_max(max_num_nodes):
    """PACKETHISTORY_MAX - twice the hot store, floored at 100."""
    return max(max_num_nodes * 2, 100)


# NodeDB.cpp:3330 and MeshTypes.h:51. Both two hours, and both narrow what can be resolved: a peer
# not heard inside the window is neither online for congestion scaling nor a usable next hop.
NUM_ONLINE_SECS = 60 * 60 * 2
NEXTHOP_NEIGHBOR_FRESH_MSEC = 60 * 60 * 2 * 1000.0

# Same-SF LoRa capture: a packet survives an overlap if it is this much stronger than the
# interferer, or loses if the interferer locked the preamble first and is not this much weaker.
CAPTURE_DB = 6.0

# Longest a packet can hold the channel at the slowest preset, so a scan back this far cannot miss
# an overlap. LONG_SLOW at a full payload is about 6 s; the margin is deliberate.
MAX_AIRTIME_MS = 20000.0

# The firmware's TX queue is finite, and overflow is its only drop: setTransmitDelay reschedules a
# blocked packet indefinitely rather than giving up on it, so congestion shows up as a full queue
# and as latency, never as a packet that quietly evaporates. The old model also dropped after 400
# backoffs; that cap has no counterpart in the firmware and is gone. On overflow the firmware picks
# which packet to lose rather than always losing the newcomer - see replaceLowerPriorityPacket.
QUEUE_DEPTH = 16

# meshtastic_MeshPacket_Priority, only the values the queue order actually distinguishes.
PRIORITY_BACKGROUND = 10
PRIORITY_DEFAULT = 64
PRIORITY_RELIABLE = 70
PRIORITY_ACK = 120


class Profile:
    """Which firmware's rules to obey.

    `2.8` is this tree. `legacy` restores the rules the transport carried before the fold-in, so a
    result can be attributed to a rule change rather than to the rewrite around it. It is not
    bit-identical to the pre-fold-in code: the TX queue replaced a recursive retry closure, so the
    RNG is consumed in a different order and a seed does not reproduce the old run packet for
    packet. Distributions are the thing it preserves, not streams.

    The flags are separate rather than one version string because the interesting sweeps turn them
    one at a time - the router offset alone moves relay counts more than everything else together.
    """

    __slots__ = (
        "name",
        "cw_min",
        "cw_max",
        "snr_min",
        "snr_max",
        "router_offset",
        "router_cw_floor",
        "quantised_slots",
        "clamp_cw",
        "util_backoff",
        "role_aware_cancel",
        "late_window",
        "preserve_hops",
        "hop_upgrade",
        "next_hop_routing",
        "reliable_retx",
        "exhaust_hops",
        "event_relay_hop_limit",
        "opaque_relay",
    )

    def __init__(self, name="2.8", **overrides):
        if name not in ("2.8", "legacy"):
            raise ValueError(f"unknown profile {name!r}")
        modern = name == "2.8"
        self.name = name
        self.cw_min = CW_MIN if modern else 2
        self.cw_max = CW_MAX
        self.snr_min = SNR_MIN_DB
        self.snr_max = SNR_MAX_DB if modern else 15.0
        # The 2 * CWmax * slotTime every non-router waits before it may rebroadcast. Without it a
        # well-placed client beats every router to the relay, which is the opposite of the design.
        self.router_offset = modern
        # The old model pinned a router to the bottom of the window and drew from 2^CWmin. 2.8
        # keeps a router's window SNR-derived and only halves the exponent to a doubling.
        self.router_cw_floor = not modern
        # random(0, N) is integer and half-open; a continuous draw cannot produce a slot collision.
        self.quantised_slots = modern
        # getCWsize() runs Arduino map() and does not constrain the result.
        self.clamp_cw = not modern
        self.util_backoff = modern
        self.role_aware_cancel = modern
        self.late_window = modern
        self.preserve_hops = modern
        self.hop_upgrade = modern
        self.next_hop_routing = modern
        self.reliable_retx = modern
        # Fork-only, and off in the firmware unless the module or the build flag is on.
        self.exhaust_hops = False
        self.event_relay_hop_limit = None
        self.opaque_relay = modern

        for key, value in overrides.items():
            if key not in Profile.__slots__ or key == "name":
                raise ValueError(f"unknown profile flag {key!r}")
            setattr(self, key, value)


def arduino_map(value, in_min, in_max, out_min, out_max):
    """Arduino's map(): long arithmetic, and famously no clamping.

    Two details decide the answer and neither is Python's default. The parameters are `long`, so a
    float SNR or utilisation is truncated toward zero on the way in - -5.7 dB enters as -5. And C
    integer division also truncates toward zero, where Python's `//` floors, which disagree for
    every negative numerator: getCWsize(-25) is 0 in the firmware and -1 under `//`.

    getCWsize() takes the result as a uint8_t without constraining it, so an SNR outside
    [SNR_MIN, SNR_MAX] extrapolates off the end of the window rather than saturating at it. That
    only matters in the tails - which is exactly where a relay decision on a marginal link is made.
    """
    value = int(
        value
    )  # truncates toward zero, as the implicit float -> long conversion does
    numerator = (value - int(in_min)) * (int(out_max) - int(out_min))
    denominator = int(in_max) - int(in_min)
    quotient = abs(numerator) // abs(denominator)
    if (numerator < 0) != (denominator < 0):
        quotient = -quotient
    return quotient + int(out_min)


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
        # The outer routing header. `relay_node` and `next_hop` are one byte on the wire - the last
        # byte of a 32-bit node number - and that truncation is the whole reason 2.8 has to ask
        # whether a byte resolves to exactly one node before trusting it.
        "relay_node",
        "next_hop",
        # rx_rssi / rx_snr are zero on a locally generated packet, which is how RadioLibInterface
        # tells the two apart when picking a transmit delay. Never fake them for our own traffic.
        "rx_rssi",
        "rx_snr",
        "priority",
        "want_ack",
        "request_id",
        "reply_id",
        "opaque",
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
        priority=None,
        want_ack=False,
        request_id=0,
        reply_id=0,
        opaque=False,
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
        self.relay_node = 0
        self.next_hop = NO_NEXT_HOP_PREFERENCE
        self.rx_rssi = 0.0
        self.rx_snr = 0.0
        self.priority = (
            priority
            if priority is not None
            else (PRIORITY_RELIABLE if want_ack else PRIORITY_DEFAULT)
        )
        self.want_ack = want_ack
        self.request_id = request_id
        self.reply_id = reply_id
        # A packet this node cannot decrypt. It never enters packet history or the app layer; the
        # only thing 2.8 does with it is relay the outer header (NextHopRouter::relayOpaquePacket).
        self.opaque = opaque

    def hops_taken(self):
        return self.hop_start - self.hop_limit

    def is_ack_or_reply(self):
        return bool(self.request_id or self.reply_id)

    def copy(self):
        """A relay copy. The firmware allocates from the packet pool; we just clone the header."""
        clone = Packet(
            self.id,
            self.origin,
            self.portnum,
            self.length,
            hop_limit=self.hop_limit,
            kind=self.kind,
            payload=self.payload,
            destination=self.destination,
            priority=self.priority,
            want_ack=self.want_ack,
            request_id=self.request_id,
            reply_id=self.reply_id,
            opaque=self.opaque,
        )
        clone.hop_start = self.hop_start
        clone.relay_node = self.relay_node
        clone.next_hop = self.next_hop
        clone.rx_rssi = self.rx_rssi
        clone.rx_snr = self.rx_snr
        return clone


class SeenRecord:
    """PacketHistory's record of one packet: what we relayed, who else did, and how far it can go.

    `highest_hop_limit` is the field the upgrade path turns on. Hearing the same packet again with
    more hops left than the copy we queued means an earlier relay took a shorter route, and 2.8
    throws away the queued copy in favour of the better one.
    """

    __slots__ = (
        "highest_hop_limit",
        "our_tx_hop_limit",
        "relayed_by",
        "next_hop",
        "rx_time",
        "sender",
    )

    def __init__(self, sender, hop_limit, next_hop, rx_time):
        self.sender = sender
        self.highest_hop_limit = hop_limit
        self.our_tx_hop_limit = 0
        self.relayed_by = []
        self.next_hop = next_hop
        self.rx_time = rx_time

    def note_relayer(self, relay_byte):
        if relay_byte and relay_byte not in self.relayed_by:
            if len(self.relayed_by) < MAX_RELAYERS:
                self.relayed_by.append(relay_byte)

    def was_relayer(self, relay_byte):
        return bool(relay_byte) and relay_byte in self.relayed_by


class NodeRecord:
    """One `NodeInfoLite` in the hot store: what this node knows about a peer, and when it heard it.

    Everything routing can do is bounded by this record existing. A peer evicted from the hot store
    is not a peer the device routes to badly - it is a peer the device cannot resolve a relay byte
    to, cannot hold a next hop for, and does not count as online. The store is small (10 on
    STM32WL) and the mesh may not be, which is the whole point of modelling it.
    """

    __slots__ = ("last_heard", "hops_away", "next_hop", "is_favourite")

    def __init__(self, last_heard, hops_away=None, is_favourite=False):
        self.last_heard = last_heard
        # None until we have heard a packet from this node with a usable hop count - `has_hops_away`
        # in the firmware. Zero means a direct neighbour, which is what next-hop resolution wants.
        self.hops_away = hops_away
        self.next_hop = NO_NEXT_HOP_PREFERENCE
        self.is_favourite = is_favourite

    @property
    def is_protected(self):
        """Protected records outrank recency when the store has to give something up."""
        return self.is_favourite


class RouteHealth:
    """How fresh a learned next hop is, and how many directed deliveries to it have failed.

    RAM-only in the firmware too: the route itself lives in NodeDB, this is just the metadata that
    lets getNextHop() decay a dead hop back to flooding instead of spending a DM discovering it.
    """

    __slots__ = ("learned_at", "consecutive_failures", "last_next_hop")

    def __init__(self, learned_at, next_hop):
        self.learned_at = learned_at
        self.consecutive_failures = 0
        self.last_next_hop = next_hop


class QueueEntry:
    """A packet waiting for the channel.

    `tx_after` is an absolute deadline, not a delay: MeshPacketQueue sorts every deferred packet
    behind every ready one, and the late-rebroadcast window is nothing more than setting it.
    """

    __slots__ = ("packet", "tx_after", "sent")

    def __init__(self, packet, tx_after=0.0):
        self.packet = packet
        self.tx_after = tx_after
        self.sent = False


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
        # 2.8 state
        "node_num",
        "rebroadcast_mode",
        "favourites",
        "history",
        "queue",
        "tx_token",
        "nodedb",
        "max_num_nodes",
        "history_max",
        "platform",
        "profile",
        "online",
        "route_health",
        "reliable",
        "util_ring",
        "util_index",
        "util_epoch",
    )

    def __init__(
        self,
        index,
        x,
        y,
        role=CLIENT,
        node_num=None,
        max_num_nodes=MAX_NUM_NODES,
        platform="nrf52840",
        profile=None,
    ):
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

        # A real 32-bit node number, so that two nodes can share a last byte the way they do on a
        # real mesh. Nothing in 2.8's routing is safe against that collision by construction; it
        # detects it and takes the conservative branch, which cannot be tested without collisions.
        self.node_num = node_num if node_num is not None else (index + 1)
        self.rebroadcast_mode = REBROADCAST_ALL
        self.favourites = set()  # node indices this operator marked favourite
        self.history = (
            {}
        )  # packet id -> SeenRecord (PacketHistory), bounded, oldest evicted
        self.queue = []  # QueueEntry, MeshPacketQueue order
        self.tx_token = (
            None  # the single pending transmit timer, overwritten like the firmware's
        )
        # The hot store: node index -> NodeRecord. Bounded, and the bound is load-bearing - it caps
        # what can be resolved, routed to, or counted as online.
        self.nodedb = {}
        # Which board this is, and therefore how much of the mesh it can hold in RAM.
        self.platform = platform
        # Which firmware this node is running. Per-node, because a real mesh is never all on one
        # version - the interesting question is what a 2.8 node does surrounded by older ones.
        self.profile = profile if profile is not None else Profile("2.8")
        # False once the node has been taken down. An offline node neither transmits nor receives,
        # and its NodeDB goes stale in everyone else's store rather than being deleted from it.
        self.online = True
        self.max_num_nodes = max_num_nodes
        self.history_max = packet_history_max(max_num_nodes)
        self.route_health = {}  # destination index -> RouteHealth
        self.reliable = {}  # packet id -> pending retransmission record

        # AirTime's 6 x 10 s ring of channel-busy milliseconds. Counts our own transmissions and
        # every packet we could hear, which is what channelUtilizationPercent() reports.
        self.util_ring = [0.0] * 6
        self.util_index = 0
        self.util_epoch = 0.0

    @property
    def relay_byte(self):
        """The last byte of our node number - all that fits in the packet's relay_node field."""
        return self.node_num & 0xFF

    def position(self):
        return (self.x, self.y)

    def is_router_like(self):
        return self.role in ROUTER_LIKE

    # ---- NodeDB (hot store) ------------------------------------------------------------

    def update_from(self, peer, now, hops_away=None):
        """NodeDB::updateFrom - note that we heard a peer, and how far away it is.

        `hops_away` stays None until a packet arrives with a usable hop count, matching
        `has_hops_away`: "we have never established this" is a different answer from "zero hops",
        and next-hop resolution turns on the difference.
        """
        record = self.nodedb.get(peer)
        if record is None:
            record = NodeRecord(now, hops_away, is_favourite=peer in self.favourites)
            self.nodedb[peer] = record
        else:
            record.last_heard = now
            if hops_away is not None:
                record.hops_away = hops_away
        return record

    def trim_nodedb(self):
        """Demote the stalest unprotected record when the store overflows.

        `demoteOldestHotNodesToWarm`: protection outranks recency, and within a class the
        most-recently-heard survives. There is no warm tier here - a demoted node is simply
        forgotten, which is what the hot store's callers experience either way.

        Returns the records dropped, because losing one is how a learned route dies **without any
        expiry being involved** - see the four separate lifetimes in Mesh.get_next_hop.
        """
        dropped = []
        while len(self.nodedb) > self.max_num_nodes:
            victim = min(
                self.nodedb,
                key=lambda peer: (
                    self.nodedb[peer].is_protected,
                    self.nodedb[peer].last_heard,
                ),
            )
            dropped.append(self.nodedb.pop(victim))
        return dropped

    def knows(self, peer):
        """Is this peer in the hot store at all? KNOWN_ONLY and LOCAL_ONLY ask exactly this."""
        return peer in self.nodedb

    def num_online(self, now):
        """NodeDB::getNumOnlineMeshNodes - bounded by the store *and* by a two-hour window.

        Not read by the transport, but it is the input to the congestion coefficient, and the
        reason that coefficient cannot run away on a large mesh.
        """
        cutoff = now - NUM_ONLINE_SECS * 1000.0
        return sum(1 for r in self.nodedb.values() if r.last_heard >= cutoff)

    # ---- PacketHistory ----------------------------------------------------------------

    def remember(self, packet_id, record):
        """Insert a history record, evicting the oldest when the ring is full."""
        self.history[packet_id] = record
        self.seen[packet_id] = record.rx_time
        while len(self.history) > self.history_max:
            oldest = min(self.history, key=lambda pid: self.history[pid].rx_time)
            del self.history[oldest]
            # `seen` is the campaign-facing view of the same ring; letting it outlive the record
            # would restore the suppression the eviction just gave up.
            self.seen.pop(oldest, None)

    def log_airtime(self, now, ms):
        """Add busy milliseconds to the current 10 s bucket, rolling the ring as time passes."""
        elapsed = int((now - self.util_epoch) // 10000.0)
        if elapsed > 0:
            if elapsed >= len(self.util_ring):
                self.util_ring = [0.0] * len(self.util_ring)
                self.util_index = 0
            else:
                for _ in range(elapsed):
                    self.util_index = (self.util_index + 1) % len(self.util_ring)
                    self.util_ring[self.util_index] = 0.0
            self.util_epoch += elapsed * 10000.0
        self.util_ring[self.util_index] += ms

    def channel_utilization_percent(self, now):
        self.log_airtime(now, 0.0)  # roll the ring forward before reading it
        return (sum(self.util_ring) / (len(self.util_ring) * 10000.0)) * 100.0


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


def place_clustered(count, area, rng, min_dist, towns=4, spread=0.10):
    """Towns, with a thin scatter between them. What most regional meshes actually look like.

    A uniform field gives every node roughly the same neighbourhood; a clustered one gives dense
    pockets joined by a handful of long links, which is where placement advice either holds or does
    not. Nine in ten nodes belong to a town; the rest are the ones holding the mesh together.
    """
    centres = [
        (rng.uniform(0.15, 0.85) * area, rng.uniform(0.15, 0.85) * area)
        for _ in range(towns)
    ]
    points, attempts = [], 0
    while len(points) < count and attempts < count * 4000:
        attempts += 1
        if rng.random() < 0.9:
            cx, cy = centres[rng.randrange(towns)]
            p = (rng.gauss(cx, spread * area), rng.gauss(cy, spread * area))
        else:
            p = (rng.uniform(0, area), rng.uniform(0, area))
        if not (0 <= p[0] <= area and 0 <= p[1] <= area):
            continue
        if all(math.dist(p, q) >= min_dist for q in points):
            points.append(p)
    if len(points) < count:
        raise RuntimeError("clustered placement could not converge")
    return points


def place_corridor(count, area, rng, min_dist, aspect=6.0):
    """A valley, a road, a coastline: long and thin, so the diameter is huge for the node count.

    Hop limit binds far harder here than in a square, and placement becomes nearly one-dimensional -
    the interesting question stops being "where in the area" and becomes "how far along".
    """
    length = area * math.sqrt(aspect)
    width = area / math.sqrt(aspect)
    points, attempts = [], 0
    while len(points) < count and attempts < count * 4000:
        attempts += 1
        p = (rng.uniform(0, length), rng.uniform(0, width))
        if all(math.dist(p, q) >= min_dist for q in points):
            points.append(p)
    if len(points) < count:
        raise RuntimeError("corridor placement could not converge")
    return points


def place_hub(count, area, rng, min_dist, spokes=6):
    """A dense core with radial arms. The core hears everything; the spoke ends hear almost nothing.

    This is the sharpest test of "put the archive beside a router": in a hub the well-connected nodes
    are all in one place, so an archive there is maximally redundant with its peers.
    """
    centre = (area / 2, area / 2)
    points, attempts = [], 0
    core = max(1, count // 3)
    while len(points) < count and attempts < count * 4000:
        attempts += 1
        if len(points) < core:
            p = (rng.gauss(centre[0], 0.08 * area), rng.gauss(centre[1], 0.08 * area))
        else:
            angle = (rng.randrange(spokes) / spokes) * 2 * math.pi
            along = rng.uniform(0.1, 0.5) * area
            jitter = rng.gauss(0, 0.02 * area)
            p = (
                centre[0] + math.cos(angle) * along + jitter,
                centre[1] + math.sin(angle) * along + jitter,
            )
        if not (0 <= p[0] <= area and 0 <= p[1] <= area):
            continue
        if all(math.dist(p, q) >= min_dist for q in points):
            points.append(p)
    if len(points) < count:
        raise RuntimeError("hub placement could not converge")
    return points


def place_chain(count, area, rng, min_dist, towns=None, spread=0.035):
    """Towns strung out in a line, each linked to the next. A valley, a rail line, a coast road.

    The point is a mesh that is *long and connected*. Stretching a uniform field far enough to exceed
    seven hops eventually fragments it - at 20 km with 60 nodes a fifth of them are isolated and the
    measured diameter becomes the diameter of a surviving fragment, which is meaningless. A chain
    stretches without breaking, because consecutive towns are placed inside each other's range.
    """
    towns = towns or max(3, count // 8)
    # Span the diagonal so the chain has room; step is what keeps consecutive towns in contact.
    step = area * 1.35 / max(1, towns - 1)
    centres = [
        (0.06 * area + i * step * 0.70, 0.5 * area + rng.gauss(0, 0.05 * area))
        for i in range(towns)
    ]
    points, attempts = [], 0
    while len(points) < count and attempts < count * 6000:
        attempts += 1
        cx, cy = (
            centres[len(points) % towns]
            if rng.random() < 0.92
            else centres[rng.randrange(towns)]
        )
        p = (rng.gauss(cx, spread * area), rng.gauss(cy, spread * area))
        if all(math.dist(p, q) >= min_dist for q in points):
            points.append(p)
    if len(points) < count:
        raise RuntimeError("chain placement could not converge")
    return points


TOPOLOGIES = {
    "uniform": lambda c, a, r, m: place_nodes(c, a, r, m),
    "clustered": place_clustered,
    "corridor": place_corridor,
    "hub": place_hub,
    "chain": place_chain,
}


def place(topology, count, area, rng, min_dist=300.0):
    """Place nodes by the named generator. `mixed` draws the generator from the same seed.

    Drawing the shape from the seed is the point: a sweep then samples across mesh *shapes* rather
    than across draws of one shape, and a placement rule that only survives uniform points is visibly
    an artefact of the generator rather than advice.
    """
    if topology == "mixed":
        topology = sorted(TOPOLOGIES)[rng.randrange(len(TOPOLOGIES))]
    return TOPOLOGIES[topology](count, area, rng, min_dist), topology


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

    def __init__(
        self,
        conf,
        nodes,
        rng,
        hop_limit=3,
        area=8000.0,
        extra_loss=0.0,
        burst_loss=0.0,
        burst_ms=60000.0,
        profile="2.8",
    ):
        self.conf = conf
        self.nodes = nodes
        self.rng = rng
        self.hop_limit = hop_limit
        self.area = area
        # The mesh-wide default. Individual nodes carry their own and may disagree with it; this
        # is what a node gets when nothing said otherwise, and what the report labels the run with.
        self.profile = profile if isinstance(profile, Profile) else Profile(profile)
        for node in nodes:
            if node.profile is None:
                node.profile = self.profile
        # A hook the traffic-management arm sets: fn(packet) -> bool, forcing one relay at
        # hop_limit 0 (TrafficManagementModule::shouldExhaustHops).
        self.should_exhaust_hops = None
        # A flat loss floor on every reception, on top of the physics. It stands in for the things
        # the model does not carry - interference from outside the mesh, fading, a receiver busy
        # elsewhere - and is the knob the capacity-against-loss sweep turns.
        self.extra_loss = extra_loss
        # Bursty deafness: a node is periodically unable to receive for a stretch, standing in for
        # a blocked antenna, a neighbour keying up nearby, or a radio busy elsewhere. Flat loss and
        # bursty loss are different problems for a sketch - flat loss spreads the divergence evenly
        # across buckets, a burst puts a whole bucket's worth into one.
        self.burst_loss = burst_loss
        self.burst_ms = burst_ms
        self._deaf_until = [0.0] * len(nodes)
        self._deaf_checked = [0.0] * len(nodes)
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
            "rebroadcasts_queued": 0,
            "rebroadcasts_cancelled": 0,
            "receptions": 0,
            "lost_to_collision": 0,
            "lost_to_half_duplex": 0,
            "lost_to_phy": 0,
            "bytes_on_air": 0,
            # 2.8 paths, so a run can show whether any of them fired at all.
            "hop_upgrades": 0,
            "late_window_clamps": 0,
            "cancel_refused_by_role": 0,
            "hop_limit_preserved": 0,
            "next_hop_unicast": 0,
            "next_hop_learned": 0,
            "next_hop_ambiguous": 0,
            "next_hop_fallbacks": 0,
            "route_expired_ttl": 0,
            "route_expired_failures": 0,
            "routes_lost_to_eviction": 0,
            "nodedb_evictions": 0,
            "reliable_retx": 0,
            "reliable_failures": 0,
            "opaque_relays": 0,
            "hops_exhausted": 0,
            "rebroadcast_suppressed_by_mode": 0,
            "nodes_taken_down": 0,
            "nodes_brought_up": 0,
            "links_severed": 0,
            "sends_while_offline": 0,
        }
        self.airtime_by_kind = {}
        # Filled by build() when per-node hop limits are in play; None means everyone uses the same.
        self.node_hop_limit = None
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
        comps = self.components()
        largest = max((len(c) for c in comps), default=0)
        return {
            "links": sum(degrees) // 2,
            "mean_degree": sum(degrees) / len(degrees),
            "isolated": sum(1 for d in degrees if d == 0),
            # A diameter measured across a fragmented graph is the diameter of whichever fragment the
            # walk started in, which is not a diameter. Report the structure so it cannot be read as one.
            "components": len(comps),
            "largest_component": largest,
            "connected": len(comps) == 1,
        }

    def components(self):
        seen, out = set(), []
        for start in range(len(self.nodes)):
            if start in seen:
                continue
            stack, comp = [start], []
            seen.add(start)
            while stack:
                node = stack.pop()
                comp.append(node)
                for peer in self.neighbours[node]:
                    if peer not in seen:
                        seen.add(peer)
                        stack.append(peer)
            out.append(comp)
        return out

    def diameter(self):
        """Longest shortest-path within the largest component, and None if the mesh is fragmented."""
        comps = self.components()
        if len(comps) != 1:
            return None
        return max(max(self.hops_from([i]).values()) for i in range(len(self.nodes)))

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
        """RadioInterface::computeSlotTimeMsec. 0.2 + 0.4 + 7 ms of propagation, turnaround and MAC.

        The CAD duration differs on the 2.4 GHz parts: AN1200.22 wants four symbols plus a
        sf-dependent term, where sub-GHz takes max(2.25, NUM_SYM_CAD + 0.5).
        """
        p = self.conf.current_preset
        sf = p["sf"]
        symbol_ms = (2.0**sf) / (p["bw"] / 1000.0)
        if self._wide_lora():
            return (4 + (2 * sf + 3) // 32) * symbol_ms + 7.6
        return max(2.25, 2 + 0.5) * symbol_ms + 7.6

    def _wide_lora(self):
        """The 2.4 GHz regions. Preset bandwidth above 500 kHz only happens there."""
        return self.conf.current_preset["bw"] > 500

    # ---- contention window (RadioInterface.cpp:779-855) --------------------------------

    def cw_size(self, node, snr):
        """RadioInterface::getCWsize. Integer map, and no clamp - see arduino_map()."""
        profile = self.nodes[node].profile
        cw = arduino_map(
            snr, profile.snr_min, profile.snr_max, profile.cw_min, profile.cw_max
        )
        if profile.clamp_cw:
            cw = min(profile.cw_max, max(profile.cw_min, cw))
        return cw

    def _draw_slots(self, node, bound):
        """random(0, bound): integer and half-open, so `bound` itself never comes out.

        Under the legacy profile this stays a continuous draw. That difference is not cosmetic: two
        nodes can only pick the same slot if slots are discrete, so a continuous draw removes an
        entire class of collision the firmware produces routinely.
        """
        if self.nodes[node].profile.quantised_slots:
            bound = int(bound)
            return self.rng.randrange(0, bound) if bound > 0 else 0
        return self.rng.uniform(0, bound) if bound > 0 else 0.0

    def tx_delay_msec(self, node):
        """RadioInterface::getTxDelayMsec - the delay for something we composed ourselves.

        The window is sized from channel utilisation, so a busy mesh backs off harder. Nothing fed
        this before the fold-in, which is why congestion used to cost latency but never contention.
        """
        profile = self.nodes[node].profile
        if not profile.util_backoff:
            return self.slot_time_ms() * self.rng.uniform(1, 4)
        util = self.nodes[node].channel_utilization_percent(self.now)
        cw = arduino_map(util, 0, 100, profile.cw_min, profile.cw_max)
        return self._draw_slots(node, 2**cw) * self.slot_time_ms()

    def _rebroadcasts_early(self, node):
        """RadioInterface::shouldRebroadcastEarlyLikeRouter - ROUTER, and only ROUTER."""
        return self.nodes[node].role == ROUTER

    def tx_delay_weighted(self, node, snr):
        """RadioInterface::getTxDelayMsecWeighted - the delay for relaying someone else's packet.

        High SNR means a large window, because a node that heard the packet loudly is close to the
        sender and its relay adds least. The router offset is the part that was missing: everyone
        who is not a ROUTER waits out the whole router window first, so routers always go first.
        """
        profile = self.nodes[node].profile
        cw = self.cw_size(node, snr)
        slot = self.slot_time_ms()
        if self._rebroadcasts_early(node):
            if profile.router_cw_floor:
                return self._draw_slots(node, 2**profile.cw_min) * slot
            return self._draw_slots(node, 2 * cw) * slot
        offset = 2 * profile.cw_max * slot if profile.router_offset else 0.0
        return offset + self._draw_slots(node, 2**cw) * slot

    def tx_delay_weighted_worst(self, node, snr):
        """RadioInterface::getTxDelayMsecWeightedWorst - the far end of a non-router's window.

        This is the whole definition of "late": ROUTER_LATE relays at the point everyone else would
        already have given up on.
        """
        profile = self.nodes[node].profile
        return (2 * profile.cw_max + 2 ** self.cw_size(node, snr)) * self.slot_time_ms()

    def retransmission_msec(self, node, packet):
        """RadioInterface::getRetransmissionMsec - long enough for a send and an ACK to come back.

        Assumes the worst contention window and a responder at half the SNR range, then adds the
        4.5 s the firmware allows for constructing, processing and reconstructing a packet.
        """
        airtime = int(self.airtime_ms(packet.length))
        util = self.nodes[node].channel_utilization_percent(self.now)
        profile = self.nodes[node].profile
        cw = arduino_map(util, 0, 100, profile.cw_min, profile.cw_max)
        slot = self.slot_time_ms()
        return (
            2 * airtime
            + (
                2**cw
                + 2 * profile.cw_max
                + 2 ** ((profile.cw_max + profile.cw_min) // 2)
            )
            * slot
            + PROCESSING_TIME_MSEC
        )

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

    # ---- TX queue (MeshPacketQueue.cpp, RadioLibInterface.cpp) -------------------------

    def send(self, node, packet, token=None):
        """RadioLibInterface::send - enqueue, then set the transmit delay.

        The radio holds the packet; it does not discard it. A congested mesh shows up as latency and
        as a full queue, not as packets that quietly evaporate. The one drop is queue overflow,
        which is what the firmware does too.

        `token` is the campaign's handle on a packet it may want to cancel; it stays a dict with
        `sent` and `event` keys so callers written against the old signature keep working.
        """
        radio = self.nodes[node]
        if not radio.online:
            self.stats["sends_while_offline"] += 1
            return None
        entry = QueueEntry(packet)
        if len(radio.queue) >= QUEUE_DEPTH:
            # Something is dropped either way, so the counter fires either way - the question the
            # firmware asks is only *which* packet. MeshPacketQueue::enqueue sets `dropped` before
            # it knows the answer, and txDrop counts that.
            self.stats["queue_drops"] += 1
            if not self._replace_lower_priority(radio, entry):
                return None
        else:
            self._enqueue(radio, entry)
        if token is not None:
            token["entry"] = entry
        self.set_transmit_delay(node)
        return entry

    def _replace_lower_priority(self, radio, entry):
        """MeshPacketQueue::replaceLowerPriorityPacket - make room, or refuse to.

        A full queue does not simply reject the newcomer: the firmware looks for something it would
        rather lose. Three chances, in order, and each one gives up the *back* of the queue because
        that is the packet furthest from being sent.

        This only bites once the queue holds a mix, which for this transport means once ROUTER_LATE
        is in play - the late window is what puts deferred packets behind ready ones.
        """
        if not radio.queue:
            return False
        packet = entry.packet
        back = radio.queue[-1]

        # 1. The back is ready and worth less than the newcomer.
        if not back.tx_after and back.packet.priority < packet.priority:
            self._evict(radio, len(radio.queue) - 1)
            self._enqueue(radio, entry)
            return True

        if back.tx_after:
            # 2. The back is deferred, so look past the deferred tail for the last ready packet and
            #    take that instead - a deferred packet is not necessarily the cheapest thing to lose.
            index = len(radio.queue) - 1
            while index > 0 and radio.queue[index].tx_after:
                index -= 1
            candidate = radio.queue[index]
            if not candidate.tx_after and candidate.packet.priority < packet.priority:
                self._evict(radio, index)
                self._enqueue(radio, entry)
                return True

            # 3. Nothing ready to give up. Drop the back if its deadline has already passed and the
            #    newcomer is more urgent still: ready always beats deferred, and between two overdue
            #    packets the one that has been waiting longer goes first.
            if self.now >= back.tx_after:
                new_goes_first = not entry.tx_after or (
                    self.now >= entry.tx_after
                    and (self.now - back.tx_after) < (self.now - entry.tx_after)
                )
                if new_goes_first:
                    self._evict(radio, len(radio.queue) - 1)
                    self._enqueue(radio, entry)
                    return True

        return False

    @staticmethod
    def _evict(radio, index):
        """Drop a queued packet to make room, and forget any relay record pointing at it."""
        evicted = radio.queue.pop(index)
        radio.pending.pop(evicted.packet.id, None)
        return evicted

    @staticmethod
    def _enqueue(radio, entry):
        """MeshPacketQueue::enqueue. Deferred packets sort behind ready ones, always.

        Within the ready group it is priority order, newest last among equals; within the deferred
        group it is deadline order. Keeping the two groups apart is what makes the late-rebroadcast
        window work - a clamped packet goes to the back and stays there until its time comes.
        """
        if entry.tx_after:
            position = len(radio.queue)
            while position > 0:
                prev = radio.queue[position - 1]
                if not prev.tx_after or prev.tx_after <= entry.tx_after:
                    break
                position -= 1
        else:
            position = 0
            while position < len(radio.queue):
                other = radio.queue[position]
                if other.tx_after or other.packet.priority < entry.packet.priority:
                    break
                position += 1
        radio.queue.insert(position, entry)

    def set_transmit_delay(self, node):
        """RadioLibInterface::setTransmitDelay - decide when to next look at the queue.

        A packet we relayed carries the RSSI and SNR it arrived with, and that is exactly how the
        firmware distinguishes it from something we composed: a locally generated packet has both at
        zero, because the radio's noise floor offset guarantees a received one never does.
        """
        radio = self.nodes[node]
        if not radio.queue:
            return
        entry = radio.queue[0]
        packet = entry.packet
        if entry.tx_after:
            add = (
                self.tx_delay_weighted(node, packet.rx_snr)
                if packet.rx_rssi
                else self.tx_delay_msec(node)
            )
            entry.tx_after = min(
                max(entry.tx_after + add, self.now + add),
                self.now + 2 * self.tx_delay_weighted_worst(node, packet.rx_snr),
            )
            self._arm(node, entry.tx_after)
        elif packet.rx_snr == 0 and packet.rx_rssi == 0:
            self._arm(node, self.now + self.tx_delay_msec(node))
        else:
            self._arm(node, self.now + self.tx_delay_weighted(node, packet.rx_snr))

    def _arm(self, node, when):
        """One transmit timer per radio, overwritten on each call (txTimerOverwrite)."""
        radio = self.nodes[node]
        self.cancel(radio.tx_token)
        radio.tx_token = self.at(max(when, self.now), lambda: self._service_queue(node))

    def _service_queue(self, node):
        """The TRANSMIT_DELAY_COMPLETED handler: send the front packet, or wait some more."""
        radio = self.nodes[node]
        radio.tx_token = None
        if not radio.queue:
            return
        entry = radio.queue[0]
        if entry.tx_after and self.now < entry.tx_after:
            self._arm(node, entry.tx_after)
            return
        if not radio.online:
            radio.queue.clear()
            return
        if self._channel_busy(node) or radio.busy_until > self.now:
            self.stats["deferrals"] += 1
            self.set_transmit_delay(node)
            return
        radio.queue.pop(0)
        entry.sent = True
        self._start_send(node, entry.packet)
        if radio.queue:
            self.set_transmit_delay(node)

    def _cancel_sending(self, node, packet_id, only_ready=False, only_late=False):
        """Router::cancelSending / MeshPacketQueue::remove. A packet already keying up is gone."""
        radio = self.nodes[node]
        for index, entry in enumerate(radio.queue):
            if entry.packet.id != packet_id:
                continue
            if only_ready and entry.tx_after:
                continue
            if only_late and not entry.tx_after:
                continue
            return radio.queue.pop(index)
        return None

    def clamp_to_late_rebroadcast_window(self, node, packet):
        """RadioLibInterface::clampToLateRebroadcastWindow.

        ROUTER_LATE heard someone else relay this. It will not cancel - that is the role's whole
        point - but it moves to the back of the window, so it only speaks if the mesh still needs it.
        """
        entry = self._cancel_sending(node, packet.id, only_ready=True)
        if entry is None:
            return False
        entry.tx_after = self.now + self.tx_delay_weighted_worst(
            node, entry.packet.rx_snr
        )
        self._enqueue(self.nodes[node], entry)
        self.stats["late_window_clamps"] += 1
        self._arm(node, self.nodes[node].queue[0].tx_after or self.now)
        return True

    def _start_send(self, node, packet):
        duration = self.airtime_ms(packet.length)
        radio = self.nodes[node]
        radio.busy_until = self.now + duration
        radio.log_airtime(self.now, duration)
        packet.relay_node = radio.relay_byte
        tx = Transmission(packet, node, self.now, self.now + duration, radio.role)
        self.transmissions.append(tx)
        self.stats["transmissions"] += 1
        # A relay copy is the one carrying the RSSI it was heard at. Counted here rather than when
        # it was queued, so `rebroadcasts` stays what it always was: relays that reached the air.
        if packet.rx_rssi:
            self.stats["rebroadcasts"] += 1
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

        # A radio cannot hear while it is keying up. This matters more than it sounds: a router
        # relays everything it hears, so it spends a large share of the time deaf, and the node
        # beside it - which hears the same traffic and relays less - is a better listener than the
        # router itself. Any conclusion about where an archive belongs depends on modelling it.
        transmitting = {o.tx_node for o in interferers}

        # AirTime charges every packet a receiver could hear against its channel utilisation,
        # decoded or not, and that figure is what sizes the contention window for our own traffic.
        duration = tx.end - tx.start
        cad_floor = sensitivity - 3
        for rx in self.neighbours[tx.tx_node]:
            if self.rssi[tx.tx_node][rx] >= cad_floor:
                self.nodes[rx].log_airtime(self.now, duration)

        for rx in self.neighbours[tx.tx_node]:
            rssi = self.rssi[tx.tx_node][rx]
            if rssi < sensitivity:
                continue
            if not self.nodes[rx].online:
                continue
            if rx in transmitting:
                self.stats["lost_to_half_duplex"] += 1
                continue
            if not self._survives_capture(tx, rx, rssi, interferers, sensitivity):
                self.stats["lost_to_collision"] += 1
                continue
            if (
                self._deaf(rx)
                or self._lost_to_phy(rssi, packet.length)
                or (self.extra_loss and self.rng.random() < self.extra_loss)
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

    def _deaf(self, node):
        """Is this node inside a loss burst? Redrawn once per burst window, not per packet."""
        if not self.burst_loss:
            return False
        if self.now >= self._deaf_checked[node]:
            self._deaf_checked[node] = self.now + self.burst_ms
            if self.rng.random() < self.burst_loss:
                self._deaf_until[node] = self.now + self.burst_ms
        return self.now < self._deaf_until[node]

    def _lost_to_phy(self, rssi, length):
        import lib.radio_loss as radio_loss

        if not self.conf.PHY_LOSS_MODEL_ENABLED:
            return False
        return radio_loss.payload_is_lost(
            self.conf, rssi, self.conf.current_preset["cr"], length, self.rng.random()
        )

    # ---- breaking the mesh -------------------------------------------------------------

    def take_down(self, index):
        """Turn a node off. It stops transmitting and stops hearing anything.

        Deliberately *not* a deletion. Every other node keeps its NodeDB record for this one and
        keeps believing whatever it last learned - including a next hop pointing through it. That
        gap between what the mesh knows and what is true is the thing worth simulating; a mesh
        where failure instantly updates everyone's routing table is not a mesh under test.
        """
        node = self.nodes[index]
        if not node.online:
            return False
        node.online = False
        node.queue.clear()
        self.cancel(node.tx_token)
        node.tx_token = None
        node.busy_until = 0.0
        self.stats["nodes_taken_down"] += 1
        return True

    def bring_up(self, index):
        """Turn a node back on, with everything it knew intact.

        A real node that reboots loses far more than this, but a node that was merely out of range
        loses nothing, and both are "offline" to the rest of the mesh. `wipe` covers the other case.
        """
        node = self.nodes[index]
        if node.online:
            return False
        node.online = True
        self.stats["nodes_brought_up"] += 1
        return True

    def wipe(self, index):
        """Forget everything this node learned - a factory reset, or a store that did not persist."""
        node = self.nodes[index]
        node.nodedb.clear()
        node.history.clear()
        node.seen.clear()
        node.route_health.clear()

    def sever(self, a, b):
        """Cut the link between two nodes in both directions, leaving the rest of the mesh intact.

        A partition is the sharpest question an archive can be asked - two halves that each keep
        working, diverge, and then have to reconcile when the link comes back.
        """
        self.rssi[a][b] = -999.0
        self.rssi[b][a] = -999.0
        if b in self.neighbours[a]:
            self.neighbours[a].remove(b)
        if a in self.neighbours[b]:
            self.neighbours[b].remove(a)

    def partition(self, group):
        """Sever every link crossing out of `group`, splitting the mesh in two.

        Returns the number of links cut. Zero means the group was already disconnected from the
        rest, which is worth knowing rather than silently succeeding.
        """
        inside = set(group)
        cut = 0
        # Both directions, because links are not reciprocal here - `_build_links` gives each pair an
        # asymmetry draw, so A can hear B without B hearing A. Scanning only outward from the group
        # leaves every inbound-only link intact, and the mesh stays connected through them.
        for a in range(len(self.nodes)):
            for b in list(self.neighbours[a]):
                if (a in inside) != (b in inside):
                    self.sever(a, b)
                    cut += 1
        self.stats["links_severed"] += cut
        return cut

    def articulation_nodes(self):
        """The nodes whose loss would split the mesh - the bridges worth breaking.

        Plain Hopcroft-Tarjan over the link graph. Killing random nodes mostly does nothing on a
        well-connected mesh; killing these is what actually tests what the archive survives.
        """
        n = len(self.nodes)
        depth = [None] * n
        low = [0] * n
        parent = [None] * n
        found = set()

        for root in range(n):
            if depth[root] is not None:
                continue
            # Iterative, because a corridor topology can be deep enough to blow the stack.
            stack = [(root, iter(self.neighbours[root]))]
            depth[root] = low[root] = 0
            root_children = 0
            while stack:
                node, peers = stack[-1]
                advanced = False
                for peer in peers:
                    if depth[peer] is None:
                        parent[peer] = node
                        depth[peer] = low[peer] = depth[node] + 1
                        stack.append((peer, iter(self.neighbours[peer])))
                        if node == root:
                            root_children += 1
                        advanced = True
                        break
                    if peer != parent[node]:
                        low[node] = min(low[node], depth[peer])
                if advanced:
                    continue
                stack.pop()
                if stack:
                    up = stack[-1][0]
                    low[up] = min(low[up], low[node])
                    if up != root and low[node] >= depth[up]:
                        found.add(up)
            if root_children > 1:
                found.add(root)
        return sorted(found)

    def break_mesh(self, mode, count=3, rng=None):
        """Damage the mesh in a named way, and report what was done.

        The modes are ordered by how targeted they are. `random` is the null hypothesis and on a
        well-connected mesh it usually does nothing at all, which is worth seeing. `bridge` is the
        sharpest, but a mesh at degree 8 has no articulation points to take - so it falls back to
        degree, and says so rather than silently doing something else.

        `split` does not remove any node: it cuts every link across a geographic line, which is the
        only one of these guaranteed to actually partition a healthy mesh. That is the case an
        archive most needs answered - two halves that keep working and diverge.
        """
        rng = rng or self.rng
        live = [n.index for n in self.nodes if n.online]
        if mode == "none":
            return {"mode": mode, "taken_down": [], "links_cut": 0}

        if mode == "split":
            # Cut on the median x, so both halves are viable rather than one being a fragment.
            median = sorted(self.nodes[i].x for i in live)[len(live) // 2]
            west = {i for i in live if self.nodes[i].x < median}
            cut = self.partition(west)
            return {
                "mode": mode,
                "taken_down": [],
                "links_cut": cut,
                "sides": (len(west), len(live) - len(west)),
            }

        if mode == "bridge":
            targets = [i for i in self.articulation_nodes() if self.nodes[i].online]
            fell_back = not targets
            if fell_back:
                targets = sorted(live, key=lambda i: -len(self.neighbours[i]))
        elif mode == "routers":
            fell_back = False
            targets = sorted(
                (i for i in live if self.nodes[i].is_router_like()),
                key=lambda i: -len(self.neighbours[i]),
            )
        elif mode == "degree":
            fell_back = False
            targets = sorted(live, key=lambda i: -len(self.neighbours[i]))
        elif mode == "random":
            fell_back = False
            targets = rng.sample(live, min(count, len(live)))
        else:
            raise ValueError(f"unknown break mode {mode!r}")

        taken = [i for i in targets[:count] if self.take_down(i)]
        return {
            "mode": mode,
            "taken_down": taken,
            "links_cut": 0,
            "fell_back_to_degree": fell_back,
        }

    def note_heard(self, rx, peer, hops_away=None):
        """Record a peer in rx's hot store, trimming it if that pushed it over the cap.

        Counted here rather than on the Node so the loss is visible: a route dropped by eviction
        never expires, never fails, and never shows up as a fallback - it simply stops existing,
        which is the least legible of the four ways a next hop can die.
        """
        node = self.nodes[rx]
        record = node.update_from(peer, self.now, hops_away=hops_away)
        for dropped in node.trim_nodedb():
            self.stats["nodedb_evictions"] += 1
            if dropped.next_hop != NO_NEXT_HOP_PREFERENCE:
                self.stats["routes_lost_to_eviction"] += 1
        return record

    # ---- last-byte resolution (NodeDB::resolveLastByte) --------------------------------

    def resolve_unique_last_byte(self, rx, relay_byte, require_direct_neighbour=False):
        """NodeDB::resolveLastByte. Which node is this relay byte? Exactly one, several, or none.

        `relay_node` and `next_hop` are one byte of a 32-bit node number, so on a mesh of any size
        they collide. 2.8 never guesses: an ambiguous byte means take the safe branch - decrement
        the hop limit, flood instead of unicasting, learn nothing. Returning None is that answer.

        Two gates decide the candidate set, and both shrink it well below "every node with this
        byte". The **candidate** gate is the hot store: a peer we have evicted or never heard is not
        a candidate at all. The **relevance** gate then asks whether the peer is a plausible relay
        for this question - on the send path, a direct neighbour heard in the last two hours;
        otherwise a direct neighbour, a favourite, or a router-like node.

        The consequence is worth stating, because it is the opposite of what a birthday-problem
        table over the whole mesh suggests: **the small store makes the byte less ambiguous, not
        more.** What a large mesh costs is knowledge, not resolution.
        """
        if not relay_byte:
            return None
        me = self.nodes[rx]
        cutoff = self.now - NEXTHOP_NEIGHBOR_FRESH_MSEC
        match = None
        for peer, record in me.nodedb.items():
            if peer == rx or self.nodes[peer].relay_byte != relay_byte:
                continue
            if require_direct_neighbour:
                relevant = record.hops_away == 0 and record.last_heard >= cutoff
            else:
                relevant = (
                    record.hops_away == 0
                    or record.is_favourite
                    or self.nodes[peer].is_router_like()
                )
            if not relevant:
                continue
            if match is not None:
                # A second relevant candidate shares the byte. Nothing later can resolve that.
                self.stats["next_hop_ambiguous"] += 1
                return None
            match = peer
        return match

    # ---- routers (FloodingRouter.cpp, NextHopRouter.cpp) -------------------------------

    def is_rebroadcaster(self, rx, packet=None):
        """FloodingRouter::isRebroadcaster, plus the portnum gates the modes imply."""
        node = self.nodes[rx]
        if node.role == CLIENT_MUTE or node.rebroadcast_mode == REBROADCAST_NONE:
            return False
        if (
            packet is not None
            and node.rebroadcast_mode == REBROADCAST_CORE_PORTNUMS_ONLY
        ):
            if packet.portnum not in CORE_PORTNUMS:
                self.stats["rebroadcast_suppressed_by_mode"] += 1
                return False
        if packet is not None and node.rebroadcast_mode in (
            REBROADCAST_KNOWN_ONLY,
            REBROADCAST_LOCAL_ONLY,
        ):
            # Both modes need the originator in our NodeDB - now literally that, and so subject to
            # eviction: forgetting a node stops us relaying for it until we hear it again.
            if not node.knows(packet.origin):
                self.stats["rebroadcast_suppressed_by_mode"] += 1
                return False
        return True

    def _favourite_traffic(self, rx, packet):
        node = self.nodes[rx]
        return packet.origin in node.favourites or packet.destination in node.favourites

    def role_allows_canceling_dupe(self, rx, packet):
        """FloodingRouter::roleAllowsCancelingDupe.

        A ROUTER never drops a relay it has queued, however many other stations it hears do the
        job. That is deliberate - the role exists to be the copy that definitely goes out - and it
        is the single biggest reason a 2.8 router carries more airtime than the old model showed.
        """
        if not self.nodes[rx].profile.role_aware_cancel:
            return True
        role = self.nodes[rx].role
        if role in (ROUTER, ROUTER_LATE):
            return False
        if role == CLIENT_BASE:
            return not self._favourite_traffic(rx, packet)
        return True

    def should_decrement_hop_limit(self, rx, packet):
        """Router::shouldDecrementHopLimit - when a relay is free.

        A hop between two favourited routers costs nothing, so a spine of them does not eat the
        sender's hop budget. The first hop always pays, and an ambiguous relay byte always pays,
        because preserving hops for the wrong node is worse than charging one too many.
        """
        if not self.nodes[rx].profile.preserve_hops:
            return True
        if packet.hops_taken() == 0:
            return True
        node = self.nodes[rx]
        if not node.is_router_like():
            return True
        resolved = self.resolve_unique_last_byte(rx, packet.relay_node)
        if resolved is None:
            return True
        if resolved in node.favourites and self.nodes[resolved].is_router_like():
            self.stats["hop_limit_preserved"] += 1
            return False
        return True

    def get_next_hop(self, rx, destination, relay_byte):
        """NextHopRouter::getNextHop. None means flood.

        A stored route decays: unconfirmed for half an hour, or three failed directed deliveries in
        a row, and it is cleared rather than trusted for one more DM. We also never hand a packet
        back to the node that just relayed it, and never emit a byte that no longer resolves to a
        single reachable neighbour.

        The route lives in the destination's own hot-store record, exactly as `NodeInfoLite.next_hop`
        does, so **evicting a peer forgets the way to it**. That is the cost of a small store on a
        large mesh, and it is a different cost from the ambiguity the relay byte causes.
        """
        if destination == BROADCAST or not self.nodes[rx].profile.next_hop_routing:
            return None
        node = self.nodes[rx]
        record = node.nodedb.get(destination)
        stored = record.next_hop if record is not None else NO_NEXT_HOP_PREFERENCE
        if not stored:
            return None
        health = node.route_health.get(destination)
        if health is not None and health.last_next_hop == stored:
            failed = health.consecutive_failures >= ROUTE_FAILURE_THRESHOLD
            aged = (self.now - health.learned_at) >= ROUTE_TTL_MSEC
            if failed or aged:
                record.next_hop = NO_NEXT_HOP_PREFERENCE
                node.route_health.pop(destination, None)
                # Split, because they answer different questions: TTL says the route went unused
                # or unconfirmed, failures say it was tried and did not work.
                self.stats[
                    "route_expired_failures" if failed else "route_expired_ttl"
                ] += 1
                return None
        if stored == relay_byte:
            return None
        if (
            self.resolve_unique_last_byte(rx, stored, require_direct_neighbour=True)
            is None
        ):
            return None
        return stored

    def note_route_learned(self, rx, destination, next_hop):
        node = self.nodes[rx]
        if (
            destination not in node.route_health
            and len(node.route_health) >= ROUTE_HEALTH_MAX
        ):
            oldest = min(
                node.route_health, key=lambda d: node.route_health[d].learned_at
            )
            node.route_health.pop(oldest)
        node.route_health[destination] = RouteHealth(self.now, next_hop)

    def note_route_failure(self, rx, destination):
        health = self.nodes[rx].route_health.get(destination)
        if health is None:
            self.note_route_learned(rx, destination, NO_NEXT_HOP_PREFERENCE)
            health = self.nodes[rx].route_health[destination]
        health.consecutive_failures += 1

    # ---- reception ---------------------------------------------------------------------

    def _receive(self, rx, packet, rssi):
        node = self.nodes[rx]
        snr = rssi - self.conf.NOISE_LEVEL
        # A relay copy carries the RSSI and SNR it was heard at. Everything downstream - the
        # contention window, the late window, whether this looks locally generated - reads these.
        heard = packet.copy()
        heard.rx_rssi = rssi
        heard.rx_snr = snr

        if heard.opaque:
            # Never enters history, NodeDB, or the app layer. The only thing 2.8 does with a packet
            # it cannot decrypt is relay the outer header and let hop exhaustion bound it.
            self._relay_opaque(rx, heard)
            return

        record = node.history.get(packet.id)
        if record is None and packet.id in node.seen:
            # A caller marked this packet seen without going through originate() - the campaign's
            # hop-by-hop DM walk does exactly that. Treat it as history, or we would relay our own
            # packet back into the mesh the moment we overheard it.
            record = SeenRecord(
                packet.origin, packet.hop_limit, packet.next_hop, node.seen[packet.id]
            )
            node.remember(packet.id, record)

        if record is not None:
            # wasSeenRecently, the update half: the record tracks the best hop limit anyone has
            # shown us and everyone we have seen relay this, both of which later decisions read.
            upgraded = (
                node.profile.hop_upgrade and packet.hop_limit > record.highest_hop_limit
            )
            record.highest_hop_limit = max(record.highest_hop_limit, packet.hop_limit)
            record.note_relayer(packet.relay_node)
            we_were_next_hop = record.next_hop == node.relay_byte

            if upgraded and self._handle_upgraded(rx, heard):
                return
            self._handle_dupe(rx, heard, we_were_next_hop)
            return

        # NodeDB::updateFrom. getHopsAway is hop_start - hop_limit, so a packet that has not been
        # relayed yet is what tells us a peer is a direct neighbour.
        self.note_heard(rx, packet.origin, hops_away=packet.hops_taken())
        fresh = SeenRecord(packet.origin, packet.hop_limit, packet.next_hop, self.now)
        fresh.note_relayer(packet.relay_node)
        node.remember(packet.id, fresh)

        self._sniff_ack_or_reply(rx, heard)
        if self.on_receive is not None:
            self.on_receive(node, packet, rssi, snr)
        self.perhaps_rebroadcast(rx, heard)

    def _handle_dupe(self, rx, packet, we_were_next_hop):
        """FloodingRouter / NextHopRouter::shouldFilterReceived, the seen-recently branch."""
        node = self.nodes[rx]
        self._stop_retransmission(rx, packet.id)

        is_repeated = packet.hops_taken() == 0
        if is_repeated:
            # The originator is retrying, so its ACK never came back. If we no longer have a copy
            # queued we have to relay it again, or the retry buys nothing.
            if not any(e.packet.id == packet.id for e in node.queue):
                self.perhaps_rebroadcast(rx, packet)
            return
        if we_were_next_hop:
            return  # we were explicitly asked to relay this; a dupe does not excuse us

        if self.role_allows_canceling_dupe(rx, packet):
            entry = self._cancel_sending(rx, packet.id)
            if entry is not None:
                node.pending.pop(packet.id, None)
                self.stats["rebroadcasts_cancelled"] += 1
        else:
            self.stats["cancel_refused_by_role"] += 1

        if node.profile.late_window and (
            node.role == ROUTER_LATE
            or (node.role == CLIENT_BASE and self._favourite_traffic(rx, packet))
        ):
            self.clamp_to_late_rebroadcast_window(rx, packet)

    def _handle_upgraded(self, rx, packet):
        """FloodingRouter::perhapsHandleUpgradedPacket.

        A copy with more hops left than the one we queued reached us by a shorter route. Swap it in:
        relaying the copy with fewer hops left would strand everything beyond our own horizon.
        """
        node = self.nodes[rx]
        if not (self.is_rebroadcaster(rx, packet) and packet.hop_limit > 0):
            return False
        replaced = False
        for index, entry in enumerate(list(node.queue)):
            if (
                entry.packet.id == packet.id
                and entry.packet.hop_limit < packet.hop_limit
            ):
                node.queue.pop(index)
                replaced = True
                break
        if not replaced:
            return False
        node.pending.pop(packet.id, None)
        self.stats["hop_upgrades"] += 1
        self.perhaps_rebroadcast(rx, packet)
        return True

    def _relay_opaque(self, rx, packet):
        """NextHopRouter::relayOpaquePacket - relay from the immutable outer header only."""
        node = self.nodes[rx]
        if not node.profile.opaque_relay:
            return
        if node.rebroadcast_mode not in (
            REBROADCAST_ALL,
            REBROADCAST_ALL_SKIP_DECODING,
        ):
            return
        if (
            packet.hop_limit == 0
            or rx == packet.origin
            or packet.destination == rx
            or not self.is_rebroadcaster(rx)
        ):
            return
        if packet.next_hop not in (NO_NEXT_HOP_PREFERENCE, node.relay_byte):
            return
        relay = packet.copy()
        relay.hop_limit -= 1
        self._cap_event_hops(relay)
        self.stats["opaque_relays"] += 1
        self.send(rx, relay)

    def _cap_event_hops(self, packet):
        """NextHopRouter::capEventRelayHops - event mode bounds what a relay may pass on."""
        cap = (
            self.profile.event_relay_hop_limit
        )  # a mesh-wide build flag, not a per-node one
        if cap is None or packet.hop_limit <= cap:
            return
        reduction = packet.hop_limit - cap
        packet.hop_start = max(0, packet.hop_start - reduction)
        packet.hop_limit = cap

    def perhaps_rebroadcast(self, rx, packet):
        """NextHopRouter::perhapsRebroadcast. True when a relay copy was queued."""
        node = self.nodes[rx]
        exhaust = bool(
            self.nodes[rx].profile.exhaust_hops
            and self.should_exhaust_hops is not None
            and self.should_exhaust_hops(packet)
        )
        if packet.destination == rx or rx == packet.origin:
            return False
        if packet.hop_limit <= 0 and not exhaust:
            return False
        if not self.is_rebroadcaster(rx, packet):
            return False
        if packet.next_hop not in (NO_NEXT_HOP_PREFERENCE, node.relay_byte):
            return False

        relay = packet.copy()
        if exhaust:
            relay.hop_limit = 0
            self.stats["hops_exhausted"] += 1
        elif self.should_decrement_hop_limit(rx, packet):
            relay.hop_limit -= 1
        self._cap_event_hops(relay)

        # A directed packet gets a next hop if we know one; otherwise it floods, which is what
        # NO_NEXT_HOP_PREFERENCE means on the wire.
        relay.next_hop = (
            self.get_next_hop(rx, relay.destination, packet.relay_node)
            or NO_NEXT_HOP_PREFERENCE
        )
        if relay.next_hop != NO_NEXT_HOP_PREFERENCE:
            self.stats["next_hop_unicast"] += 1

        node.history.setdefault(
            packet.id,
            SeenRecord(packet.origin, packet.hop_limit, packet.next_hop, self.now),
        ).our_tx_hop_limit = relay.hop_limit

        self.stats["rebroadcasts_queued"] += 1
        record = {"sent": False, "event": None, "entry": None}
        entry = self.send(rx, relay, token=record)
        if entry is not None:
            node.pending[packet.id] = record
        return entry is not None

    def _sniff_ack_or_reply(self, rx, packet):
        """NextHopRouter::sniffReceived - learn a route from a delivery that demonstrably worked.

        Only a relayer that also carried the original teaches us anything, and only when its byte
        resolves to one node. Both gates matter: without the first we would learn a hop that never
        touched this path, and without the second we would aim every future DM at whichever node
        happened to share a last byte.
        """
        if not self.nodes[rx].profile.next_hop_routing or not packet.is_ack_or_reply():
            return
        node = self.nodes[rx]
        original = node.history.get(packet.request_id or packet.reply_id)
        if original is not None:
            we_were_relayer = original.was_relayer(node.relay_byte)
            already_relayer = original.was_relayer(packet.relay_node)
            sole_relayer = original.relayed_by == [node.relay_byte]
            if (we_were_relayer and already_relayer) or (
                packet.hops_taken() == 0 and sole_relayer
            ):
                resolved = self.resolve_unique_last_byte(rx, packet.relay_node)
                if resolved is not None:
                    self.note_heard(rx, packet.origin).next_hop = packet.relay_node
                    self.note_route_learned(rx, packet.origin, packet.relay_node)
                    self.stats["next_hop_learned"] += 1

        if packet.destination != rx:
            self._cancel_sending(rx, packet.request_id)
            self._stop_retransmission(rx, packet.request_id)

    # ---- reliable delivery (ReliableRouter, NextHopRouter::doRetransmissions) ----------

    def _start_retransmission(self, node, packet, attempts):
        if not self.nodes[node].profile.reliable_retx:
            return
        self.nodes[node].reliable[packet.id] = {
            "packet": packet,
            "left": attempts - 1,
            "initial": attempts - 1,
            "token": self.at(
                self.now + self.retransmission_msec(node, packet),
                lambda: self._do_retransmission(node, packet.id),
            ),
        }

    def _stop_retransmission(self, node, packet_id):
        record = self.nodes[node].reliable.pop(packet_id, None)
        if record is not None:
            self.cancel(record["token"])
        return record is not None

    def _do_retransmission(self, node, packet_id):
        radio = self.nodes[node]
        record = radio.reliable.get(packet_id)
        if record is None:
            return
        packet = record["packet"]
        if record["left"] <= 0:
            radio.reliable.pop(packet_id, None)
            self.stats["reliable_failures"] += 1
            return

        retry = packet.copy()
        if packet.destination != BROADCAST and record["left"] == 1:
            # Last directed try. The route has not worked; record the failure, clear it, and let
            # this attempt flood, which is the only thing left that can still deliver.
            self.note_route_failure(node, packet.destination)
            dest_record = radio.nodedb.get(packet.destination)
            if dest_record is not None:
                dest_record.next_hop = NO_NEXT_HOP_PREFERENCE
            retry.next_hop = NO_NEXT_HOP_PREFERENCE
            self.stats["next_hop_fallbacks"] += 1
        record["left"] -= 1
        self.stats["reliable_retx"] += 1
        self.send(node, retry)
        record["token"] = self.at(
            self.now + self.retransmission_msec(node, packet),
            lambda: self._do_retransmission(node, packet_id),
        )

    def _prune(self):
        """Keep the transmission list bounded; nothing this old can overlap anything new."""
        if len(self.transmissions) < 4000:
            return
        cutoff = self.now - MAX_AIRTIME_MS
        self.transmissions = [t for t in self.transmissions if t.start > cutoff]

    def hop_limit_for(self, node):
        """This node's configured hop limit. Operators do not all set 3."""
        return self.node_hop_limit[node] if self.node_hop_limit else self.hop_limit

    def originate(
        self,
        node,
        portnum,
        length,
        kind=None,
        payload=None,
        hop_limit=None,
        destination=BROADCAST,
        want_ack=False,
        priority=None,
        request_id=0,
        reply_id=0,
        opaque=False,
    ):
        """Inject a packet from a node's application layer, as if it had composed it.

        Mirrors Router::send: we add our own packet to the history first, so the copies we hear
        coming back are recognised as our own, and set the next hop before it goes out.
        """
        radio = self.nodes[node]
        if not radio.online:
            # The radio is off, so nothing is composed. Returning the packet anyway would let the
            # caller register a message that never existed - an archive counting objects it never
            # sent is exactly the silent accounting error this whole exercise is trying to avoid.
            self.stats["sends_while_offline"] += 1
            return None
        packet = Packet(
            self.new_packet_id(),
            node,
            portnum,
            length,
            hop_limit=self.hop_limit_for(node) if hop_limit is None else hop_limit,
            kind=kind,
            payload=payload,
            destination=destination,
            priority=priority,
            want_ack=want_ack,
            request_id=request_id,
            reply_id=reply_id,
            opaque=opaque,
        )
        packet.relay_node = radio.relay_byte
        packet.next_hop = (
            self.get_next_hop(node, destination, packet.relay_node)
            or NO_NEXT_HOP_PREFERENCE
        )
        if packet.next_hop != NO_NEXT_HOP_PREFERENCE:
            self.stats["next_hop_unicast"] += 1
        own = SeenRecord(node, packet.hop_limit, packet.next_hop, self.now)
        own.note_relayer(radio.relay_byte)
        radio.remember(packet.id, own)
        if want_ack:
            self._start_retransmission(
                node,
                packet,
                (
                    NUM_RELIABLE_RETX
                    if destination == BROADCAST
                    else NUM_RELIABLE_UNICAST_ATTEMPTS
                ),
            )
        self.send(node, packet)
        return packet


def assign_platforms(node_count, platform_mix, rng):
    """Draw a board for every node from a named mix.

    Drawn rather than striped, so the small-store nodes are not evenly spaced by construction - a
    mesh where the one STM32WL happens to sit on the only bridge is a mesh worth simulating, and
    striping would never produce it.
    """
    if platform_mix in PLATFORM_HOT_STORE:
        return [platform_mix] * node_count  # a single-board mesh, named directly
    if platform_mix not in PLATFORM_MIXES:
        raise ValueError(f"unknown platform mix {platform_mix!r}")
    weights = PLATFORM_MIXES[platform_mix]
    names = sorted(weights)
    return rng.choices(names, weights=[weights[n] for n in names], k=node_count)


def build(
    conf,
    node_count,
    area,
    rng,
    hop_limit=3,
    min_dist=300.0,
    router_fraction=0.0,
    extra_loss=0.0,
    burst_loss=0.0,
    burst_ms=60000.0,
    hop_spread=False,
    hop_assign="centrality",
    topology="uniform",
    profile="2.8",
    legacy_fraction=0.0,
    router_late_fraction=0.0,
    client_base_fraction=0.0,
    role_mix=None,
    favourite_routers=False,
    rebroadcast_mode=REBROADCAST_ALL,
    max_num_nodes=None,
    platform_mix="uniform",
):
    """A mesh with positions drawn from `rng` and a share of the nodes promoted to ROUTER.

    Routers are chosen by degree rather than at random: a deployment puts the repeater on the hill,
    and choosing them randomly would understate how much a flood depends on a few well-sited nodes.
    ROUTER_LATE and CLIENT_BASE are drawn from the same ranking, below the plain routers.
    """
    points, resolved = place(topology, node_count, area, rng, min_dist)
    # Real node numbers, so two nodes can share a last byte the way they do on a real mesh. 2.8
    # detects that collision and takes the conservative branch; sequential ids would hide the path.
    node_nums = [rng.randrange(1, 1 << 32) for _ in range(node_count)]
    # Hand out boards before positions matter: a node's hot store is a property of what it is, not
    # of where it sits. `max_num_nodes` overrides the mix outright, so a sweep can hold the store
    # fixed and vary something else.
    platforms = assign_platforms(node_count, platform_mix, rng)
    # Firmware version per node. Drawn at random rather than by degree: a node's owner updating it
    # has nothing to do with how well sited it is, and assuming otherwise would quietly decide the
    # answer to "do the old nodes hold the mesh back" before the sweep ran.
    default_profile = profile if isinstance(profile, Profile) else Profile(profile)
    legacy_profile = Profile("legacy")
    stale = set()
    if legacy_fraction > 0:
        want = max(1, int(round(node_count * legacy_fraction)))
        stale = set(rng.sample(range(node_count), min(want, node_count)))
    nodes = [
        Node(
            i,
            x,
            y,
            node_num=node_nums[i],
            platform=platforms[i],
            profile=legacy_profile if i in stale else default_profile,
            max_num_nodes=(
                max_num_nodes
                if max_num_nodes is not None
                else PLATFORM_HOT_STORE[platforms[i]]
            ),
        )
        for i, (x, y) in enumerate(points)
    ]
    for node in nodes:
        node.rebroadcast_mode = rebroadcast_mode
    mesh = Mesh(
        conf,
        nodes,
        rng,
        hop_limit=hop_limit,
        area=area,
        extra_loss=extra_loss,
        burst_loss=burst_loss,
        burst_ms=burst_ms,
        profile=profile,
    )
    mesh.topology = resolved
    mesh.hop_assign = hop_assign

    if hop_spread:
        # Real meshes are not uniform in this. A node in the middle of a dense mesh sees everything
        # it needs at 3 or 4 hops and its owner leaves the default alone; someone on the edge turns
        # it up until they can reach the rest, and 7 is where the field guidance tops out.
        #
        # `centrality` reproduces that correlation, and in doing so makes hop limit and position
        # perfectly confounded: a table of receptions-by-hop-limit then measures position and labels it
        # hop limit, which cannot answer whether raising your own limit helps you. `random` breaks the
        # correlation on purpose. It is not how operators behave - it is the control that isolates the
        # hop limit's own effect from the siting of the nodes that happen to have raised it.
        if hop_assign == "random":
            order = list(range(node_count))
            rng.shuffle(order)
        else:
            order = sorted(range(node_count), key=lambda i: -len(mesh.neighbours[i]))
        rank = {
            node: position / max(1, node_count - 1)
            for position, node in enumerate(order)
        }
        mesh.node_hop_limit = [
            min(7, 3 + int(rank[i] * 4 + rng.random() * 1.5)) for i in range(node_count)
        ]

    by_degree = sorted(range(node_count), key=lambda i: -len(mesh.neighbours[i]))
    if role_mix:
        shares = ROLE_MIXES[role_mix] if isinstance(role_mix, str) else role_mix
        # Router-like roles go to the best-sited nodes, because that is what an operator does with
        # a hilltop. CLIENT_MUTE is drawn at random from the rest: muting a node is a decision about
        # power or noise, not about siting, and handing it to the worst-connected nodes would make
        # it look free.
        taken = 0
        for role in (ROUTER, ROUTER_LATE, CLIENT_BASE):
            want = int(round(node_count * shares.get(role, 0.0)))
            for i in by_degree[taken : taken + want]:
                nodes[i].role = role
            taken += want
        rest = by_degree[taken:]
        rng.shuffle(rest)
        muted = int(round(node_count * shares.get(CLIENT_MUTE, 0.0)))
        for i in rest[:muted]:
            nodes[i].role = CLIENT_MUTE
    else:
        taken = 0
        for fraction, role in (
            (router_fraction, ROUTER),
            (router_late_fraction, ROUTER_LATE),
            (client_base_fraction, CLIENT_BASE),
        ):
            if fraction <= 0:
                continue
            want = max(1, int(round(node_count * fraction)))
            for i in by_degree[taken : taken + want]:
                nodes[i].role = role
            taken += want

    if favourite_routers:
        # Hop preservation only fires between nodes that have favourited each other, which in the
        # field means an operator who runs both ends of a spine. Modelling it as "every router-like
        # node favourites every other" is the upper bound on how much free relaying 2.8 can do.
        spine = [i for i in range(node_count) if nodes[i].is_router_like()]
        for i in spine:
            nodes[i].favourites = {j for j in spine if j != i}
    return mesh
