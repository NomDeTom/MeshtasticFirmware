#pragma once

#include "MeshModule.h"
#include "concurrency/Lock.h"
#include "concurrency/OSThread.h"
#include "mesh/generated/meshtastic/mesh.pb.h"
#include "mesh/generated/meshtastic/telemetry.pb.h"

#if HAS_TRAFFIC_MANAGEMENT

/**
 * TrafficManagementModule - Packet inspection and traffic shaping for mesh networks.
 *
 * This module provides:
 * - Position deduplication (drop redundant position broadcasts)
 * - Per-node rate limiting (throttle chatty nodes)
 * - Unknown packet filtering (drop undecoded packets from repeat offenders)
 * - NodeInfo direct response (answer queries from cache to reduce mesh chatter)
 * - Local-only telemetry/position (exhaust hop_limit for local broadcasts)
 * - Router hop preservation (maintain hop_limit for router-to-router traffic)
 *
 * Memory Optimization:
 * Uses a unified cache with cuckoo hashing for O(1) lookups and 56% memory reduction
 * compared to separate per-feature caches. Timestamps are stored as 8-bit relative
 * offsets from a rolling epoch to further reduce memory footprint.
 */
class TrafficManagementModule : public MeshModule, private concurrency::OSThread
{
  public:
    TrafficManagementModule();
    ~TrafficManagementModule();

    // Singleton — no copying or moving
    TrafficManagementModule(const TrafficManagementModule &) = delete;
    TrafficManagementModule &operator=(const TrafficManagementModule &) = delete;

    meshtastic_TrafficManagementStats getStats() const;
    void resetStats();
    void recordRouterHopPreserved();

    // Next-hop overflow cache (routing hint).
    // setNextHop: store a confirmed last-byte next hop for `dest`. Called by
    //   NextHopRouter from its ACK-confirmed decision (see sniffReceived). The
    //   byte must come from a bidirectionally-verified relay, not one-way inference.
    // getNextHopHint: return the cached next-hop byte for `dest`, 0 if unknown.
    void setNextHop(NodeNum dest, uint8_t nextHopByte);
    uint8_t getNextHopHint(NodeNum dest);

    // Warm-start the next-hop cache from persisted NodeInfoLite hints so confirmed
    // hops survive later hot-store (NodeDB) eviction. Idempotent; runs once after
    // nodeDB is populated (lazily on first maintenance pass).
    void preloadNextHopsFromNodeDB();

    /**
     * Check if this packet should have its hops exhausted.
     * Called from perhapsRebroadcast() to force hop_limit = 0 regardless of
     * router_preserve_hops or favorite node logic.
     */
    bool shouldExhaustHops(const meshtastic_MeshPacket &mp) const
    {
        return exhaustRequested && exhaustRequestedFrom == getFrom(&mp) && exhaustRequestedId == mp.id;
    }

  protected:
    ProcessMessage handleReceived(const meshtastic_MeshPacket &mp) override;
    bool wantPacket(const meshtastic_MeshPacket *p) override { return true; }
    void alterReceived(meshtastic_MeshPacket &mp) override;
    int32_t runOnce() override;
    // Protected so test shims can force epoch rollover behavior.
    void resetEpoch(uint32_t nowMs);
    // Sliding-epoch rebase: advance the epoch and shift live entries back by the
    // same wall-clock amount instead of flushing, so cached state survives past the
    // ~19h horizon. Caller must hold cacheLock.
    void rebaseEpoch(uint32_t nowMs);

