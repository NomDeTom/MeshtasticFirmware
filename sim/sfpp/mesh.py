"""The radio underneath the sketch: nodes, links, airtime, collisions, managed flood.

The physics is Meshtasticator's - estimate_path_loss() for who hears whom, airtime() for how long a
packet holds the channel, and its empirical SNR-to-PER curve for marginal links - wrapped in an event
loop implementing the firmware's own rules: CAD before transmit, SNR-weighted rebroadcast delay,
duplicate suppression, and cancelling a pending rebroadcast on hearing someone else do it first.

Where each rule comes from: RadioInterface for the contention window and the retransmission timer,
MeshPacketQueue and RadioLibInterface for queue order and the deferred `tx_after` window,
FloodingRouter for who may cancel a dupe, Router::shouldDecrementHopLimit for when a hop is free,
NextHopRouter for directed delivery and its fallback to flooding.

Profile selects which release series' rules to obey - see Profile for what differs between them.
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

# The portnums CORE_PORTNUMS_ONLY lets through. Nothing the SR protocol invents is among them, so
# under this mode no node relays an advert or a replay.
CORE_PORTNUMS = frozenset({1, 3, 4, 5, 67, 70})

# From RadioInterface.h: the contention window is sized from SNR so that distant nodes - the ones
# whose rebroadcast actually extends coverage - transmit first. These are this tree's values; see
# CW_BOUNDS and SNR_BOUNDS for the earlier series.
CW_MIN, CW_MAX = 3, 8
SNR_MIN_DB, SNR_MAX_DB = -20.0, 10.0

# The release series a Profile can be named for, oldest first. A series profile carries the rules of
# that series' *final* release - 2.4 = v2.4.3, 2.5 = v2.5.23, 2.6 = v2.6.13, 2.7 = v2.7.21, 2.8 =
# this tree - so a mechanism that arrived mid-series is present in that series' profile. FEATURE_TAG
# records the release each one actually shipped in.
VERSIONS = ("2.4", "2.5", "2.6", "2.7", "2.8")

# RadioInterface.h CWmin/CWmax per series, and the SNR range getCWsize maps onto them. Both moved:
# 2.5 lowered CWmax to 7, 2.6 raised CWmin to 3 and put it back, and 2.6 also narrowed the top of
# the SNR range from 15 dB to 10, which shifts every rebroadcast delay on a strong link.
CW_BOUNDS = {"2.4": (2, 8), "2.5": (2, 7), "2.6": (3, 8), "2.7": (3, 8), "2.8": (3, 8)}
SNR_BOUNDS = {
    "2.4": (-20.0, 15.0),
    "2.5": (-20.0, 15.0),
    "2.6": (-20.0, 10.0),
    "2.7": (-20.0, 10.0),
    "2.8": (-20.0, 10.0),
}

# The earliest series whose profile carries each mechanism, and the release it shipped in. Read off
# the tags in this repository rather than remembered; a value of None means it is only in this tree.
FEATURE_TAG = {
    "core_portnums_mode": ("2.5", "v2.5.8"),
    "queue_late_first": ("2.5", "v2.5.18"),
    "late_window": ("2.5", "v2.5.18"),
    "router_late_role": ("2.5", "v2.5.18"),
    "next_hop_routing": ("2.6", "v2.6.0"),
    "client_base_role": ("2.7", "v2.7.9"),
    "role_aware_cancel": ("2.7", "v2.7.10"),
    "preserve_hops": ("2.7", "v2.7.11"),
    "hop_upgrade": ("2.7", "v2.7.13"),
    "next_hop_learning": ("2.7", "v2.7.13"),
    "traceroute_learning": ("2.7", "v2.7.13"),
    "traceroute_corroboration": ("2.8", None),
    "route_cache": ("2.8", None),
    "adopt_hop_recommendation": ("2.8", None),
    "resolve_ambiguity": ("2.8", None),
    "route_health": ("2.8", None),
    "warm_store": ("2.8", None),
    "signing": ("2.8", None),
    "hop_scaling": ("2.8", None),
    "opaque_relay": ("2.8", None),
    "congestion_clamp": ("2.8", None),
}

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

# WarmNodeStore.h: the low WARM_META_BITS of `last_heard` carry role, protected category and the
# XEdDSA-signed flag, so the timestamp left in the high bits has a 128-second resolution.
WARM_META_BITS = 7
WARM_TIME_QUANTUM_MS = (1 << WARM_META_BITS) * 1000.0


def warm_quantise(last_heard_ms):
    """Drop the bits the warm tier's metadata occupies, as warmTimeOf() does."""
    return (last_heard_ms // WARM_TIME_QUANTUM_MS) * WARM_TIME_QUANTUM_MS


# WARM_NODE_COUNT, which keys on memory class rather than on flash. The platform names here are
# flash-derived, so the two do not line up exactly: STM32WL is MEM_CLASS_TINY and has no warm tier
# at all, nRF52840 is a named case at 100, and a non-PSRAM ESP32-S3 takes the 150 of MEM_CLASS_
# MEDIUM. The 16 MB tier is taken as the PSRAM-equipped S3 that MEM_CLASS_LARGE describes, which is
# an assumption about the boards in that tier rather than something the flash size determines -
# `warm_num_nodes` overrides it outright when a sweep wants to price it the other way.
PLATFORM_WARM_STORE = {
    "stm32wl": 0,
    "esp32s3_4mb": 150,
    "nrf52840": 100,
    "esp32s3_8mb": 150,
    "esp32s3_16mb": 2000,
}

# TRAFFIC_MANAGEMENT_CACHE_SIZE, the cold tier. A key seen on the wire is cached here and can answer
# the inbound-decrypt path, but it is never authoritative: nothing routes, resolves or attributes
# identity from it. Keyed on memory class in the same way the warm tier is.
PLATFORM_COLD_CACHE = {
    "stm32wl": 0,
    "esp32s3_4mb": 500,
    "nrf52840": 250,
    "esp32s3_8mb": 500,
    "esp32s3_16mb": 2048,
}

# CryptoEngine.h and RadioInterface.h. An XEdDSA signature is 64 bytes, and the protobuf field that
# carries it costs two more. A frame is 255 bytes with a 16-byte header, so a Data payload signs only
# while it stays under 173 bytes - the gate signedDataFits() applies with the real encoder.
XEDDSA_SIGNATURE_SIZE = 64
XEDDSA_SIGNATURE_FIELD_BYTES = XEDDSA_SIGNATURE_SIZE + 2
MAX_LORA_PAYLOAD_LEN = 255
MESHTASTIC_HEADER_LENGTH = 16

# config.security.packet_signature_policy, applied by the receiver.
SIGNATURE_POLICY_COMPATIBLE = "COMPATIBLE"
SIGNATURE_POLICY_BALANCED = "BALANCED"
SIGNATURE_POLICY_STRICT = "STRICT"


def signed_data_fits(length):
    """Router.cpp signedDataFits - would this payload still fit a frame with a signature on it?"""
    return (
        length + XEDDSA_SIGNATURE_FIELD_BYTES + MESHTASTIC_HEADER_LENGTH
        <= MAX_LORA_PAYLOAD_LEN
    )


# RepeatScalingModule (branch `extra-repeats`). Cancelling a queued rebroadcast on the first heard
# copy costs delivery on the one class with no ACK behind it, so text tolerates a second copy first.
# The suppression thresholds are the module's own, and none of the three is validated.
REPEAT_TRACKER_SIZE = 8
REPEAT_THRESHOLD_TEXT = 2
REPEAT_THRESHOLD_DEFAULT = 1
BUSY_CHANNEL_UTIL_PERCENT = 10.0
BUSY_AIR_UTIL_TX_PERCENT = 4.0
BUSY_DIRECT_ACTIVE_NODES = 10

# mesh.proto portnums the transport itself reads. NodeInfo is the one that carries a public key, so
# it is where a node learns whom it can encrypt to.
NODEINFO_PORTNUM = 4
ROUTING_PORTNUM = 5  # ROUTING_APP, which is what an ACK is
TRACEROUTE_PORTNUM = 70  # TRACEROUTE_APP, whose payload is a RouteDiscovery

# RouteDiscovery's route array. A traceroute that runs out of slots stops recording hops.
TRACEROUTE_MAX_HOPS = 8
# Each hop in the route is a 4-byte NodeNum plus a byte of SNR, on top of the request's own header.
TRACEROUTE_BYTES_PER_HOP = 5
TRACEROUTE_BASE_BYTES = 12
TEXT_PORTNUMS = frozenset({1, 8})  # TEXT_MESSAGE_APP and TEXT_MESSAGE_COMPRESSED_APP

# Router.cpp:483: the hop recommendation is applied to routine device broadcasts and to nothing
# else. A text message keeps whatever hop limit its operator set.
VARIABLE_HOP_PORTNUMS = frozenset({3, 4, 67, 71})  # POSITION, NODEINFO, TELEMETRY, NEIGHBORINFO

# HopScalingModule: reporting roles stay reachable on a dense mesh whatever the histogram says.
# None of the roles this transport models maps to one of these, so the floor is zero throughout.
ROLE_HOP_FLOOR = {"TRACKER": 2, "TAK_TRACKER": 2, "SENSOR": 1}

# A Routing ACK is a two-byte payload; the header dominates it.
ACK_PAYLOAD_BYTES = 2

# NodeDB.h LastByteResolution. Callers must treat anything but UNIQUE as untrustworthy.
RESOLUTION_NONE = "none"
RESOLUTION_UNIQUE = "unique"
RESOLUTION_AMBIGUOUS = "ambiguous"

# mesh-pb-constants.h. The hot store is platform-dependent, and everything routing knows is bounded
# by it: a node cannot resolve, route to, or count a peer it has evicted. A real mesh is a mix of
# these, so scaling questions have one answer per platform rather than one overall.
PLATFORM_HOT_STORE = {
    "stm32wl": 10,  # ARCH_STM32WL
    "esp32s3_4mb": 100,  # CONFIG_IDF_TARGET_ESP32S3, flash < 7 MB
    "nrf52840": 120,  # nRF52840 and generic ESP32, the compile-time default
    "esp32s3_8mb": 200,  # flash 7-15 MB
    "esp32s3_16mb": 250,  # flash >= 15 MB
}
MAX_NUM_NODES = PLATFORM_HOT_STORE["nrf52840"]

# The same table for the earlier series, keyed by Profile.hot_store_model. Up to 2.5 the cap was a
# flat 100 for every board; 2.6 introduced the platform split with nRF52 at 80 and the ESP32-S3 flash
# tiers; this tree raised the compile-time default to 120 and dropped the separate nRF52 branch.
#
# The `nrf52840` key stands for "nRF52840 and generic ESP32", which 2.6 and 2.7 do not treat alike:
# ARCH_NRF52 takes 80 there and a generic ESP32 falls through to 100. The nRF52 value is used.
PLATFORM_HOT_STORE_BY_VERSION = {
    "flat100": dict.fromkeys(PLATFORM_HOT_STORE, 100),
    "2.6": {
        "stm32wl": 10,
        "esp32s3_4mb": 100,
        "nrf52840": 80,
        "esp32s3_8mb": 200,
        "esp32s3_16mb": 250,
    },
    "2.8": PLATFORM_HOT_STORE,
}
PLATFORM_HOT_STORE_BY_VERSION["2.7"] = PLATFORM_HOT_STORE_BY_VERSION["2.6"]

# Which board every declared hardware model is, as a hot-store size. Derived from this tree's own
# variants: each platformio.ini declares custom_meshtastic_hw_model_slug,
# custom_meshtastic_architecture and custom_meshtastic_partition_scheme, and mesh-pb-constants.h
# turns those into MAX_NUM_NODES. Regenerate after a firmware bump.
#
# Note HELTEC_V3 is an 8 MB ESP32-S3 and so gets 200 slots, not the 120 of the compile-time default.
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

    `census` maps hardware model slugs - the names the firmware puts on the wire - to counts or
    shares. An unknown slug raises rather than falling into a default bucket, so an unrecognised
    share cannot silently become a census of the default.

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


# Named mixes. `uniform` puts every node on the 120-slot default; it is a control, not a deployment.
#
# `baymesh-2026-08` is a census of 1769 nodes on the Bay Area mesh, exported from
# meshview.bayme.sh/stats on 2026-08-17 and run through census_to_mix(). 87% mapped to a board in
# this tree; the 13% that did not is PORTDUINO (operator-set cap, no fixed tier), one unknown model
# id, and a long tail reported only as "Other". The weights are over the mapped share, and it is one
# regional mesh at one moment rather than the population of all meshes.
PLATFORM_MIXES = {
    "uniform": {"nrf52840": 1.0},
    "baymesh-2026-08": {
        "nrf52840": 0.616,  # RAK4631 24%, T1000-E 8%, WIO Tracker L1 4%, T114 4%, T-Echo 2%, ...
        "esp32s3_8mb": 0.192,  # Heltec V3 13%, T-Beam S3 Core, XIAO S3, Wireless Tracker
        "esp32s3_16mb": 0.192,  # Heltec V4 10%, Station G2 5%, T-Deck 2%
    },
    # Every node on the smallest store there is. No node in the census is on this tier, so it is a
    # stress test: what routing does when almost nothing fits.
    "constrained": {"stm32wl": 1.0},
}

# Role shares from the same census (1769 nodes). TRACKER, CLIENT_HIDDEN, TAK and SENSOR together are
# ~1% and fold into CLIENT: none of them changes a rebroadcast decision, which is all a role is read
# for here.
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
    # Adversarial, and deliberately not a census of anything. CLIENT_MUTE is what keeps a real mesh
    # quiet - a fifth of Baymesh does not rebroadcast at all - so removing it entirely is the single
    # cruellest realistic change to a role mix. Everything left relays.
    "no-mute": {CLIENT: 0.81, CLIENT_BASE: 0.16, ROUTER: 0.02, ROUTER_LATE: 0.01},
    # The same, with the router share inflated: every router rebroadcasts early and without the
    # contention-window offset a client pays, so a mesh of routers is a mesh with no backoff.
    "all-routers": {ROUTER: 1.0},
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
# an overlap. LONG_SLOW at a full payload is about 6 s, so this leaves a wide margin.
MAX_AIRTIME_MS = 20000.0

# The firmware's TX queue is finite, and overflow is its only drop: setTransmitDelay reschedules a
# blocked packet indefinitely, so congestion shows up as a full queue and as latency rather than as a
# packet that evaporates. On overflow it picks which packet to lose - see replaceLowerPriorityPacket.
QUEUE_DEPTH = 16

# meshtastic_MeshPacket_Priority, only the values the queue order actually distinguishes.
PRIORITY_BACKGROUND = 10
PRIORITY_DEFAULT = 64
PRIORITY_RELIABLE = 70
PRIORITY_ACK = 120


class Profile:
    """Which firmware's rules to obey.

    Named for a release series - `2.4` through `2.8` - and carrying that series' rules as of its
    final release. `2.8` is this tree. Nodes may each run a different profile, which is how a mixed
    mesh is modelled.

    `legacy` is not a firmware version: it is the rule set this transport carried before the 2.8
    fold-in, kept so runs measured under it still reproduce. Four of its deviations were never any
    firmware's behaviour - no router offset, a continuous slot draw, a clamped contention window and
    a 400-backoff discard - so it must not be read as "2.7 and earlier". It reproduces distributions
    rather than streams: the TX queue replaced a recursive retry closure, so a seed does not
    reproduce a pre-fold-in run packet for packet.

    Individual flags stay overridable, for sweeps that turn one rule at a time.
    """

    __slots__ = (
        "name",
        "version",
        "cw_min",
        "cw_max",
        "snr_min",
        "snr_max",
        "early_rebroadcast",
        "router_offset",
        "router_cw_floor",
        "max_backoffs",
        "quantised_slots",
        "clamp_cw",
        "util_backoff",
        "queue_late_first",
        "queue_prefers_relayed",
        "role_aware_cancel",
        "router_late_role",
        "client_base_role",
        "core_portnums_mode",
        "late_window",
        "preserve_hops",
        "hop_upgrade",
        "next_hop_routing",
        "next_hop_learning",
        "traceroute_learning",
        "traceroute_corroboration",
        "route_cache",
        "resolve_ambiguity",
        "route_health",
        "reliable_retx",
        "broadcast_attempts",
        "unicast_attempts",
        "hot_store_model",
        "warm_store",
        "congestion_model",
        "congestion_clamp",
        "signing",
        "hop_scaling",
        "adopt_hop_recommendation",
        "nohop_portnums",
        "extra_repeats",
        "early_flood_on_unverified",
        "coding_rate_ladder",
        "exhaust_hops",
        "event_relay_hop_limit",
        "opaque_relay",
    )

    def __init__(self, name="2.8", **overrides):
        if name in VERSIONS:
            self._firmware(name)
        elif name == "legacy":
            self._legacy()
        else:
            known = ", ".join(VERSIONS + ("legacy",))
            raise ValueError(f"unknown profile {name!r}; expected one of {known}")
        self.name = name

        for key, value in overrides.items():
            if key not in Profile.__slots__ or key in ("name", "version"):
                raise ValueError(f"unknown profile flag {key!r}")
            setattr(self, key, value)

    def at_least(self, version):
        """Is this profile's series `version` or newer? `legacy` is older than all of them."""
        if self.version is None:
            return False
        return VERSIONS.index(self.version) >= VERSIONS.index(version)

    def _firmware(self, version):
        self.version = version
        self.cw_min, self.cw_max = CW_BOUNDS[version]
        self.snr_min, self.snr_max = SNR_BOUNDS[version]

        # RadioInterface::shouldRebroadcastEarlyLikeRouter, and the inline role test that preceded
        # it. Who skips the router offset and draws from the bottom of the window: ROUTER and
        # REPEATER up to 2.6, plus CLIENT_BASE on favourite traffic in 2.7, and ROUTER alone in 2.8
        # once REPEATER and CLIENT_BASE were taken back out.
        if version == "2.7":
            self.early_rebroadcast = "router_repeater_favourite_base"
        elif version == "2.8":
            self.early_rebroadcast = "router"
        else:
            self.early_rebroadcast = "router_repeater"

        # The 2 * CWmax * slotTime a non-early rebroadcaster waits first. Present in every series
        # here; only `legacy` lacks it.
        self.router_offset = True
        self.router_cw_floor = False
        self.max_backoffs = None
        self.quantised_slots = True
        self.clamp_cw = False
        self.util_backoff = True

        # MeshPacketQueue::CompareMeshPacketFunc. 2.4 orders a max-heap by priority alone, ties to
        # the lower id. 2.5 replaced it with a sorted insert that puts the late-transmit group last
        # and, at equal priority, prefers a packet already on the mesh over one of ours.
        self.queue_late_first = self.at_least("2.5")
        self.queue_prefers_relayed = self.at_least("2.5")

        self.router_late_role = self.at_least("2.5")
        self.client_base_role = self.at_least("2.7")
        self.core_portnums_mode = self.at_least("2.5")
        self.late_window = self.at_least("2.5")
        self.role_aware_cancel = self.at_least("2.7")
        self.preserve_hops = self.at_least("2.7")
        self.hop_upgrade = self.at_least("2.7")
        self.next_hop_routing = self.at_least("2.6")
        self.next_hop_learning = self.at_least("2.7")

        # TraceRouteModule::updateNextHops, v2.7.13: a traceroute reply teaches a next hop for
        # every node beyond the learner in the route, not just the neighbour. The corroboration
        # guard that refuses to learn unless the byte the route names is the one that actually
        # relayed the reply is only in this tree - 2.7 learns from the unauthenticated payload
        # unconditionally, so it learns more, and some of what it learns is wrong.
        self.traceroute_learning = self.at_least("2.7")
        self.traceroute_corroboration = self.at_least("2.8")
        # The TrafficManagement overflow cache, which can hold a route for a node the hot store has
        # evicted or never held.
        self.route_cache = self.at_least("2.8")

        # NodeDB::resolveUniqueLastByte. Before this tree a last-byte lookup took the first node it
        # matched; nothing asked whether a second node shared the byte. So hop preservation and
        # next-hop emission were ambiguity-blind, and got it wrong silently on a dense mesh.
        self.resolve_ambiguity = self.at_least("2.8")
        self.route_health = self.at_least("2.8")

        self.reliable_retx = True
        self.broadcast_attempts = NUM_RELIABLE_RETX
        self.unicast_attempts = (
            NUM_RELIABLE_UNICAST_ATTEMPTS if self.at_least("2.8") else NUM_RELIABLE_RETX
        )

        # mesh-pb-constants.h. 2.4 and 2.5 give every board 100 slots; 2.6 introduced the platform
        # split; this tree raised the default to 120 and sizes ESP32-S3 by flash.
        self.hot_store_model = "flat100" if not self.at_least("2.6") else version
        self.warm_store = self.at_least("2.8")

        # Default.h congestionScalingCoefficient. A flat 0.075 per node over 40 in 2.4; a
        # per-preset table in 2.5 and 2.6, which switches the throttle off entirely on the two
        # shortest presets; 2^SF / (BW_kHz * divisor) from 2.7.
        if not self.at_least("2.5"):
            self.congestion_model = "flat"
        elif not self.at_least("2.7"):
            self.congestion_model = "preset"
        else:
            self.congestion_model = "sf_bw"
        self.congestion_clamp = self.at_least("2.8")

        self.signing = self.at_least("2.8")
        self.hop_scaling = self.at_least("2.8")
        # The module's whole purpose: a node lowers its own hop limit on routine broadcasts to what
        # it estimates the mesh needs. Unconditional in the firmware wherever the module exists, so
        # it follows hop_scaling - separable only so a sweep can hold the loop open.
        self.adopt_hop_recommendation = self.at_least("2.8")
        # Portduino's nohop_ports: named portnums relayed at hop_limit 0. Operator configuration
        # rather than a release feature, so no version turns it on.
        self.nohop_portnums = frozenset()

        # Off unless the module or the build flag is on. `extra_repeats` is RepeatScalingModule,
        # which is on a branch rather than in any release, so no series turns it on by itself.
        self.extra_repeats = False
        # M4 is written and compiled out at NEXTHOP_EARLY_FLOOD_ON_UNVERIFIED 0, and the coding-rate
        # ladder is on a branch, so no release turns either on.
        self.early_flood_on_unverified = False
        self.coding_rate_ladder = False
        self.exhaust_hops = False
        self.event_relay_hop_limit = None
        self.opaque_relay = self.at_least("2.8")

    def _legacy(self):
        self.version = None
        self.cw_min, self.cw_max = 2, 8
        self.snr_min, self.snr_max = -20.0, 15.0
        self.early_rebroadcast = "router"
        self.router_offset = False
        # Pinned a router to the bottom of the window and drew from 2^CWmin, where the firmware
        # keeps a router's window SNR-derived and halves the exponent to a doubling.
        self.router_cw_floor = True
        # The pre-fold-in CSMA loop discarded a packet that could not find a clear channel within
        # 400 backoffs. No firmware does this - setTransmitDelay reschedules indefinitely - but the
        # runs measured under this profile had it, so it stays.
        self.max_backoffs = 400
        self.quantised_slots = False
        self.clamp_cw = True
        self.util_backoff = False
        self.queue_late_first = False
        self.queue_prefers_relayed = False
        self.router_late_role = False
        self.client_base_role = False
        self.core_portnums_mode = False
        self.late_window = False
        self.role_aware_cancel = False
        self.preserve_hops = False
        self.hop_upgrade = False
        self.next_hop_routing = False
        self.next_hop_learning = False
        self.traceroute_learning = False
        self.traceroute_corroboration = False
        self.route_cache = False
        self.resolve_ambiguity = False
        self.route_health = False
        self.reliable_retx = False
        self.broadcast_attempts = NUM_RELIABLE_RETX
        self.unicast_attempts = NUM_RELIABLE_RETX
        self.hot_store_model = "2.8"
        self.warm_store = False
        self.congestion_model = "sf_bw"
        self.congestion_clamp = False
        self.signing = False
        self.hop_scaling = False
        self.adopt_hop_recommendation = False
        self.nohop_portnums = frozenset()
        self.exhaust_hops = False
        self.event_relay_hop_limit = None
        self.extra_repeats = False
        self.early_flood_on_unverified = False
        self.coding_rate_ladder = False
        self.opaque_relay = False


def arduino_map(value, in_min, in_max, out_min, out_max):
    """Arduino's map(): long arithmetic, and no clamping.

    Two details decide the answer and neither is Python's default. The parameters are `long`, so a
    float SNR or utilisation truncates toward zero on the way in - -5.7 dB enters as -5. C integer
    division also truncates toward zero where Python's `//` floors, and the two disagree for every
    negative numerator: getCWsize(-25) is 0 in the firmware and -1 under `//`.

    getCWsize() takes the result as a uint8_t without constraining it, so an SNR outside
    [SNR_MIN, SNR_MAX] extrapolates off the end of the window rather than saturating at it.
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
        # byte of a 32-bit node number - so both are ambiguous whenever two known nodes share it.
        "relay_node",
        "next_hop",
        # rx_rssi / rx_snr are zero on a locally generated packet, which is how RadioLibInterface
        # tells the two apart when picking a transmit delay. Never fake them for our own traffic.
        "rx_rssi",
        "rx_snr",
        "priority",
        "want_ack",
        "xeddsa_signed",
        "pki_encrypted",
        "coding_rate",
        "route",
        # NOT ON THE WIRE. relay_node is one ambiguous byte; this is the sending node's index, kept
        # for instrumentation only so a cancellation can be attributed to the node that caused it.
        # Nothing in the transport reads it, and no decision may - resolving a relay to a node is
        # exactly the ambiguity resolve_unique_last_byte exists to model.
        "relay_index",
        # RouteDiscovery carries two arrays, not one: `route` accumulates on the way out and
        # `route_back` on the way home (mesh.proto tags 1 and 3, TraceRouteModule.cpp:377). Keeping
        # a single list conflated the legs, and on an asymmetric mesh - which this transport models
        # - a reply need not retrace the request, so return-leg relays were learned as forward hops.
        "route_back",
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
        # Set by the sender when it attached a signature, and read by every receiver's policy.
        self.xeddsa_signed = False
        # A DM encrypted to the destination's key. It carries no signature - the encryption is the
        # authentication - so every receive policy passes it through untouched.
        self.pki_encrypted = False
        # A per-packet TX coding-rate override. None uses the preset's, which is what every packet
        # does unless the retransmission ladder has stepped this one.
        self.coding_rate = None
        # RouteDiscovery's route array, on a traceroute and nothing else. None means this packet is
        # not a traceroute; a list is the hops recorded so far, in order.
        self.route = None
        self.route_back = None
        self.relay_index = None
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
        clone.xeddsa_signed = self.xeddsa_signed
        clone.pki_encrypted = self.pki_encrypted
        clone.coding_rate = self.coding_rate
        clone.route = None if self.route is None else list(self.route)
        clone.route_back = None if self.route_back is None else list(self.route_back)
        clone.relay_index = self.relay_index
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
    cannot be resolved from a relay byte, cannot hold a next hop, and does not count as online.
    """

    __slots__ = (
        "last_heard",
        "hops_away",
        "next_hop",
        "is_favourite",
        "is_ignored",
        "has_key",
        "xeddsa_signed",
    )

    def __init__(
        self,
        last_heard,
        hops_away=None,
        is_favourite=False,
        is_ignored=False,
        has_key=False,
        xeddsa_signed=False,
    ):
        self.last_heard = last_heard
        # None until we have heard a packet from this node with a usable hop count - `has_hops_away`
        # in the firmware. Zero means a direct neighbour, which is what next-hop resolution wants.
        self.hops_away = hops_away
        self.next_hop = NO_NEXT_HOP_PREFERENCE
        # Both live in the packed bitfield that replaced the separate booleans in NodeInfoLite.
        self.is_favourite = is_favourite
        # An ignored node is not a resolution candidate at all, so it can neither be routed through
        # nor collide with anyone else's last byte.
        self.is_ignored = is_ignored
        # The Curve25519 public key, as a fact rather than as bytes: without it a PKI DM to this
        # peer cannot be encrypted, and it is the one field the warm tier exists to preserve.
        self.has_key = has_key
        # Learned from verified traffic rather than from NodeInfo, so it survives a round trip
        # through the warm tier instead of being relearned from zero.
        self.xeddsa_signed = xeddsa_signed

    @property
    def is_protected(self):
        """Protected records outrank recency when the store has to give something up."""
        return self.is_favourite


class HopScaling:
    """HopScalingModule: a sampled, capped, hash-collided estimate of how far the mesh is.

    Not a histogram so much as an estimator that emits a hop-limit recommendation. Every property
    below costs it accuracy against the exhaustive count, and each is here because the difference is
    the point of modelling it at all:

    - identity is a 16-bit hash, so two nodes can share an entry
    - 128 entries, 4 bytes each, and it fills
    - only one node in `sampling_denominator` is ever admitted, chosen by hash
    - the buckets are scaled by `filtering_denominator` before the recommendation walk
    - recency is a 13-bit hourly bitmap, not a count
    - on overflow it raises the denominator and drops nodes, warning that the answer may be skewed

    So the module's per-hop counts are an estimate of the mesh, and the transport can compute the
    truth. Reporting both is what says how far apart they are.
    """

    CAPACITY = 128
    DENOM_MIN = 1
    DENOM_MAX = 128
    MAX_HOP = 7
    FILL_HIGH_PCT = 80
    FILL_LOW_PCT = 20
    FILTER_DENOM_HOLD_ROLLS = 13
    HOURS_TRACKED = 13
    RUNS_PER_HOUR = 12
    TARGET_AFFECTED_NODES = 40
    MAX_TARGET_NODES = 80
    POLITENESS_DENOM = 4
    POLITENESS_GENEROUS = 4
    POLITENESS_DEFAULT = 2
    POLITENESS_STRICT = 1
    ACTIVITY_WEIGHT_SCALE = 10
    ACTIVITY_WEIGHT_GENEROUS_MAX_NUMER = 9
    ACTIVITY_WEIGHT_STRICT_MIN_NUMER = 12

    __slots__ = (
        "hash_seed",
        "entries",
        "sampling_denominator",
        "filtering_denominator",
        "hold_rolls_remaining",
        "denominator_history",
        "last_per_hop",
        "last_total",
        "last_scaled_per_hop",
        "last_suggested_hop",
        "polite_numer",
        "rolls",
        "dropped_full",
    )

    def __init__(self, hash_seed=0):
        self.hash_seed = hash_seed & 0xFFFF
        # hash -> [hops_away, seen bitmap], in insertion order, capped at CAPACITY.
        self.entries = {}
        self.sampling_denominator = self.DENOM_MIN
        self.filtering_denominator = self.DENOM_MIN
        self.hold_rolls_remaining = 0
        self.denominator_history = [0] * self.HOURS_TRACKED
        self.last_per_hop = [0] * (self.MAX_HOP + 1)
        self.last_total = 0
        self.last_scaled_per_hop = [0] * (self.MAX_HOP + 1)
        self.last_suggested_hop = self.MAX_HOP
        self.polite_numer = self.POLITENESS_DEFAULT
        self.rolls = 0
        self.dropped_full = 0

    def hash_node_id(self, node_num):
        """Knuth's multiplicative hash, folded to 16 bits and seeded per node."""
        return (((node_num * 2654435761) >> 16) & 0xFFFF) ^ self.hash_seed

    @staticmethod
    def passes_filter(node_hash, denominator):
        """Subsample in hash space rather than by node number, so the choice is stable."""
        return denominator > 0 and (node_hash & (denominator - 1)) == 0

    @property
    def fill_percentage(self):
        return len(self.entries) * 100 // self.CAPACITY

    def sample(self, node_num, hops):
        """samplePacketForHistogram, called for every packet heard over the air."""
        node_hash = self.hash_node_id(node_num)
        if not self.passes_filter(node_hash, self.sampling_denominator):
            return False
        hops = min(max(0, hops), self.MAX_HOP)
        entry = self.entries.get(node_hash)
        if entry is not None:
            entry[0] = hops
            entry[1] |= 1  # markCurrentHour
            return True
        if self.fill_percentage >= self.FILL_HIGH_PCT:
            self.trim_if_needed()
        if len(self.entries) < self.CAPACITY:
            self.entries[node_hash] = [hops, 1]
            return True
        # Full at the coarsest denominator there is; the recommendation is skewed from here on.
        self.dropped_full += 1
        return False

    def trim_if_needed(self):
        """Drop everything unseen for 13 hours, then halve the sample rate if it is still full."""
        self.entries = {
            node_hash: entry for node_hash, entry in self.entries.items() if entry[1]
        }
        if (
            self.fill_percentage < self.FILL_HIGH_PCT
            or self.sampling_denominator >= self.DENOM_MAX
        ):
            return
        self.sampling_denominator = min(self.sampling_denominator * 2, self.DENOM_MAX)
        self.filtering_denominator = max(
            self.filtering_denominator, self.sampling_denominator
        )
        self.hold_rolls_remaining = self.FILTER_DENOM_HOLD_ROLLS
        self.denominator_history = [
            max(d, self.filtering_denominator) for d in self.denominator_history
        ]
        self.entries = {
            node_hash: entry
            for node_hash, entry in self.entries.items()
            if self.passes_filter(node_hash, self.sampling_denominator)
        }

    def roll_hour(self):
        """The hourly pass: summarise, recommend, then shift every bitmap along by one hour."""
        self.denominator_history = [
            self.filtering_denominator
        ] + self.denominator_history[:-1]

        per_hop = [0] * (self.MAX_HOP + 1)
        total = 0
        hourly_raw = [0] * self.HOURS_TRACKED
        for node_hash, (hops, seen) in self.entries.items():
            for hour in range(self.HOURS_TRACKED):
                if (seen & (1 << hour)) and self.passes_filter(
                    node_hash, self.denominator_history[hour]
                ):
                    hourly_raw[hour] += 1
            if not self.passes_filter(node_hash, self.filtering_denominator):
                continue
            if seen:
                per_hop[hops] += 1
                total += 1
        self.last_per_hop = per_hop
        self.last_total = total

        # How fast the mesh is turning over decides how generous the walk is allowed to be.
        recent = hourly_raw[0] + hourly_raw[1]
        older = hourly_raw[1] + hourly_raw[2]
        if older > 1 and recent > 1:
            scaled_recent = recent * self.ACTIVITY_WEIGHT_SCALE
            if scaled_recent < older * self.ACTIVITY_WEIGHT_GENEROUS_MAX_NUMER:
                self.polite_numer = self.POLITENESS_GENEROUS
            elif scaled_recent > older * self.ACTIVITY_WEIGHT_STRICT_MIN_NUMER:
                self.polite_numer = self.POLITENESS_STRICT
            else:
                self.polite_numer = self.POLITENESS_DEFAULT
        else:
            self.polite_numer = self.POLITENESS_DEFAULT

        self.last_suggested_hop = self._walk(per_hop, total)
        self.last_scaled_per_hop = [
            count * self.filtering_denominator for count in per_hop
        ]

        # Scale down when the sample has thinned out, holding the filtering denominator for 13
        # rolls so the buckets it scaled are not reinterpreted under a different one.
        if total * 100 < self.CAPACITY * self.FILL_LOW_PCT:
            if self.sampling_denominator > self.DENOM_MIN:
                self.sampling_denominator //= 2
        if self.filtering_denominator > self.sampling_denominator:
            if self.hold_rolls_remaining > 0:
                self.hold_rolls_remaining -= 1
            if self.hold_rolls_remaining == 0:
                self.filtering_denominator = max(
                    self.filtering_denominator // 2, self.sampling_denominator
                )

        for entry in self.entries.values():
            entry[1] = (entry[1] << 1) & 0x1FFF
        self.rolls += 1
        return self.last_suggested_hop

    def _walk(self, per_hop, total):
        """The recommendation: the first hop that reaches enough nodes, plus one if it is cheap.

        `enough` is 40 nodes after scaling by the filtering denominator. The extension to the next
        hop is allowed when the nodes it would add still leave the total inside a budget that runs
        from 40 to 80, scaled by how politely the mesh is behaving.
        """
        if total <= 0:
            return self.MAX_HOP
        suggested = self.MAX_HOP
        cumulative = 0
        for hop in range(self.MAX_HOP + 1):
            cumulative += per_hop[hop] * self.filtering_denominator
            if cumulative >= self.TARGET_AFFECTED_NODES:
                suggested = hop
                break
        if suggested < self.MAX_HOP:
            at_next = per_hop[suggested + 1] * self.filtering_denominator
            gap = self.MAX_TARGET_NODES - self.TARGET_AFFECTED_NODES
            if (cumulative + at_next) * self.POLITENESS_DENOM <= (
                self.TARGET_AFFECTED_NODES * self.POLITENESS_DENOM
                + gap * self.polite_numer
            ):
                suggested += 1
        return suggested


class WarmEntry:
    """One 40-byte `WarmNodeEntry`: node number, last_heard, and a Curve25519 public key.

    The role, a protected category and an XEdDSA-signed flag are packed into the low seven bits of
    `last_heard`, which is why warm recency is quantised to 128 seconds. That coarseness is
    load-bearing rather than cosmetic: the tier is LRU-ordered by this field, so two nodes heard
    within the same 128-second window are indistinguishable to eviction.
    """

    __slots__ = ("last_heard", "has_key", "role", "xeddsa_signed", "is_protected")

    def __init__(
        self, last_heard, has_key=False, role=CLIENT, xeddsa_signed=False, is_protected=False
    ):
        self.last_heard = warm_quantise(last_heard)
        self.has_key = has_key
        self.role = role
        self.xeddsa_signed = xeddsa_signed
        self.is_protected = is_protected


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

    __slots__ = ("packet", "tx_after", "sent", "backoffs")

    def __init__(self, packet, tx_after=0.0):
        self.packet = packet
        self.tx_after = tx_after
        self.sent = False
        # Only read under the legacy profile, where exceeding a cap discarded the packet.
        self.backoffs = 0


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
        "warm",
        "cold_keys",
        "max_num_nodes",
        "warm_num_nodes",
        "cold_cache_size",
        "signature_policy",
        "history_max",
        "platform",
        "profile",
        "online",
        "route_health",
        "reliable",
        "util_ring",
        "util_index",
        "util_epoch",
        "tx_ring",
        "tx_index",
        "tx_epoch",
        "repeat_counts",
        "repeat_slot",
        "route_cache",
        "hop_scaling",
        "observed_hops",
        "required_hop",
    )

    def __init__(
        self,
        index,
        x,
        y,
        role=CLIENT,
        node_num=None,
        max_num_nodes=MAX_NUM_NODES,
        warm_num_nodes=0,
        cold_cache_size=0,
        signature_policy=SIGNATURE_POLICY_COMPATIBLE,
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

        # A real 32-bit node number, so two nodes can share a last byte as they do on a real mesh.
        # Routing detects that collision rather than being safe against it by construction, so the
        # detection cannot be exercised without real numbers.
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
        # version.
        self.profile = profile if profile is not None else Profile("2.8")
        # False once the node has been taken down. An offline node neither transmits nor receives,
        # and its NodeDB goes stale in everyone else's store rather than being deleted from it.
        self.online = True
        self.max_num_nodes = max_num_nodes
        # The warm tier: node index -> WarmEntry, for peers evicted from the hot store. A node is
        # in one or the other, never both. Zero slots means the platform has no warm tier.
        self.warm = {}
        self.warm_num_nodes = warm_num_nodes
        # The cold tier: keys seen on the wire, usable on the inbound-decrypt path and never
        # authoritative, so nothing routes or resolves from it.
        self.cold_keys = set()
        self.cold_cache_size = cold_cache_size
        # config.security.packet_signature_policy. Only affects what this node accepts, never what
        # it sends: a node signs whatever it can whichever policy it runs.
        self.signature_policy = signature_policy
        self.history_max = packet_history_max(max_num_nodes)
        self.route_health = {}  # destination index -> RouteHealth
        self.reliable = {}  # packet id -> pending retransmission record

        # AirTime's 6 x 10 s ring of channel-busy milliseconds. Counts our own transmissions and
        # every packet we could hear, which is what channelUtilizationPercent() reports.
        self.util_ring = [0.0] * 6
        self.util_index = 0
        self.util_epoch = 0.0
        # AirTime's TX ring: 60 x 1 minute of our own transmit time, which is a different window
        # from the channel-utilisation ring above.
        self.tx_ring = [0.0] * 60
        self.tx_index = 0
        self.tx_epoch = 0.0
        # RepeatScalingModule's ring of (sender, id) -> heard duplicates, eight entries deep and
        # replaced round-robin. Small enough to thrash on a busy mesh, which is the point of
        # modelling its size rather than a dict.
        self.repeat_counts = [None] * REPEAT_TRACKER_SIZE
        self.repeat_slot = 0
        # TrafficManagementModule's overflow cache of next-hop hints: node index -> relay byte. It
        # is much larger than the hot store and is not bounded by it, so it can hold a route for a
        # node the NodeDB has evicted or never admitted.
        self.route_cache = {}
        # The firmware's estimator, when this node's release has it, and the exhaustive count the
        # transport can take for free. Reporting both is what says how far apart they are.
        self.hop_scaling = None
        self.observed_hops = {}
        # getLastRequiredHop: HOP_MAX until the module has rolled at least once with something in
        # it, then the walk's answer raised to this role's floor.
        self.required_hop = HopScaling.MAX_HOP

    @property
    def relay_byte(self):
        """NodeDB::getLastByteOfNodeNum - all of our number that fits in `relay_node`.

        A low byte of zero is sent as 0xFF, because 0 is the NO_RELAY_NODE sentinel. So one node
        number in 256 is not identified by its own last byte, and 0xFF answers for twice as many
        nodes as any other value.
        """
        return (self.node_num & 0xFF) or 0xFF

    def position(self):
        return (self.x, self.y)

    def is_router_like(self):
        return self.role in ROUTER_LIKE

    @property
    def direct_neighbours(self):
        """Peers in the hot store recorded at zero hops away.

        Stands in for HopScalingModule::getLastPerHopCounts().perHop[0], which is a sampled and
        capped estimate of the same quantity - so this is the exact figure the estimator is trying
        to approximate rather than the estimate itself.
        """
        return sum(1 for record in self.nodedb.values() if record.hops_away == 0)

    # ---- NodeDB (hot store) ------------------------------------------------------------

    def update_from(self, peer, now, hops_away=None):
        """NodeDB::updateFrom - note that we heard a peer, and how far away it is.

        `hops_away` stays None until a packet arrives with a usable hop count, matching
        `has_hops_away`: "we have never established this" is a different answer from "zero hops",
        and next-hop resolution turns on the difference.

        Admitting a node the warm tier holds empties that slot and restores what it kept - the key
        and the XEdDSA-signed bit. `next_hop` and `hops_away` were never in the warm record, so a
        re-admitted node is routed to by flooding until its route is learned again.
        """
        record = self.nodedb.get(peer)
        if record is None:
            warm = self.warm_take(peer)
            record = NodeRecord(
                now,
                hops_away,
                is_favourite=peer in self.favourites,
                has_key=warm.has_key if warm else False,
                xeddsa_signed=warm.xeddsa_signed if warm else False,
            )
            self.nodedb[peer] = record
        else:
            record.last_heard = now
            if hops_away is not None:
                record.hops_away = hops_away
        return record

    def trim_nodedb(self):
        """Demote the stalest unprotected record when the store overflows.

        `demoteOldestHotNodesToWarm`: protection outranks recency, and within a class the
        most-recently-heard survives. The evicted record is offered to the warm tier, which keeps
        the identity and the key but not the routing - so this is still how a learned route dies
        without any expiry being involved. See the four lifetimes in Mesh.get_next_hop.

        Returns (record, warm outcome) for each record dropped from the hot store.
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
            record = self.nodedb.pop(victim)
            dropped.append((record, self.warm_absorb(victim, record)))
        return dropped

    # ---- warm tier (WarmNodeStore) ----------------------------------------------------

    def warm_absorb(self, peer, record):
        """WarmNodeStore::absorb - keep an evicted node's identity, key and role.

        40 bytes: node number, last_heard, and a Curve25519 public key. The key is what the tier
        exists for - expensive to re-learn, where the rest rebuilds from traffic in seconds - so a
        keyless candidate never displaces a keyed entry, and eviction takes the oldest keyless
        entry before it takes any keyed one.

        What is not carried over is the routing: `next_hop` and `hops_away` are hot-store fields
        and do not survive demotion.
        """
        if not self.warm_num_nodes:
            return "no_tier"
        entry = WarmEntry(
            last_heard=record.last_heard,
            has_key=record.has_key,
            role=self.role,
            xeddsa_signed=record.xeddsa_signed,
            is_protected=record.is_protected,
        )
        if peer in self.warm:
            self.warm[peer] = entry
            return "stored"
        outcome = "stored"
        if len(self.warm) >= self.warm_num_nodes:
            keyless = [p for p, e in self.warm.items() if not e.has_key]
            if not keyless and not entry.has_key:
                # A keyless candidate cannot displace a keyed entry, so it is simply not kept.
                return "refused"
            victim = min(
                keyless or list(self.warm), key=lambda p: self.warm[p].last_heard
            )
            del self.warm[victim]
            outcome = "replaced"
        self.warm[peer] = entry
        return outcome

    def warm_take(self, peer):
        """WarmNodeStore::take - re-admission to the hot store empties the warm slot.

        A node lives in hot or warm, never both. What comes back is last_heard, the key, the role
        and the XEdDSA-signed bit; `next_hop` and `hops_away` were never stored and start again at
        nothing, so a re-admitted node is routed to by flooding until it is relearned.
        """
        return self.warm.pop(peer, None)

    def warm_key(self, peer):
        """`copyPublicKey`: hot first, then warm. Both are authoritative.

        Our own record is in the hot store with our own key, so a node can always verify a
        signature of its own - which is what it hears when its relay comes back.
        """
        if peer == self.index:
            return True
        record = self.nodedb.get(peer)
        if record is not None and record.has_key:
            return True
        entry = self.warm.get(peer)
        if entry is not None and entry.has_key:
            return True
        return False

    def knows_key(self, peer):
        """`copyPublicKeyForDecrypt`: the authoritative tiers, or a key-proven cold-cache entry.

        The cold tier is a cache for the inbound-decrypt path and is never authoritative, so it can
        answer this question and cannot be used to claim a node's identity.
        """
        return self.warm_key(peer) or peer in self.cold_keys

    def knows(self, peer):
        """Is this peer in the hot store at all? KNOWN_ONLY and LOCAL_ONLY ask exactly this."""
        return peer in self.nodedb

    def num_online(self, now):
        """NodeDB::getNumOnlineMeshNodes - bounded by the store *and* by a two-hour window.

        Not read by the transport, but it is the input to the congestion coefficient, which is
        therefore bounded by the store rather than by mesh size.
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

    def log_tx_airtime(self, now, ms):
        """AirTime's second ring: our own transmissions only, per minute over the last hour.

        A separate structure from channel utilisation, and over a different window - sixty minutes
        against sixty seconds - so the two answer different questions. This one is what the duty
        cycle is enforced against.
        """
        elapsed = int((now - self.tx_epoch) // 60000.0)
        if elapsed > 0:
            if elapsed >= len(self.tx_ring):
                self.tx_ring = [0.0] * len(self.tx_ring)
                self.tx_index = 0
            else:
                for _ in range(elapsed):
                    self.tx_index = (self.tx_index + 1) % len(self.tx_ring)
                    self.tx_ring[self.tx_index] = 0.0
            self.tx_epoch += elapsed * 60000.0
        self.tx_ring[self.tx_index] += ms

    def utilization_tx_percent(self, now):
        """AirTime::utilizationTXPercent - the share of the last hour we spent transmitting."""
        self.log_tx_airtime(now, 0.0)
        return (sum(self.tx_ring) / (len(self.tx_ring) * 60000.0)) * 100.0


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

    The minimum spacing stops stacked nodes making the mesh look better connected than a real
    deployment.
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

    Dense pockets joined by a handful of long links, rather than the even neighbourhoods a uniform
    field gives. Nine in ten nodes belong to a town; the rest hold the mesh together.
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

    Hop limit binds far harder than in a square, and placement is nearly one-dimensional.
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

    The well-connected nodes are all in one place, so archives placed among them are maximally
    redundant with each other.
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

    Under `mixed` a sweep samples across mesh shapes rather than across draws of one shape, so a
    placement rule that only holds on uniform points shows up as an artefact of the generator.
    """
    if topology == "mixed":
        topology = sorted(TOPOLOGIES)[rng.randrange(len(TOPOLOGIES))]
    return TOPOLOGIES[topology](count, area, rng, min_dist), topology


# Where a node actually is, as a gain offset in dB on every link it takes part in. These are the
# deployments people describe having, and the spread between them is larger than most parameters this
# simulator sweeps: a roof node and a basement node differ by 26 dB, which is more than the whole span
# from SHORT_FAST to VERY_LONG_SLOW sensitivity.
#
# Values are deliberately round rather than measured - they are a stated assumption, not data. A roof
# node clears local clutter and gets height; a desk node is indoors with a window; a pocket node pays
# body loss and no height; a basement node is below grade, which is the worst case people actually run.
#
# NOT FROM THE FIRMWARE, AND NOT MEASURED. The firmware has no concept of siting - it knows tx_power
# and a GPS position, and grep for antenna_gain across src/ and the protobufs returns nothing. Even the
# simulator's own antenna gain is a single global Config.GL, not per node. These four numbers are a
# modelling assumption of mine, and the 26 dB between roof and basement is wide enough to move results,
# so they want replacing with measurements rather than defending.
SITINGS = {
    "roof": 6.0,
    "desk": 0.0,
    "pocket": -10.0,
    "basement": -20.0,
}

# Named mixes of the above. `local-typical` is the small deployment a hobbyist actually has; `event`
# is a field of people carrying nodes; `backbone` is a line of roof-mounted routers.
SITING_MIXES = {
    "uniform": {"desk": 1.0},
    "local-typical": {"roof": 0.15, "desk": 0.45, "pocket": 0.3, "basement": 0.1},
    "event": {"roof": 0.05, "desk": 0.15, "pocket": 0.8},
    "backbone": {"roof": 0.8, "desk": 0.2},
    # Adversarial. `basement-heavy` is the estate of indoor nodes nobody has re-sited; `worst-case`
    # is every node badly placed, which is not a deployment but is the floor a design has to clear.
    "basement-heavy": {"desk": 0.2, "pocket": 0.3, "basement": 0.5},
    "worst-case": {"pocket": 0.2, "basement": 0.8},
}


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
        siting_gain=None,
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
        # Per-node gain offset in dB, from build()'s siting mix. Read by _build_links, so it has
        # to arrive with the constructor rather than being set afterwards - links are computed once.
        self.siting_gain = (
            list(siting_gain) if siting_gain is not None else [0.0] * len(nodes)
        )
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
            "cancelled_by_weaker_relay": 0,
            "cancelled_reach_lost": 0,
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
            # No candidate at all, as against two of them: a byte this node has not learned.
            "next_hop_unresolved": 0,
            "next_hop_fallbacks": 0,
            "route_expired_ttl": 0,
            "route_expired_failures": 0,
            "routes_lost_to_eviction": 0,
            "nodedb_evictions": 0,
            # Demoted to the warm tier rather than forgotten, and re-admitted from it.
            "warm_demotions": 0,
            "warm_promotions": 0,
            "warm_evictions": 0,
            # A PKI DM that could not be encrypted because no tier held the peer's key.
            "dm_blocked_no_key": 0,
            # Signing, and the three ways a receive policy refuses a packet.
            "packets_signed": 0,
            "packets_too_large_to_sign": 0,
            "dropped_unsigned_strict": 0,
            "dropped_unverifiable": 0,
            "dropped_downgrade": 0,
            "signature_bootstraps": 0,
            # A duplicate heard and tolerated rather than cancelled on, and the times the mesh was
            # too busy for that to be allowed.
            "extra_repeats_tolerated": 0,
            "extra_repeats_suppressed": 0,
            # M4 flooding a retry early, and retransmission chains that never got an ACK.
            "early_floods": 0,
            # Routing ACKs generated at a destination, and deliveries they confirmed.
            "acks_sent": 0,
            "acks_delivered": 0,
            # Traceroute: what was asked, what came back, what it taught, and what the
            # corroboration guard refused to learn from.
            "traceroutes_sent": 0,
            "traceroute_replies": 0,
            "traceroute_routes_learned": 0,
            "traceroute_uncorroborated": 0,
            # The overflow cache: hints written, and routes served from it that the hot store
            # could not have answered.
            "route_cache_writes": 0,
            "route_cache_hits": 0,
            # The hop-scaling estimator: packets it admitted, hourly rolls, and nodes it had to
            # drop because it was full at the coarsest sampling it has.
            "hop_samples": 0,
            "hop_rolls": 0,
            "hop_dropped_full": 0,
            # The recommendation actually taking effect, and the zero-hop list doing the same.
            "hop_limit_lowered": 0,
            "hop_limit_zeroed": 0,
            "reliable_retx": 0,
            "reliable_failures": 0,
            "opaque_relays": 0,
            "hops_exhausted": 0,
            "rebroadcast_suppressed_by_mode": 0,
            "nodes_taken_down": 0,
            "nodes_brought_up": 0,
            "links_severed": 0,
            "sends_while_offline": 0,
            "dropped_to_backoff_cap": 0,
            # A role the node's own firmware series does not have, so it runs as CLIENT instead.
            "role_unavailable_in_version": 0,
        }
        self.airtime_by_kind = {}
        # Filled by build() when per-node hop limits are in play; None means everyone uses the same.
        self.node_hop_limit = None
        self.on_receive = (
            None  # callback(node, packet, rssi, snr) for the campaign's app layer
        )
        # Per-node samples of every adaptive quantity, filled by start_adaptive_trace().
        self.adaptive_trace = []
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
                siting = self.siting_gain[i] + self.siting_gain[j]
                skew = (
                    self.rng.gauss(
                        conf.MODEL_ASYMMETRIC_LINKS_MEAN,
                        conf.MODEL_ASYMMETRIC_LINKS_STDDEV,
                    )
                    if conf.MODEL_ASYMMETRIC_LINKS
                    else 0.0
                )
                self.rssi[i][j] = base + siting + skew
                self.rssi[j][i] = base + siting - skew

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

    def airtime_ms(self, length, coding_rate=None):
        """How long a payload holds the channel. A slower coding rate is a longer packet."""
        import lib.phy as phy

        p = self.conf.current_preset
        return phy.airtime(
            self.conf, p["sf"], coding_rate or p["cr"], length, p["bw"]
        )

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
        """`myRegion->wideLora` - a property of the configured region, not of the bandwidth.

        The vendored region table carries the flag, so this asks the same question the firmware
        does. Selecting on bandwidth instead would take the 2.4 GHz CAD term everywhere, putting the
        LONG_FAST slot at 40.4 ms rather than 28.1.
        """
        return bool(self.conf.REGION.get("wide_lora", False))

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

        Under the legacy profile this stays a continuous draw, which removes a class of collision
        the firmware produces routinely: two nodes can only pick the same slot if slots are discrete.
        """
        if self.nodes[node].profile.quantised_slots:
            bound = int(bound)
            return self.rng.randrange(0, bound) if bound > 0 else 0
        return self.rng.uniform(0, bound) if bound > 0 else 0.0

    def tx_delay_msec(self, node):
        """RadioInterface::getTxDelayMsec - the delay for something we composed ourselves.

        The window is sized from channel utilisation, so a busy mesh backs off harder.
        """
        profile = self.nodes[node].profile
        if not profile.util_backoff:
            return self.slot_time_ms() * self.rng.uniform(1, 4)
        util = self.nodes[node].channel_utilization_percent(self.now)
        cw = arduino_map(util, 0, 100, profile.cw_min, profile.cw_max)
        return self._draw_slots(node, 2**cw) * self.slot_time_ms()

    def _rebroadcasts_early(self, node, packet=None):
        """RadioInterface::shouldRebroadcastEarlyLikeRouter - who skips the router offset.

        This tree says ROUTER and nothing else. Up to 2.6 the test was inline in
        getTxDelayMsecWeighted and admitted REPEATER as well; 2.7 added CLIENT_BASE for traffic to or
        from one of its favourites, then 2.8 removed both again.
        """
        me = self.nodes[node]
        mode = me.profile.early_rebroadcast
        if me.role == ROUTER:
            return True
        if mode == "router":
            return False
        if me.role == REPEATER:
            return True
        if mode == "router_repeater_favourite_base" and me.role == CLIENT_BASE:
            return packet is not None and self._favourite_traffic(node, packet)
        return False

    def tx_delay_weighted(self, node, snr, packet=None):
        """RadioInterface::getTxDelayMsecWeighted - the delay for relaying someone else's packet.

        High SNR means a large window, because a node that heard the packet loudly is close to the
        sender and its relay adds least. Everyone who is not an early rebroadcaster waits out the
        whole router window first, so routers go first.
        """
        profile = self.nodes[node].profile
        cw = self.cw_size(node, snr)
        slot = self.slot_time_ms()
        if self._rebroadcasts_early(node, packet):
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
        airtime = int(self.airtime_ms(packet.length, packet.coding_rate))
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

        The radio holds the packet rather than discarding it, so a congested mesh shows up as
        latency and as a full queue. Queue overflow is the only drop.

        `token` is the caller's handle on a packet it may want to cancel: a dict with `sent`,
        `event` and `entry` keys.
        """
        radio = self.nodes[node]
        if not radio.online:
            self.stats["sends_while_offline"] += 1
            return None
        entry = QueueEntry(packet)
        if len(radio.queue) >= QUEUE_DEPTH:
            # Something is dropped either way; only which packet is in question.
            # MeshPacketQueue::enqueue sets `dropped` before it knows, and txDrop counts that.
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
        rather lose, in three passes, each giving up the back of the queue as the packet furthest
        from being sent. Only reachable once the queue holds a mix of ready and deferred packets.
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

            # 3. Nothing ready to give up. Drop the back if its deadline has already passed: a
            #    ready packet always beats an overdue deferred one.
            #
            #    The wait-time comparison below is unreachable today. send() is the only caller and
            #    always arrives with a freshly built QueueEntry, whose tx_after is 0.0, so the first
            #    branch always wins. It is kept because the firmware's own comparison is between two
            #    overdue packets and a future caller could pass a deferred entry; nothing here
            #    currently constructs one.
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
        """MeshPacketQueue::enqueue.

        From 2.5 this is an upper_bound insert into a sorted list: the deferred group sorts behind
        the ready one always, the ready group is priority order, and at equal priority a packet
        already on the mesh sorts ahead of one we originated. Within the deferred group it is
        deadline order. Keeping the groups apart is what makes the late-rebroadcast window work: a
        clamped packet goes to the back and stays there until its time comes.

        2.4 has no late group and no relayed-first tie-break: it holds a max-heap ordered by
        priority alone, ties to the lower packet id. Pop order under that comparator is a total
        order, so a sorted insert reproduces the sequence the heap dequeues.
        """
        profile = radio.profile
        if not profile.queue_late_first:
            position = 0
            while position < len(radio.queue):
                other = radio.queue[position].packet
                if other.priority < entry.packet.priority or (
                    other.priority == entry.packet.priority
                    and other.id > entry.packet.id
                ):
                    break
                position += 1
            radio.queue.insert(position, entry)
            return

        if entry.tx_after:
            position = len(radio.queue)
            while position > 0:
                prev = radio.queue[position - 1]
                if not prev.tx_after or prev.tx_after <= entry.tx_after:
                    break
                position -= 1
        else:
            ours = entry.packet.origin == radio.index
            position = 0
            while position < len(radio.queue):
                other = radio.queue[position]
                if other.tx_after or other.packet.priority < entry.packet.priority:
                    break
                if (
                    profile.queue_prefers_relayed
                    and not ours
                    and other.packet.priority == entry.packet.priority
                    and other.packet.origin == radio.index
                ):
                    break
                position += 1
        radio.queue.insert(position, entry)

    def set_transmit_delay(self, node):
        """RadioLibInterface::setTransmitDelay - decide when to next look at the queue.

        A packet we relayed carries the RSSI and SNR it arrived with, which is how the firmware
        tells it from something we composed: a locally generated packet has both at zero, and the
        radio's noise floor offset guarantees a received one never does.
        """
        radio = self.nodes[node]
        if not radio.queue:
            return
        entry = radio.queue[0]
        packet = entry.packet
        if entry.tx_after:
            add = (
                self.tx_delay_weighted(node, packet.rx_snr, packet)
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
            self._arm(node, self.now + self.tx_delay_weighted(node, packet.rx_snr, packet))

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
            entry.backoffs += 1
            cap = radio.profile.max_backoffs
            if cap is not None and entry.backoffs > cap:
                # The pre-fold-in defect, faithfully reproduced: give up and drop it.
                radio.queue.pop(0)
                radio.pending.pop(entry.packet.id, None)
                self.stats["queue_drops"] += 1
                self.stats["dropped_to_backoff_cap"] += 1
                if radio.queue:
                    self.set_transmit_delay(node)
                return
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
        duration = self.airtime_ms(packet.length, packet.coding_rate)
        radio = self.nodes[node]
        radio.busy_until = self.now + duration
        radio.log_airtime(self.now, duration)
        radio.log_tx_airtime(self.now, duration)
        packet.relay_node = radio.relay_byte
        packet.relay_index = node  # instrumentation only; see the slot comment
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

        # A radio cannot hear while it is keying up. A router relays everything it hears, so it
        # spends a large share of the time deaf, and the node beside it - same traffic, fewer
        # relays - is the better listener.
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
                or self._lost_to_phy(rssi, packet.length, packet.coding_rate)
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

    def _lost_to_phy(self, rssi, length, coding_rate=None):
        """The empirical SNR-to-PER curve. More redundancy survives a worse link."""
        import lib.radio_loss as radio_loss

        if not self.conf.PHY_LOSS_MODEL_ENABLED:
            return False
        return radio_loss.payload_is_lost(
            self.conf,
            rssi,
            coding_rate or self.conf.current_preset["cr"],
            length,
            self.rng.random(),
        )

    # ---- breaking the mesh -------------------------------------------------------------

    def take_down(self, index):
        """Turn a node off. It stops transmitting and stops hearing anything.

        Not a deletion: every other node keeps its NodeDB record for this one and keeps believing
        whatever it last learned, including a next hop pointing through it. Failure is not
        broadcast, so the gap between what the mesh believes and what is true has to be modelled.
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

        Two halves that each keep working, diverge, and reconcile when the link comes back.
        """
        self.rssi[a][b] = -999.0
        self.rssi[b][a] = -999.0
        if b in self.neighbours[a]:
            self.neighbours[a].remove(b)
        if a in self.neighbours[b]:
            self.neighbours[b].remove(a)

    def partition(self, group):
        """Sever every link crossing out of `group`, splitting the mesh in two.

        Returns the number of links cut; zero means the group was already disconnected.
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

        Hopcroft-Tarjan over the link graph. Killing random nodes mostly does nothing on a
        well-connected mesh; these are the ones whose loss changes its shape.
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

        The modes are ordered by how targeted they are. `random` is the null hypothesis and usually
        does nothing on a well-connected mesh. `bridge` is the sharpest, but a mesh at degree 8 has
        no articulation points to take, so it falls back to `degree` and reports that it did.

        `split` removes no node: it cuts every link across a geographic line, and is the only mode
        guaranteed to partition a healthy mesh.
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
        never expires, never fails and never shows up as a fallback - it stops existing.
        """
        node = self.nodes[rx]
        was_warm = peer in node.warm
        record = node.update_from(peer, self.now, hops_away=hops_away)
        if was_warm:
            self.stats["warm_promotions"] += 1
        for dropped, warm_outcome in node.trim_nodedb():
            self.stats["nodedb_evictions"] += 1
            if dropped.next_hop != NO_NEXT_HOP_PREFERENCE:
                self.stats["routes_lost_to_eviction"] += 1
            if warm_outcome in ("stored", "replaced"):
                self.stats["warm_demotions"] += 1
            if warm_outcome == "replaced":
                # The tier was full, so keeping this identity cost another one.
                self.stats["warm_evictions"] += 1
        return record

    def _signature_policy_admits(self, rx, packet):
        """Router::checkXeddsaReceivePolicy - does this node accept the packet as it arrived?

        COMPATIBLE takes anything. STRICT takes only what it can verify. BALANCED sits between:
        it accepts unsigned traffic in general, but drops an unsigned broadcast from a node it has
        already seen sign, when that payload would have fitted a signature.

        The size test is the sharp edge. It is the mirror of the sender's gate, so a payload big
        enough to push a signature over the frame is exempt from the downgrade rule - which is what
        an attacker inflates to evade it, and what makes an honest unsigned broadcast get dropped
        if a signable type ever grows past the budget.
        """
        node = self.nodes[rx]
        if not node.profile.signing or packet.pki_encrypted:
            # PKI encryption authenticates the sender on its own, so no policy inspects it.
            return True
        policy = node.signature_policy
        if packet.xeddsa_signed:
            if packet.origin == rx:
                # Our own relay coming back. We hold our own key, so it verifies, and there is no
                # peer record to update - a node does not keep a NodeDB entry about itself here.
                return True
            if node.warm_key(packet.origin):
                # Verification succeeds: nothing here forges a signature. Remember that the sender
                # signs, which is what the downgrade rule later reads. The policy runs before the
                # store is updated for this packet, so the record is created here as
                # getOrCreateMeshNode does.
                self.note_heard(rx, packet.origin).xeddsa_signed = True
                return True
            if packet.portnum == NODEINFO_PORTNUM:
                # verifyFirstContactNodeInfo: a signed NodeInfo carries the sender's own key, and
                # the node number is a CRC32 of that key, so the packet verifies against itself and
                # nobody can claim another node's number with it. This is how a mesh bootstraps at
                # all under STRICT - without it the NodeInfo that would teach the key is itself
                # dropped for want of the key.
                record = self.note_heard(rx, packet.origin)
                record.has_key = True
                record.xeddsa_signed = True
                self._cache_cold_key(rx, packet.origin)
                self.stats["signature_bootstraps"] += 1
                return True
            # A signature we have no key to check. Strict refuses to guess.
            if policy == SIGNATURE_POLICY_STRICT:
                self.stats["dropped_unverifiable"] += 1
                return False
            return True
        if policy == SIGNATURE_POLICY_STRICT:
            self.stats["dropped_unsigned_strict"] += 1
            return False
        if policy == SIGNATURE_POLICY_COMPATIBLE:
            return True
        record = node.nodedb.get(packet.origin)
        known_signer = record is not None and record.xeddsa_signed
        if (
            known_signer
            and packet.destination == BROADCAST
            and signed_data_fits(packet.length)
        ):
            self.stats["dropped_downgrade"] += 1
            return False
        return True

    def _cache_cold_key(self, rx, peer):
        """The cold tier: a key seen on the wire, kept for the decrypt path only.

        Bounded like the cache it stands for, and evicted at random rather than by recency, since
        nothing here tracks the per-entry timestamps that would make an LRU meaningful.
        """
        node = self.nodes[rx]
        if not node.cold_cache_size or peer == rx:
            return
        node.cold_keys.add(peer)
        while len(node.cold_keys) > node.cold_cache_size:
            node.cold_keys.pop()

    # ---- last-byte resolution (NodeDB::resolveLastByte) --------------------------------

    def resolve_last_byte(self, rx, relay_byte, require_direct_neighbour=False):
        """NodeDB::resolveLastByte. Returns (status, peer) - UNIQUE, AMBIGUOUS or NONE.

        `relay_node` and `next_hop` are one byte of a 32-bit node number, so on a mesh of any size
        they collide. Callers treat anything but UNIQUE as the safe branch: decrement the hop limit,
        flood instead of unicasting, learn nothing. The two failures are kept apart because they say
        different things - AMBIGUOUS is a dense mesh, NONE is a mesh this node has not learned.

        Two gates decide the candidate set, and both shrink it well below "every node with this
        byte". The candidate gate is the hot store, minus ourselves and any ignored node: an evicted
        or never-heard peer is not a candidate. The relevance gate asks whether the peer is a
        plausible relay for this question - on the send path a direct neighbour heard within two
        hours, otherwise a direct neighbour, a favourite or a router-like node.

        So a smaller store makes the byte less ambiguous rather than more, which is the opposite of
        a birthday bound taken over the whole mesh. A large mesh costs knowledge, not resolution.

        Only this tree scans for a second candidate. Under 2.6 and 2.7 the lookup takes the first
        node it matches and the caller is never told it guessed, which is modelled by returning
        UNIQUE on that first match.

        One fidelity gap: the firmware reads the role recorded in its own `NodeInfoLite`, learned
        from a NodeInfo exchange this model does not run, so the role gate here reads the peer's
        true role and is better informed than the firmware's.
        """
        if not relay_byte:
            return RESOLUTION_NONE, None
        me = self.nodes[rx]
        cutoff = self.now - NEXTHOP_NEIGHBOR_FRESH_MSEC
        match = None
        for peer, record in me.nodedb.items():
            if peer == rx or record.is_ignored:
                continue
            if self.nodes[peer].relay_byte != relay_byte:
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
            if not me.profile.resolve_ambiguity:
                return RESOLUTION_UNIQUE, peer
            if match is not None:
                # A second relevant candidate shares the byte. Nothing later can resolve that.
                self.stats["next_hop_ambiguous"] += 1
                return RESOLUTION_AMBIGUOUS, None
            match = peer
        if match is None:
            self.stats["next_hop_unresolved"] += 1
            return RESOLUTION_NONE, None
        return RESOLUTION_UNIQUE, match

    def resolve_unique_last_byte(self, rx, relay_byte, require_direct_neighbour=False):
        """NodeDB::resolveUniqueLastByte - the peer when exactly one answers to the byte, else None."""
        status, peer = self.resolve_last_byte(rx, relay_byte, require_direct_neighbour)
        return peer if status == RESOLUTION_UNIQUE else None

    # ---- routers (FloodingRouter.cpp, NextHopRouter.cpp) -------------------------------

    def is_rebroadcaster(self, rx, packet=None):
        """FloodingRouter::isRebroadcaster, plus the portnum gates the modes imply."""
        node = self.nodes[rx]
        if node.role == CLIENT_MUTE or node.rebroadcast_mode == REBROADCAST_NONE:
            return False
        if (
            packet is not None
            and node.rebroadcast_mode == REBROADCAST_CORE_PORTNUMS_ONLY
            and node.profile.core_portnums_mode
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
        job: the role exists to be the copy that goes out regardless.
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
        sender's hop budget. The first hop always pays.

        The two implementations identify the previous relay differently. This tree resolves the
        relay byte and preserves the hop only when exactly one node answers to it, so ambiguity
        charges the hop. 2.7 walks its own store for favourited router-like nodes and preserves on
        the first byte match, which on a dense mesh gives a free hop to a node that merely shares a
        byte with a favourite.
        """
        node = self.nodes[rx]
        if not node.profile.preserve_hops:
            return True
        if packet.hops_taken() == 0:
            return True
        if not node.is_router_like():
            return True
        if node.profile.resolve_ambiguity:
            resolved = self.resolve_unique_last_byte(rx, packet.relay_node)
            if resolved is None:
                return True
            if resolved in node.favourites and self.nodes[resolved].is_router_like():
                self.stats["hop_limit_preserved"] += 1
                return False
            return True
        for peer in node.favourites:
            if peer not in node.nodedb or peer == rx:
                continue
            if not self.nodes[peer].is_router_like():
                continue
            if self.nodes[peer].relay_byte == packet.relay_node:
                self.stats["hop_limit_preserved"] += 1
                return False
        return True

    def get_next_hop(self, rx, destination, relay_byte):
        """NextHopRouter::getNextHop. None means flood.

        A stored route decays: unconfirmed for half an hour, or three failed directed deliveries in
        a row, and it is cleared rather than trusted for one more DM. We also never hand a packet
        back to the node that just relayed it, and never emit a byte that no longer resolves to a
        single reachable neighbour.

        Decay needs a health record that still matches the stored byte, and `route_health` is capped
        at ROUTE_HEALTH_MAX with LRU eviction. A destination whose record has been evicted therefore
        keeps its next_hop with no TTL and no failure count left to age it. That is the firmware's
        behaviour, not a shortcut here: NextHopRouter.cpp:297 guards the same way on the same cap of
        32, and its comment is explicit that "a next_hop set by another path (e.g. TraceRouteModule)
        with no matching record is left authoritative". On a mesh holding more than 32 live routes
        it means a dead hop can be trusted until something else routes around it - a real property
        worth knowing when reading a large-mesh result, and reproduced here deliberately.

        The route lives in the destination's own hot-store record, as `NodeInfoLite.next_hop` does,
        so evicting a peer forgets the way to it - a separate cost from the relay byte's ambiguity.
        """
        if destination == BROADCAST or not self.nodes[rx].profile.next_hop_routing:
            return None
        node = self.nodes[rx]
        record = node.nodedb.get(destination)
        stored = record.next_hop if record is not None else NO_NEXT_HOP_PREFERENCE
        if not stored:
            return self._next_hop_from_cache(rx, destination, relay_byte)
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

    def _next_hop_from_cache(self, rx, destination, relay_byte):
        """The overflow cache, consulted once the hot store has no route to offer.

        This is the case the cache exists for: a node past the hot store's capacity, whose route
        traceroute or an ACK taught us and whose NodeInfoLite has since been evicted. A stale hint
        is cleared here rather than tried, and a hint that no longer resolves to one reachable
        neighbour is not emitted at all.
        """
        node = self.nodes[rx]
        if not node.profile.route_cache:
            return None
        hint = node.route_cache.get(destination)
        if not hint or hint == relay_byte:
            return None
        health = node.route_health.get(destination)
        if (
            health is not None
            and health.last_next_hop == hint
            and (self.now - health.learned_at) >= ROUTE_TTL_MSEC
        ):
            node.route_cache.pop(destination, None)
            node.route_health.pop(destination, None)
            self.stats["route_expired_ttl"] += 1
            return None
        if (
            self.resolve_unique_last_byte(rx, hint, require_direct_neighbour=True)
            is None
        ):
            return None
        self.stats["route_cache_hits"] += 1
        return hint

    def note_route_learned(self, rx, destination, next_hop):
        """NextHopRouter::noteRouteLearned - refresh a route, without forgiving it.

        The failure count is cleared only when the hop itself changes. Re-learning the *same* hop
        keeps the count, so an asymmetric reverse path that keeps re-teaching a dead forward hop
        still ages that hop out instead of resetting the counter on every lesson.
        """
        node = self.nodes[rx]
        health = node.route_health.get(destination)
        if health is None:
            if len(node.route_health) >= ROUTE_HEALTH_MAX:
                oldest = min(
                    node.route_health, key=lambda d: node.route_health[d].learned_at
                )
                node.route_health.pop(oldest)
            node.route_health[destination] = RouteHealth(self.now, next_hop)
            return
        if health.last_next_hop != next_hop:
            health.last_next_hop = next_hop
            health.consecutive_failures = 0
        health.learned_at = self.now

    def note_route_success(self, rx, destination):
        """NextHopRouter::noteRouteSuccess - a directed delivery worked, so the route is fresh.

        Only a route we actually learned has health to refresh; nothing is created here.
        """
        health = self.nodes[rx].route_health.get(destination)
        if health is None:
            return
        health.consecutive_failures = 0
        health.learned_at = self.now

    def route_is_verified(self, rx, destination):
        """M4's test: a route we learned, never since failed, and not yet stale."""
        health = self.nodes[rx].route_health.get(destination)
        return (
            health is not None
            and health.consecutive_failures == 0
            and (self.now - health.learned_at) < ROUTE_TTL_MSEC
        )

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

        if not heard.opaque and not self._signature_policy_admits(rx, heard):
            # Router::perhapsDecode returns DECODE_POLICY_REJECT and handleReceived skips the
            # packet, so it is neither delivered nor relayed. It deliberately does not cancel a
            # queued rebroadcast of the same packet either: a policy rejection is attacker-
            # controlled input, and letting it cancel would hand anyone a way to silence a relay.
            return

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
        record = self.note_heard(rx, packet.origin, hops_away=packet.hops_taken())
        # NodeDB.cpp:3730 samples every packet heard over the air, clamping a negative hop count to
        # zero rather than rejecting it - the conservative direction for a recommendation.
        hops = max(0, packet.hops_taken())
        node.observed_hops[packet.origin] = hops
        if node.hop_scaling is not None:
            if node.hop_scaling.sample(self.nodes[packet.origin].node_num, hops):
                self.stats["hop_samples"] += 1
        if packet.portnum == NODEINFO_PORTNUM:
            # NodeInfo is what carries a peer's public key, so it is the only thing that makes a PKI
            # DM to that peer possible. The cold cache keeps a copy for the decrypt path, bounded
            # and non-authoritative, so a peer evicted from both other tiers can still be read.
            record.has_key = True
            self._cache_cold_key(rx, packet.origin)
        fresh = SeenRecord(packet.origin, packet.hop_limit, packet.next_hop, self.now)
        fresh.note_relayer(packet.relay_node)
        node.remember(packet.id, fresh)

        self._sniff_ack_or_reply(rx, heard)
        self._traceroute_learn(rx, heard)
        if self.on_receive is not None:
            self.on_receive(node, packet, rssi, snr)
        self._perhaps_ack(rx, heard)
        self._perhaps_traceroute_reply(rx, heard)
        self.perhaps_rebroadcast(rx, heard)

    # ---- extra repeats (RepeatScalingModule) -------------------------------------------

    def _mesh_too_busy_for_extra_repeats(self, rx):
        """meshTooBusyForExtraRepeats: three unvalidated constants, any one of which forces 1.

        Channel utilisation over 10%, our own transmit share of the last hour over 4%, or more than
        ten direct neighbours. The neighbour count is HopScalingModule::getLastPerHopCounts()'s
        zero-hop bucket, which is a sampled and hourly-rolled estimate rather than the exact count -
        so on a mesh whose sampling denominator has climbed, this threshold reads low and extra
        repeats stay switched on longer than the exact count would allow. Falls back to the hot
        store's neighbour count when the node's release has no such module.
        """
        node = self.nodes[rx]
        if node.channel_utilization_percent(self.now) > BUSY_CHANNEL_UTIL_PERCENT:
            return True
        if node.utilization_tx_percent(self.now) > BUSY_AIR_UTIL_TX_PERCENT:
            return True
        neighbours = (
            node.hop_scaling.last_per_hop[0]
            if node.hop_scaling is not None
            else node.direct_neighbours
        )
        if neighbours > BUSY_DIRECT_ACTIVE_NODES:
            return True
        return False

    def _dupe_cancel_threshold(self, rx, packet):
        """RepeatScalingModule::getDupeCancelThreshold. Text tolerates one heard copy; nothing else.

        An undecodable packet is classified from the plaintext header instead: flooded traffic is
        treated as text-like, directed traffic as not.
        """
        if packet.opaque:
            threshold = (
                REPEAT_THRESHOLD_TEXT
                if packet.next_hop == NO_NEXT_HOP_PREFERENCE
                else REPEAT_THRESHOLD_DEFAULT
            )
        elif packet.portnum in TEXT_PORTNUMS:
            threshold = REPEAT_THRESHOLD_TEXT
        else:
            threshold = REPEAT_THRESHOLD_DEFAULT
        if threshold > 1 and self._mesh_too_busy_for_extra_repeats(rx):
            self.stats["extra_repeats_suppressed"] += 1
            return REPEAT_THRESHOLD_DEFAULT
        return threshold

    def _should_cancel_dupe(self, rx, packet):
        """RepeatScalingModule::shouldCancelDupe - count this copy, and say whether to give up.

        Without the module the answer is always yes on the first duplicate, which is what every
        release does today.
        """
        node = self.nodes[rx]
        if not node.profile.extra_repeats:
            return True
        threshold = self._dupe_cancel_threshold(rx, packet)
        key = (packet.origin, packet.id)
        for index, entry in enumerate(node.repeat_counts):
            if entry is not None and entry[0] == key:
                count = entry[1] + 1
                if count >= threshold:
                    node.repeat_counts[index] = None
                    return True
                node.repeat_counts[index] = (key, count)
                self.stats["extra_repeats_tolerated"] += 1
                return False
        if 1 >= threshold:
            return True
        node.repeat_counts[node.repeat_slot] = (key, 1)
        node.repeat_slot = (node.repeat_slot + 1) % REPEAT_TRACKER_SIZE
        self.stats["extra_repeats_tolerated"] += 1
        return False

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

        if not self.role_allows_canceling_dupe(rx, packet):
            self.stats["cancel_refused_by_role"] += 1
        elif self._should_cancel_dupe(rx, packet):
            entry = self._cancel_sending(rx, packet.id)
            if entry is not None:
                node.pending.pop(packet.id, None)
                self.stats["rebroadcasts_cancelled"] += 1
                # Who silenced whom. A relay heard by fewer nodes than the one it just cancelled
                # has suppressed a broadcast that would have travelled further than its own - the
                # badly-sited node in the middle of a well-connected cluster. Counting it separates
                # "duplicate suppression working" from "duplicate suppression backfiring".
                sender = packet.relay_index
                if sender is not None and sender != rx:
                    if len(self.neighbours[sender]) < len(self.neighbours[rx]):
                        self.stats["cancelled_by_weaker_relay"] += 1
                        self.stats["cancelled_reach_lost"] += len(
                            self.neighbours[rx]
                        ) - len(self.neighbours[sender])

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
        self._record_traceroute_hop(rx, relay)
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

    # ---- traceroute (TraceRouteModule) -------------------------------------------------

    def send_traceroute(self, src, destination):
        """Start a route discovery. Each relay appends itself; the destination replies.

        The reply is what teaches anything: a node that finds itself in the returned route learns a
        next hop for every node beyond it, not just for its neighbour.
        """
        packet = self.originate(
            src,
            TRACEROUTE_PORTNUM,
            TRACEROUTE_BASE_BYTES,
            kind="traceroute",
            destination=destination,
        )
        if packet is not None:
            packet.route = []
            self.stats["traceroutes_sent"] += 1
        return packet

    def _record_traceroute_hop(self, rx, relay):
        """alterReceivedProtobuf: a relay writes itself into the route before passing it on.

        TraceRouteModule picks the array by direction - `route` while the request travels out,
        `route_back` once the destination has answered - so a reply that comes home by a different
        path cannot append onto the outbound leg. `request_id` is what marks a packet as the reply.
        """
        if relay.route is None:
            return
        outbound = not relay.request_id
        leg = relay.route if outbound else relay.route_back
        if leg is None or len(leg) >= TRACEROUTE_MAX_HOPS:
            return
        leg.append(rx)
        # The packet grows with every hop either leg records, which is the airtime a traceroute
        # costs; both arrays ride the same RouteDiscovery.
        recorded = len(relay.route) + len(relay.route_back or [])
        relay.length = TRACEROUTE_BASE_BYTES + recorded * TRACEROUTE_BYTES_PER_HOP

    def _perhaps_traceroute_reply(self, rx, packet):
        """The destination answers, carrying the route the request accumulated on the way out."""
        if packet.route is None or packet.request_id or packet.destination != rx:
            return None
        reply = self.originate(
            rx,
            TRACEROUTE_PORTNUM,
            packet.length,
            kind="traceroute_reply",
            destination=packet.origin,
            request_id=packet.id,
        )
        if reply is not None:
            reply.route = list(packet.route)
            # The way home is recorded separately, so the forward path stays exactly what the
            # request measured however the reply is routed back.
            reply.route_back = []
            self.stats["traceroute_replies"] += 1
        return reply

    def _traceroute_learn(self, rx, packet):
        """TraceRouteModule::updateNextHops on a returning reply.

        If the route is A->B->C->D, then B learns C as the next hop for C and for D, and C learns D
        for D. A node that is the original sender takes the first entry; a node that is last in the
        route takes the responder itself.

        The guard is the part worth having. The route array is unauthenticated payload, so this
        tree refuses to learn from it unless the node it names as our next hop is the one that
        actually relayed this reply to us, and treats a missing relay byte - an MQTT-sourced packet
        with no RF corroboration - as unlearnable. 2.7 has the learning without the guard, so a
        forged reply could point any node's next_hop anywhere.
        """
        node = self.nodes[rx]
        if not node.profile.traceroute_learning or packet.route is None:
            return
        if not packet.request_id:
            return  # a request on its way out teaches nobody
        route = packet.route
        if packet.destination == rx:
            index = 0
        elif rx in route:
            index = route.index(rx) + 1
        else:
            return
        next_hop_node = route[index] if index < len(route) else packet.origin
        if next_hop_node == rx:
            return
        next_hop_byte = self.nodes[next_hop_node].relay_byte

        if node.profile.traceroute_corroboration:
            if not packet.relay_node or next_hop_byte != packet.relay_node:
                self.stats["traceroute_uncorroborated"] += 1
                return

        for target in list(route[index:]) + [packet.origin]:
            if target == rx:
                continue
            self._maybe_set_next_hop(rx, target, next_hop_byte)

    def _maybe_set_next_hop(self, rx, target, next_hop_byte):
        """maybeSetNextHop: the hot store when it holds the target, and the overflow cache always.

        Traceroute is the highest-confidence source there is - a full known route - so the hint is
        mirrored into the overflow cache even for a target the hot store has no room for.
        """
        node = self.nodes[rx]
        record = node.nodedb.get(target)
        if record is not None and record.next_hop != next_hop_byte:
            record.next_hop = next_hop_byte
            self.note_route_learned(rx, target, next_hop_byte)
            self.stats["traceroute_routes_learned"] += 1
        if node.profile.route_cache and node.cold_cache_size:
            if node.route_cache.get(target) != next_hop_byte:
                self.stats["route_cache_writes"] += 1
            node.route_cache[target] = next_hop_byte
            while len(node.route_cache) > node.cold_cache_size:
                node.route_cache.pop(next(iter(node.route_cache)))

    def hop_limit_for_response(self, rx, packet):
        """RoutingModule::getHopLimitForResponse - answer over the distance the request came.

        A reply does not need the sender's whole hop budget: it needs the hops the request used,
        plus a margin because the way back may differ. A request that arrived with a hop_start of
        zero is answered directly and not relayed at all.
        """
        limit = self.hop_limit_for(rx)
        hops_used = packet.hops_taken()
        if hops_used >= 0:
            if hops_used > limit:
                return hops_used
            if packet.hop_start == 0:
                return 0
            if hops_used + 2 < limit:
                return hops_used + 2
        return limit

    def _apply_hop_policy(self, radio, packet):
        """Router.cpp:483 and the Portduino zero-hop list, applied to a packet we are composing.

        The recommendation reaches routine device broadcasts and nothing else - position,
        telemetry, nodeinfo, neighbourinfo - and can only lower the limit, never raise it above
        what the operator configured. A text message is untouched, which bounds how far the
        feedback loop can reach.

        A zero-hop portnum is capped afterwards. Both paths reduce hop_start by the same amount as
        hop_limit, so `hops_away` computed by every downstream receiver stays honest; changing only
        the limit would silently corrupt the very histogram the recommendation came from.
        """
        profile = radio.profile
        if (
            profile.adopt_hop_recommendation
            and packet.destination == BROADCAST
            and packet.portnum in VARIABLE_HOP_PORTNUMS
            and radio.required_hop < packet.hop_limit
        ):
            self._lower_hop_limit(packet, radio.required_hop)
            self.stats["hop_limit_lowered"] += 1
        if packet.portnum in profile.nohop_portnums and packet.hop_limit > 0:
            self._lower_hop_limit(packet, 0)
            self.stats["hop_limit_zeroed"] += 1

    @staticmethod
    def _lower_hop_limit(packet, limit):
        """capEventRelayHops' arithmetic: take the same amount off hop_start as off hop_limit."""
        reduction = packet.hop_limit - limit
        packet.hop_start = max(0, packet.hop_start - reduction)
        packet.hop_limit = limit

    def _perhaps_ack(self, rx, packet):
        """ReliableRouter::sendAckNak at the destination of a packet that asked to be acknowledged.

        Only the addressee answers, and only for a request: a packet that is itself a response gets
        a hop-limited ACK, so the far end stops retransmitting without the ACK itself flooding.
        """
        node = self.nodes[rx]
        if not node.profile.reliable_retx or not packet.want_ack:
            return None
        if packet.destination != rx or packet.origin == rx:
            return None
        is_response = bool(packet.request_id or packet.reply_id)
        hop_limit = 0 if is_response else self.hop_limit_for_response(rx, packet)
        ack = self.originate(
            rx,
            ROUTING_PORTNUM,
            ACK_PAYLOAD_BYTES,
            kind="ack",
            hop_limit=hop_limit,
            destination=packet.origin,
            priority=PRIORITY_ACK,
            request_id=packet.id,
        )
        if ack is not None:
            self.stats["acks_sent"] += 1
        return ack

    def _sniff_ack_or_reply(self, rx, packet):
        """NextHopRouter::sniffReceived - learn a route from a delivery that demonstrably worked.

        Only a relayer that also carried the original teaches us anything, and only when its byte
        resolves to one node. Without the first gate the learned hop need never have touched this
        path; without the second, every future DM aims at whichever node shares a last byte.
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

        if packet.destination == rx and packet.request_id:
            # An ACK for something we sent came back. The route we used is working, so refresh its
            # freshness and clear the failure count - without creating health for a route we never
            # learned - and stop retransmitting, which is the whole point of having asked.
            self.note_route_success(rx, packet.origin)
            if packet.portnum == ROUTING_PORTNUM:
                self.stats["acks_delivered"] += 1
            # Often already stopped: overhearing our own packet relayed is an implicit ACK, and it
            # usually beats the real one back.
            self._stop_retransmission(rx, packet.request_id)

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
        attempt = record["initial"] - record["left"] + 1
        if radio.profile.coding_rate_ladder:
            # Branch `CRCRRCRRR`: a retry that already failed once should not go out identical.
            # Base, then one step slower, then 4/8 - keyed by (from, id) so every copy of the same
            # retransmission picks up the same rate.
            retry.coding_rate = self._ladder_coding_rate(attempt)
        if packet.destination != BROADCAST and record["left"] == 1:
            # Last directed try. The route has not worked; record the failure, clear it, and let
            # this attempt flood, which is the only thing left that can still deliver.
            self._fall_back_to_flooding(node, packet, retry)
        elif (
            packet.destination != BROADCAST
            and radio.profile.early_flood_on_unverified
            and retry.next_hop != NO_NEXT_HOP_PREFERENCE
            and not self.route_is_verified(node, packet.destination)
        ):
            # M4, off in the firmware: a route that is not proven healthy does not get a second
            # directed attempt. Start flooding one retry sooner, which trades airtime for recovery
            # latency and leaves a fresh, never-failed route on the unchanged path.
            self._fall_back_to_flooding(node, packet, retry)
            self.stats["early_floods"] += 1
        record["left"] -= 1
        self.stats["reliable_retx"] += 1
        self.send(node, retry)
        record["token"] = self.at(
            self.now + self.retransmission_msec(node, packet),
            lambda: self._do_retransmission(node, packet_id),
        )

    def _fall_back_to_flooding(self, node, packet, retry):
        """Clear the route in the packet, in the hot store, and in the health table, then flood."""
        self.note_route_failure(node, packet.destination)
        dest_record = self.nodes[node].nodedb.get(packet.destination)
        if dest_record is not None:
            dest_record.next_hop = NO_NEXT_HOP_PREFERENCE
        retry.next_hop = NO_NEXT_HOP_PREFERENCE
        self.stats["next_hop_fallbacks"] += 1

    def _ladder_coding_rate(self, attempt):
        """`if (retransmissionAttempt >= 2) return 8;` else base + 1 on the first retry."""
        base = self.conf.current_preset["cr"]
        if attempt >= 2:
            return 8
        if attempt == 1:
            return min(8, base + 1)
        return base

    def start_hop_scaling(self, first_roll_ms=None):
        """Arm the hourly pass on every node that has the module.

        The module runs RUNS_PER_HOUR times an hour and rolls once, so the recommendation moves on
        an hourly cadence however busy the mesh is. Each node's roll is offset, since nothing
        synchronises boot times.
        """
        hour = 3600_000.0
        for node in self.nodes:
            if node.hop_scaling is None:
                continue
            start = (
                self.rng.uniform(0, hour) if first_roll_ms is None else first_roll_ms
            )

            def roll(index=node.index, at=start):
                target = self.nodes[index]
                if target.hop_scaling is None:
                    return
                target.hop_scaling.roll_hour()
                # lastRequiredHop = max(suggested, roleFloor), and stays at HOP_MAX until the
                # histogram has something in it to walk.
                suggested = (
                    target.hop_scaling.last_suggested_hop
                    if target.hop_scaling.rolls > 0 and target.hop_scaling.entries
                    else HopScaling.MAX_HOP
                )
                target.required_hop = max(suggested, ROLE_HOP_FLOOR.get(target.role, 0))
                self.stats["hop_rolls"] += 1
                self.stats["hop_dropped_full"] = sum(
                    n.hop_scaling.dropped_full
                    for n in self.nodes
                    if n.hop_scaling is not None
                )
                self.at(self.now + hour, roll)

            self.at(start, roll)

    def start_adaptive_trace(self, interval_ms=1800_000.0, generator=None):
        """Sample every adaptive quantity per node on a timer, and keep the series.

        An end-state mean cannot tell a value that settled from one still swinging between two,
        and every quantity here is now in a feedback loop: the hop recommendation feeds what gets
        sent, which feeds the histogram it came from; the congestion coefficient feeds how often a
        node sends, which feeds how many peers everyone hears. Sampling per node over time is what
        makes the difference visible, so it is a prerequisite for reading any of these results
        rather than a follow-up to them.
        """
        self.adaptive_trace = []

        def sample():
            hour = self.now / 3600_000.0
            for node in self.nodes:
                row = {
                    "hours": round(hour, 3),
                    "node": node.index,
                    "required_hop": node.required_hop,
                    "hop_limit": self.hop_limit_for(node.index),
                    "neighbours": node.direct_neighbours,
                    "known": len(node.nodedb),
                    "online_seen": node.num_online(self.now),
                    "channel_util": round(
                        node.channel_utilization_percent(self.now), 2
                    ),
                }
                if node.hop_scaling is not None:
                    row["suggested_hop"] = node.hop_scaling.last_suggested_hop
                    row["entries"] = len(node.hop_scaling.entries)
                    row["sampling_denominator"] = node.hop_scaling.sampling_denominator
                if generator is not None:
                    row["congestion"] = round(generator.node_congestion(node.index), 3)
                self.adaptive_trace.append(row)
            self.at(self.now + interval_ms, sample)

        self.at(0.0, sample)

    def hop_report(self, index):
        """Truth, observation and estimate side by side for one node.

        `truth` is the topological distance to every reachable node, which no device can know.
        `observed` is every hop count this node actually saw, exhaustive and exact. `estimated` is
        what the firmware's own structure would report - sampled, capped at 128 entries, hashed
        into collisions and scaled back up by the sampling denominator.

        All three are indexed by `hops_away`, where a direct neighbour is zero: getHopsAway is
        hop_start - hop_limit, so a packet that has not been relayed reads 0. A BFS distance counts
        the same neighbour as 1, so truth is shifted down to match rather than being reported in
        units nothing else uses.
        """
        node = self.nodes[index]
        truth = [0] * (HopScaling.MAX_HOP + 1)
        for peer, hops in self.hops_from([index]).items():
            if peer != index and 0 < hops <= HopScaling.MAX_HOP + 1:
                truth[hops - 1] += 1
        observed = [0] * (HopScaling.MAX_HOP + 1)
        for hops in node.observed_hops.values():
            observed[min(hops, HopScaling.MAX_HOP)] += 1
        report = {
            "node": index,
            "truth": truth,
            "truth_total": sum(truth),
            "observed": observed,
            "observed_total": sum(observed),
        }
        if node.hop_scaling is not None:
            module = node.hop_scaling
            report.update(
                {
                    "estimated": list(module.last_scaled_per_hop),
                    "estimated_total": module.last_total * module.filtering_denominator,
                    "entries": len(module.entries),
                    "sampling_denominator": module.sampling_denominator,
                    "filtering_denominator": module.filtering_denominator,
                    "suggested_hop": module.last_suggested_hop,
                    "rolls": module.rolls,
                    "dropped_full": module.dropped_full,
                }
            )
        return report

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
        pki=False,
    ):
        """Inject a packet from a node's application layer, as if it had composed it.

        Mirrors Router::send: we add our own packet to the history first, so the copies we hear
        coming back are recognised as our own, and set the next hop before it goes out.

        `pki` marks a DM that has to be encrypted to the destination's public key. Without a key in
        any tier the packet is never composed, which is what evicting a peer actually costs: not a
        worse route to it, but no conversation with it until its NodeInfo is heard again.
        """
        radio = self.nodes[node]
        if not radio.online:
            # The radio is off, so nothing is composed. Returning a packet anyway would let the
            # caller register a message that never existed.
            self.stats["sends_while_offline"] += 1
            return None
        if pki and destination != BROADCAST and not radio.knows_key(destination):
            self.stats["dm_blocked_no_key"] += 1
            return None
        signed = False
        if radio.profile.signing and not pki and destination == BROADCAST:
            # Router.cpp:1145. Unencrypted broadcasts from a keyed node are signed - a unicast only
            # when the operator is licensed, which is not modelled - and only while the payload
            # still fits a frame with 66 more bytes on it.
            if signed_data_fits(length):
                signed = True
                length += XEDDSA_SIGNATURE_FIELD_BYTES
                self.stats["packets_signed"] += 1
            else:
                self.stats["packets_too_large_to_sign"] += 1
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
        self._apply_hop_policy(radio, packet)
        packet.xeddsa_signed = signed
        packet.pki_encrypted = bool(pki) and destination != BROADCAST
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
                    radio.profile.broadcast_attempts
                    if destination == BROADCAST
                    else radio.profile.unicast_attempts
                ),
            )
        self.send(node, packet)
        return packet


def assign_platforms(node_count, platform_mix, rng):
    """Draw a board for every node from a named mix.

    Drawn rather than striped, so the small-store nodes are not evenly spaced by construction. A
    mesh whose one STM32WL sits on the only bridge is a case striping would never produce.
    """
    if platform_mix in PLATFORM_HOT_STORE:
        return [platform_mix] * node_count  # a single-board mesh, named directly
    if platform_mix not in PLATFORM_MIXES:
        raise ValueError(f"unknown platform mix {platform_mix!r}")
    weights = PLATFORM_MIXES[platform_mix]
    names = sorted(weights)
    return rng.choices(names, weights=[weights[n] for n in names], k=node_count)


def assign_sitings(node_count, siting_mix, rng):
    """Draw a siting for every node from a named mix, or name one siting for the whole mesh.

    Drawn rather than striped, for the reason assign_platforms is: a mesh whose one basement node
    sits on the only bridge is a case striping would never produce.
    """
    if siting_mix in SITINGS:
        return [siting_mix] * node_count
    if siting_mix not in SITING_MIXES:
        raise ValueError(f"unknown siting mix {siting_mix!r}")
    weights = SITING_MIXES[siting_mix]
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
    old_profile="legacy",
    router_late_fraction=0.0,
    client_base_fraction=0.0,
    role_mix=None,
    favourite_routers=False,
    rebroadcast_mode=REBROADCAST_ALL,
    max_num_nodes=None,
    warm_num_nodes=None,
    signature_policy=SIGNATURE_POLICY_COMPATIBLE,
    platform_mix="uniform",
    siting_mix="uniform",
    role_placement="degree",
):
    """A mesh with positions drawn from `rng` and a share of the nodes promoted to ROUTER.

    Routers are chosen by degree rather than at random: a deployment puts the repeater on the hill,
    and choosing them randomly would understate how much a flood depends on a few well-sited nodes.
    ROUTER_LATE and CLIENT_BASE are drawn from the same ranking, below the plain routers.
    """
    points, resolved = place(topology, node_count, area, rng, min_dist)
    # Real node numbers, so two nodes can share a last byte as they do on a real mesh; sequential
    # ids would hide the ambiguity path entirely.
    node_nums = [rng.randrange(1, 1 << 32) for _ in range(node_count)]
    # A node's hot store is a property of the board, not of where it sits. `max_num_nodes` overrides
    # the mix outright, so a sweep can hold the store fixed and vary something else.
    platforms = assign_platforms(node_count, platform_mix, rng)
    # Where each node physically is, as a gain offset on every link it takes part in. Drawn before
    # positions for the same reason boards are: a roof or a basement is a property of the node.
    sitings = assign_sitings(node_count, siting_mix, rng)
    # Firmware version per node, drawn at random rather than by degree: whether an owner has updated
    # is unrelated to how well sited the node is, and assuming otherwise would decide the result.
    default_profile = profile if isinstance(profile, Profile) else Profile(profile)
    # `legacy_fraction` of the nodes run `old_profile` instead - any release series, or `legacy`.
    older_profile = (
        old_profile if isinstance(old_profile, Profile) else Profile(old_profile)
    )
    stale = set()
    if legacy_fraction > 0:
        want = max(1, int(round(node_count * legacy_fraction)))
        stale = set(rng.sample(range(node_count), min(want, node_count)))
    nodes = []
    for i, (x, y) in enumerate(points):
        node_profile = older_profile if i in stale else default_profile
        nodes.append(
            Node(
                i,
                x,
                y,
                node_num=node_nums[i],
                platform=platforms[i],
                profile=node_profile,
                max_num_nodes=(
                    max_num_nodes
                    if max_num_nodes is not None
                    else PLATFORM_HOT_STORE_BY_VERSION[node_profile.hot_store_model][
                        platforms[i]
                    ]
                ),
                # No release before this tree has a warm tier at all, so an older node forgets an
                # evicted peer outright where a 2.8 one keeps its key.
                warm_num_nodes=(
                    0
                    if not node_profile.warm_store
                    else (
                        warm_num_nodes
                        if warm_num_nodes is not None
                        else PLATFORM_WARM_STORE[platforms[i]]
                    )
                ),
                cold_cache_size=(
                    PLATFORM_COLD_CACHE[platforms[i]] if node_profile.warm_store else 0
                ),
                signature_policy=signature_policy,
            )
        )
    for node in nodes:
        node.rebroadcast_mode = rebroadcast_mode
        if node.profile.hop_scaling:
            # A per-node hash seed, so two nodes do not collide on the same pair of peers. The
            # firmware seeds it randomly at first boot and persists it.
            node.hop_scaling = HopScaling(hash_seed=rng.randrange(0, 1 << 16))
    mesh = Mesh(
        conf,
        nodes,
        rng,
        hop_limit=hop_limit,
        area=area,
        extra_loss=extra_loss,
        burst_loss=burst_loss,
        burst_ms=burst_ms,
        siting_gain=[SITINGS[name] for name in sitings],
        profile=profile,
    )
    mesh.topology = resolved
    mesh.sitings = sitings
    mesh.hop_assign = hop_assign

    if hop_spread:
        # Operators do not all set the same hop limit: a node in a dense middle reaches what it
        # needs at 3 or 4 and its owner leaves the default alone, while one on the edge raises it
        # until the rest of the mesh answers, and field guidance tops out at 7.
        #
        # `centrality` reproduces that correlation and so confounds hop limit with position: a table
        # of receptions-by-hop-limit then measures siting under a hop-limit label. `random` breaks
        # the correlation as a control, and is not how operators behave.
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

    # `degree` is what an operator does: the repeater goes on the hill. `inverse` is the adversarial
    # control - the router is the node in the basement with three neighbours, which is what actually
    # happens when someone flashes ROUTER onto the node they already own. `random` separates the
    # role's own effect from the siting that usually comes with it.
    by_degree = sorted(range(node_count), key=lambda i: -len(mesh.neighbours[i]))
    if role_placement == "inverse":
        by_degree = list(reversed(by_degree))
    elif role_placement == "random":
        rng.shuffle(by_degree)
    if role_mix:
        shares = ROLE_MIXES[role_mix] if isinstance(role_mix, str) else role_mix
        # Router-like roles go to the head of that order. CLIENT_MUTE is drawn at random from the
        # rest: muting a node is a decision about power or noise, not about siting, and handing it
        # to the worst-connected nodes would make it look free.
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

    # A role exists only from the release that introduced it - ROUTER_LATE v2.5.18, CLIENT_BASE
    # v2.7.9 - so a node on an older profile cannot be configured into one and runs as CLIENT.
    for node in nodes:
        if node.role == ROUTER_LATE and not node.profile.router_late_role:
            node.role = CLIENT
            mesh.stats["role_unavailable_in_version"] += 1
        elif node.role == CLIENT_BASE and not node.profile.client_base_role:
            node.role = CLIENT
            mesh.stats["role_unavailable_in_version"] += 1

    if favourite_routers:
        # Hop preservation only fires between nodes that have favourited each other, which in the
        # field means one operator running both ends of a spine. Every router-like node favouriting
        # every other is the upper bound on how much relaying can be free.
        spine = [i for i in range(node_count) if nodes[i].is_router_like()]
        for i in spine:
            nodes[i].favourites = {j for j in spine if j != i}
    return mesh
