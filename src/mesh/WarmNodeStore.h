#pragma once

#include "MeshTypes.h"
#include "mesh-pb-constants.h"
#include <stdint.h>
#include <string.h>

#if WARM_NODE_COUNT > 0

/**
 * Warm ("long-tail") node tier — see .notes/nodedb-3tier-sizing.md.
 *
 * Holds a minimal identity record for nodes evicted from the hot NodeInfoLite
 * store: NodeNum, last_heard and the Curve25519 public key. The tier exists
 * primarily so DMs to/from an evicted node keep encrypting/decrypting — the
 * public key is expensive to re-learn (requires a NodeInfo exchange) while
 * everything else in NodeInfoLite is rebuilt from traffic within seconds.
 *
 * Flat fixed-capacity array, linear scan (lookups happen only on hot-store
 * misses), LRU eviction by last_heard with key-bearing entries outranking
 * keyless ones.
 */
struct WarmNodeEntry {
    NodeNum num;            // 0 = empty slot
    uint32_t last_heard;    // recency for LRU ordering
    uint8_t public_key[32]; // all-zero = no key (a real key is never all-zero)
};
static_assert(sizeof(WarmNodeEntry) == 40, "WarmNodeEntry must stay 40 B — persistence format depends on it");

#ifdef NRF52840_XXAA
// Persistence on nRF52840 uses two raw internal-flash copies (ping-pong) in
// the top 8 pages of the app region — space reclaimed by whole-image LTO.
// The linker script caps the image at WARM_FLASH_REGION_BASE and
// extra_scripts/nrf52_warm_region.py fails the build if an image grows into
// the region. LittleFS (28 KB at 0xED000) is untouched.
#define WARM_FLASH_COPY_BYTES 16384u                                            // 4 × 4 KB pages per copy
#define WARM_FLASH_REGION_BASE (0xED000u - 2u * WARM_FLASH_COPY_BYTES)          // 0xE5000
#define WARM_FLASH_COPY_ADDR(i) (WARM_FLASH_REGION_BASE + (i)*WARM_FLASH_COPY_BYTES)
#endif

class WarmNodeStore
{
  public:
    WarmNodeStore();
    ~WarmNodeStore();

    /// Remember an evicted hot node. Keyless candidates never displace keyed
    /// entries; otherwise the oldest (keyless-first) entry is replaced.
    /// @return true if the node was stored or updated
    bool absorb(NodeNum num, uint32_t lastHeard, const uint8_t *key32 /* may be NULL */);

    /// Find and remove an entry (used when the node is re-admitted to the hot store).
    bool take(NodeNum num, WarmNodeEntry &out);

    /// Copy the 32-byte public key for a node, if we have one.
    bool copyKey(NodeNum num, uint8_t out[32]) const;

    bool contains(NodeNum num) const;
    void remove(NodeNum num);
    void clear();
    size_t count() const;
    size_t capacity() const { return entries ? WARM_NODE_COUNT : 0; }

    /// Load persisted entries (called once at boot, after the node DB loads).
    void load();
    /// Persist if anything changed since the last save. Piggybacks on the
    /// node-database save cadence.
    bool saveIfDirty();

  private:
    WarmNodeEntry *entries = nullptr; // WARM_NODE_COUNT slots; PSRAM on ESP32 when available
    bool entriesFromPsram = false;
    bool dirty = false;

    WarmNodeEntry *find(NodeNum num) const;
    bool save();
};

#endif // WARM_NODE_COUNT > 0