  private:
    // =========================================================================
    // Unified Cache Entry (10 bytes) - Same for ALL platforms
    // =========================================================================
    //
    // A single compact structure used across ESP32, NRF52, and all other platforms.
    // Memory: 11 bytes × 2048 entries = 22KB
    //
    // Position Fingerprinting:
    //   Instead of storing full coordinates (8 bytes) or a computed hash,
    //   we store an 8-bit fingerprint derived deterministically from the
    //   truncated lat/lon. This extracts the lower 4 significant bits from
    //   each coordinate: fingerprint = (lat_low4 << 4) | lon_low4
    //
    //   Benefits over hash:
    //   - Adjacent grid cells have sequential fingerprints (no collision)
    //   - Two positions only collide if 16+ grid cells apart in BOTH dimensions
    //   - Deterministic: same input always produces same output
    //
    // Adaptive Timestamp Resolution:
    //   All timestamps use 8-bit values with adaptive resolution calculated
    //   from config at startup. Resolution = max(60, min(339, interval/2)).
    //   - Min 60 seconds ensures reasonable precision
    //   - Max 339 seconds allows ~24 hour range (255 * 339 = 86445 sec)
    //   - interval/2 ensures at least 2 ticks per configured interval
    //
    // Layout:
    //   [0-3]   node            - NodeNum (4 bytes)
    //   [4]     pos_fingerprint - 4 bits lat + 4 bits lon (1 byte)
    //   [5]     rate_count      - Packets in current window (1 byte)
    //   [6]     unknown_count   - Unknown packets count (1 byte)
    //   [7]     pos_time        - Position timestamp (1 byte, adaptive resolution)
    //   [8]     rate_time       - Rate window start (1 byte, adaptive resolution)
    //   [9]     unknown_time    - Unknown tracking start (1 byte, adaptive resolution)
    //   [10]    next_hop        - Last-byte relay to reach `node` (1 byte, 0 = none)
    //
    // next_hop semantics:
    //   A routing hint: the last byte of the NodeNum to use as next hop to reach
    //   `node`. Written ONLY from NextHopRouter's ACK-confirmed decision (a
    //   bidirectionally-verified relay), never inferred one-way from relayed
    //   traffic. The TMM cache acts as an overflow store for confirmed next-hops
    //   that have aged out of the hot NodeDB (NodeInfoLite). Unlike the other
    //   fields it has no TTL of its own — it keeps its slot alive (see runOnce)
    //   and is refreshed only on the next confirmed exchange.
    //
    struct __attribute__((packed)) UnifiedCacheEntry {
        NodeNum node;            // 4 bytes - Node identifier (0 = empty slot)
        uint8_t pos_fingerprint; // 1 byte  - Lower 4 bits of lat + lon
        uint8_t rate_count;      // 1 byte  - Packet count (saturates at 255)
        uint8_t unknown_count;   // 1 byte  - Unknown packet count (saturates at 255)
        uint8_t pos_time;        // 1 byte  - Position timestamp (adaptive resolution)
        uint8_t rate_time;       // 1 byte  - Rate window start (adaptive resolution)
        uint8_t unknown_time;    // 1 byte  - Unknown tracking start (adaptive resolution)
        uint8_t next_hop;        // 1 byte  - Last-byte relay to reach `node` (0 = none). See note below.
    };
    static_assert(sizeof(UnifiedCacheEntry) == 11, "UnifiedCacheEntry should be 11 bytes");

    // =========================================================================
    // Cuckoo Hash Table Implementation
    // =========================================================================
    //
    // Cuckoo hashing provides O(1) worst-case lookup time using two hash functions.
    // Each key can be in one of two possible locations (h1 or h2). On collision,
    // the existing entry is "kicked" to its alternate location.
    //
    // Benefits over linear scan:
    // - O(1) lookup vs O(n) - critical at packet processing rates
    // - O(1) insertion (amortized) with simple eviction on cycles
    // - ~95% load factor achievable
    //
    // Cache size rounds to power-of-2 for fast modulo via bitmask.
    // TRAFFIC_MANAGEMENT_CACHE_SIZE=2000 → cacheSize()=2048
    //
    static constexpr uint16_t cacheSize();
    static constexpr uint16_t cacheMask();

    // Hash functions for cuckoo hashing
    inline uint16_t cuckooHash1(NodeNum node) const { return node & cacheMask(); }
    inline uint16_t cuckooHash2(NodeNum node) const { return ((node * 2654435769u) >> (32 - cuckooHashBits())) & cacheMask(); }
    static constexpr uint8_t cuckooHashBits();

    // NodeInfo cache configuration (PSRAM path):
    // - Payload lives in PSRAM
    // - DRAM keeps packed 12-bit tags with 4-way bucketed cuckoo hashing
    //   (Fan et al., CoNEXT 2014). Tag value 0 is reserved as "empty".
    static constexpr uint16_t kNodeInfoIndexMetadataBudgetBytes = 3072; // 3KB DRAM tag store
    static constexpr uint8_t kNodeInfoTargetOccupancyPercent = 95;
    static constexpr uint8_t kNodeInfoBucketSize = 4;
    static constexpr uint8_t kNodeInfoTagBits = 12;
    static constexpr uint16_t kNodeInfoTagMask = static_cast<uint16_t>((1u << kNodeInfoTagBits) - 1u);
    static constexpr uint16_t kNodeInfoIndexSlotsRaw =
        static_cast<uint16_t>((kNodeInfoIndexMetadataBudgetBytes * 8u) / kNodeInfoTagBits);
    static constexpr uint16_t kNodeInfoIndexSlots =
        static_cast<uint16_t>(kNodeInfoIndexSlotsRaw - (kNodeInfoIndexSlotsRaw % kNodeInfoBucketSize));
    static constexpr uint16_t kNodeInfoTargetEntries =
        static_cast<uint16_t>((kNodeInfoIndexSlots * kNodeInfoTargetOccupancyPercent) / 100u);
    static_assert((kNodeInfoIndexSlots % kNodeInfoBucketSize) == 0, "NodeInfo slot count must align to bucket size");
    static_assert(kNodeInfoTargetEntries < (1u << kNodeInfoTagBits), "NodeInfo tag bits must encode payload index");

    static constexpr uint16_t nodeInfoTargetEntries();
    static constexpr uint16_t nodeInfoIndexMetadataBudgetBytes();
    static constexpr uint8_t nodeInfoTargetOccupancyPercent();
    static constexpr uint8_t nodeInfoBucketSize();
    static constexpr uint8_t nodeInfoTagBits();
    static constexpr uint16_t nodeInfoTagMask();
    static constexpr uint16_t nodeInfoIndexSlots();
    static constexpr uint16_t nodeInfoBucketCount();
    static constexpr uint16_t nodeInfoBucketMask();
    static constexpr uint8_t nodeInfoBucketHashBits();
    inline uint16_t nodeInfoHash1(NodeNum node) const { return node & nodeInfoBucketMask(); }
    inline uint16_t nodeInfoHash2(NodeNum node) const
    {
        return ((node * 2246822519u) >> (32 - nodeInfoBucketHashBits())) & nodeInfoBucketMask();
    }

    // =========================================================================
    // Adaptive Timestamp Resolution
    // =========================================================================
    //
    // All timestamps use 8-bit values with adaptive resolution calculated from
    // config at startup. This allows ~24 hour range while maintaining precision.
    //
    // Resolution formula: max(60, min(339, interval/2))
    //   - 60 sec minimum ensures reasonable precision
    //   - 339 sec maximum allows 24 hour range (255 * 339 ≈ 86400 sec)
    //   - interval/2 ensures at least 2 ticks per configured interval
    //
    // Since config changes require reboot, resolution is calculated once.
    //
    uint32_t cacheEpochMs = 0;
    uint16_t posTimeResolution = 60;     // Seconds per tick for position
    uint16_t rateTimeResolution = 60;    // Seconds per tick for rate limiting
    uint16_t unknownTimeResolution = 60; // Seconds per tick for unknown tracking

    // Calculate resolution from configured interval (called once at startup)
    static uint16_t calcTimeResolution(uint32_t intervalSecs)
    {
        // Resolution = interval/2 to ensure at least 2 ticks per interval
        // Clamped to [60, 339] for min precision and max 24h range
        uint32_t res = (intervalSecs > 0) ? (intervalSecs / 2) : 60;
        if (res < 60)
            res = 60;
        if (res > 339)
            res = 339;
        return static_cast<uint16_t>(res);
    }

    // Convert to/from 8-bit relative timestamps with given resolution.
    //
    // All stored timestamps carry a uniform +1 "presence" offset: a value of 0 is
    // reserved for "no timestamp recorded" (which is also the zero-initialized
    // state), and stored values 1..255 encode raw ticks 0..254. This keeps the
    // 0-means-empty sentinel consistent with memset/calloc zeroing across every
    // sub-store, so the maintenance sweep's `_time != 0` presence checks are
    // unambiguous (a timestamp recorded in the first tick after the epoch is no
    // longer mistaken for an empty slot). The offset is applied here and removed
    // on read, so it cancels out in all window math.
    uint8_t toRelativeTime(uint32_t nowMs, uint16_t resolutionSecs) const
    {
        uint32_t ticks = (nowMs - cacheEpochMs) / (resolutionSecs * 1000UL);
        return (ticks >= UINT8_MAX) ? UINT8_MAX : static_cast<uint8_t>(ticks + 1);
    }
    uint32_t fromRelativeTime(uint8_t ticks, uint16_t resolutionSecs) const
    {
        return (ticks == 0) ? cacheEpochMs : cacheEpochMs + (static_cast<uint32_t>(ticks - 1) * resolutionSecs * 1000UL);
    }

    // Convenience wrappers for each timestamp type (the +1 presence offset lives
    // in the shared converters above, so these are plain pass-throughs).
    uint8_t toRelativePosTime(uint32_t nowMs) const { return toRelativeTime(nowMs, posTimeResolution); }
    uint32_t fromRelativePosTime(uint8_t t) const { return fromRelativeTime(t, posTimeResolution); }

    uint8_t toRelativeRateTime(uint32_t nowMs) const { return toRelativeTime(nowMs, rateTimeResolution); }
    uint32_t fromRelativeRateTime(uint8_t t) const { return fromRelativeTime(t, rateTimeResolution); }

    uint8_t toRelativeUnknownTime(uint32_t nowMs) const { return toRelativeTime(nowMs, unknownTimeResolution); }
    uint32_t fromRelativeUnknownTime(uint8_t t) const { return fromRelativeTime(t, unknownTimeResolution); }

    // Coarsest of the three per-feature resolutions (seconds per tick).
    uint16_t maxResolution() const
    {
        uint16_t maxRes = posTimeResolution;
        if (rateTimeResolution > maxRes)
            maxRes = rateTimeResolution;
        if (unknownTimeResolution > maxRes)
            maxRes = unknownTimeResolution;
        return maxRes;
    }

    // True when relative offsets approach 8-bit overflow.
    // With max resolution of 339 sec, 200 ticks = ~19 hours (safe margin for 24h max).
    bool needsEpochReset(uint32_t nowMs) const { return (nowMs - cacheEpochMs) > (200UL * maxResolution() * 1000UL); }
    // =========================================================================
    // Position Fingerprint
    // =========================================================================
    //
    // Computes 8-bit fingerprint from truncated lat/lon coordinates.
    // Extracts lower 4 significant bits from each coordinate.
    //
    // fingerprint = (lat_low4 << 4) | lon_low4
    //
    // Unlike a hash, adjacent grid cells have sequential fingerprints,
    // so nearby positions never collide. Collisions only occur for
    // positions 16+ grid cells apart in both dimensions.
    //
    // Guards: If precision < 4 bits, uses min(precision, 4) bits.
    //
    static uint8_t computePositionFingerprint(int32_t lat_truncated, int32_t lon_truncated, uint8_t precision);

    // =========================================================================
    // Cache Storage
    // =========================================================================

    mutable concurrency::Lock cacheLock; // Protects all cache access
    UnifiedCacheEntry *cache = nullptr;  // Cuckoo hash table (unified for all platforms)
    bool cacheFromPsram = false;         // Tracks allocator for correct deallocation

    struct NodeInfoPayloadEntry {
        // Node identifier associated with this payload slot.
        // 0 means the slot is currently unused.
        NodeNum node;

        // Cached NODEINFO_APP payload body. This is separate from NodeDB and is only
        // used by the PSRAM-backed direct-response path in this module.
        meshtastic_User user;

        // Extra response metadata captured from the latest observed NODEINFO_APP
        // packet for this node. shouldRespondToNodeInfo() uses this metadata when
        // building spoofed replies for requesting clients.

        // Last local uptime tick (millis) when this entry was refreshed.
        uint32_t lastObservedMs;

        // Last RTC/packet timestamp (seconds) observed for this NodeInfo frame.
        // If unavailable in packet, remains 0.
        uint32_t lastObservedRxTime;

        // Channel where we most recently heard this node's NodeInfo.
        uint8_t sourceChannel;

        // Cached decoded bitfield metadata from the source packet.
        // We preserve non-OK_TO_MQTT bits in direct replies when available.
        bool hasDecodedBitfield;
        uint8_t decodedBitfield;
    };

    NodeInfoPayloadEntry *nodeInfoPayload = nullptr; // NodeInfo payloads in PSRAM
    bool nodeInfoPayloadFromPsram = false;           // Tracks allocator for correct deallocation
    uint8_t *nodeInfoIndex = nullptr;                // Packed 12-bit NodeInfo tags in DRAM
    uint16_t nodeInfoAllocHint = 0;
    uint16_t nodeInfoEvictCursor = 0;

    meshtastic_TrafficManagementStats stats;

    // Flag set during alterReceived() when packet should be exhausted.
    // Checked by perhapsRebroadcast() to force hop_limit = 0 only for the
    // matching packet key (from + id). Reset at start of handleReceived().
    bool exhaustRequested = false;
    NodeNum exhaustRequestedFrom = 0;
    PacketId exhaustRequestedId = 0;

    // One-shot guard: warm-start next-hop cache from NodeDB on first maintenance pass.
    bool nextHopPreloaded = false;

    // =========================================================================
    // Cache Operations
    // =========================================================================

    // Find or create entry for node using cuckoo hashing
    // Returns nullptr if cache is full and eviction fails
    UnifiedCacheEntry *findOrCreateEntry(NodeNum node, bool *isNew);

    // Find existing entry (no creation)
    UnifiedCacheEntry *findEntry(NodeNum node);

    // NodeInfo cache operations (bucketed cuckoo index + PSRAM payloads)
    const NodeInfoPayloadEntry *findNodeInfoEntry(NodeNum node) const;
    NodeInfoPayloadEntry *findOrCreateNodeInfoEntry(NodeNum node, bool *usedEmptySlot);
    uint16_t findNodeInfoPayloadIndex(NodeNum node) const;
    bool removeNodeInfoIndexEntry(NodeNum node, uint16_t payloadIndex);
    uint16_t allocateNodeInfoPayloadSlot();
    uint16_t evictNodeInfoPayloadSlot();
    bool tryInsertNodeInfoEntryInBucket(uint16_t bucket, uint16_t tag);
    uint16_t encodeNodeInfoTag(uint16_t payloadIndex) const;
    uint16_t decodeNodeInfoPayloadIndex(uint16_t tag) const;
    uint16_t getNodeInfoTag(uint16_t slot) const;
    void setNodeInfoTag(uint16_t slot, uint16_t tag);
    uint16_t countNodeInfoEntriesLocked() const;
    void cacheNodeInfoPacket(const meshtastic_MeshPacket &mp);

    // =========================================================================
    // Traffic Management Logic
    // =========================================================================

    bool shouldDropPosition(const meshtastic_MeshPacket *p, const meshtastic_Position *pos, uint32_t nowMs);
    bool shouldRespondToNodeInfo(const meshtastic_MeshPacket *p, bool sendResponse);
    bool isMinHopsFromRequestor(const meshtastic_MeshPacket *p) const;
    bool isRateLimited(NodeNum from, uint32_t nowMs);
    bool shouldDropUnknown(const meshtastic_MeshPacket *p, uint32_t nowMs);

    void logAction(const char *action, const meshtastic_MeshPacket *p, const char *reason) const;
    void incrementStat(uint32_t *field);
};

// =========================================================================
// Compile-time Cache Size Calculations
// =========================================================================
//
// Round TRAFFIC_MANAGEMENT_CACHE_SIZE up to next power of 2 for efficient
// cuckoo hash indexing (allows bitmask instead of modulo).
//
// These use C++11-compatible constexpr (single return statement).
//

namespace detail
{
// Helper: round up to next power of 2 using bit manipulation
constexpr uint16_t nextPow2(uint16_t n)
{
    return n == 0 ? 0 : (((n - 1) | ((n - 1) >> 1) | ((n - 1) >> 2) | ((n - 1) >> 4) | ((n - 1) >> 8)) + 1);
}

// Helper: floor(log2(n)) for n >= 0, C++11-compatible constexpr.
constexpr uint8_t log2Floor(uint16_t n)
{
    return n <= 1 ? 0 : static_cast<uint8_t>(1 + log2Floor(static_cast<uint16_t>(n >> 1)));
}

// Helper: ceil(log2(n)) for n >= 1, C++11-compatible constexpr.
constexpr uint8_t log2Ceil(uint16_t n)
{
    return n <= 1 ? 0 : static_cast<uint8_t>(1 + log2Floor(static_cast<uint16_t>(n - 1)));
}
} // namespace detail

constexpr uint16_t TrafficManagementModule::cacheSize()
{
    return detail::nextPow2(TRAFFIC_MANAGEMENT_CACHE_SIZE);
}

constexpr uint16_t TrafficManagementModule::cacheMask()
{
    return cacheSize() > 0 ? cacheSize() - 1 : 0;
}

constexpr uint8_t TrafficManagementModule::cuckooHashBits()
{
    return detail::log2Floor(cacheSize());
}

constexpr uint16_t TrafficManagementModule::nodeInfoTargetEntries()
{
    return kNodeInfoTargetEntries;
}

constexpr uint16_t TrafficManagementModule::nodeInfoIndexMetadataBudgetBytes()
{
    return kNodeInfoIndexMetadataBudgetBytes;
}

constexpr uint8_t TrafficManagementModule::nodeInfoTargetOccupancyPercent()
{
    return kNodeInfoTargetOccupancyPercent;
}

constexpr uint8_t TrafficManagementModule::nodeInfoBucketSize()
{
    return kNodeInfoBucketSize;
}

constexpr uint8_t TrafficManagementModule::nodeInfoTagBits()
{
    return kNodeInfoTagBits;
}

constexpr uint16_t TrafficManagementModule::nodeInfoTagMask()
{
    return kNodeInfoTagMask;
}

constexpr uint16_t TrafficManagementModule::nodeInfoIndexSlots()
{
    return kNodeInfoIndexSlots;
}

constexpr uint16_t TrafficManagementModule::nodeInfoBucketCount()
{
    return static_cast<uint16_t>(nodeInfoIndexSlots() / nodeInfoBucketSize());
}

constexpr uint16_t TrafficManagementModule::nodeInfoBucketMask()
{
    return nodeInfoBucketCount() > 0 ? nodeInfoBucketCount() - 1 : 0;
}

constexpr uint8_t TrafficManagementModule::nodeInfoBucketHashBits()
{
    return detail::log2Floor(nodeInfoBucketCount());
}

extern TrafficManagementModule *trafficManagementModule;

#endif
