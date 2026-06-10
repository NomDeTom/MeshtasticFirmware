#include "WarmNodeStore.h"

#if WARM_NODE_COUNT > 0

#include "FSCommon.h"
#include "SPILock.h"
#include "SafeFile.h"
#include "configuration.h"
#include "power/PowerHAL.h"
#include <ErriezCRC32.h>
#include <vector>

#ifdef NRF52840_XXAA
#include "flash/flash_nrf5x.h"
#endif

// Shared by the raw-flash and file backends. The header is written after the
// entries so a torn write leaves a CRC mismatch instead of a plausible record.
struct WarmStoreHeader {
    uint32_t magic;     // WARM_STORE_MAGIC
    uint32_t seq;       // ping-pong generation counter (0 for the file backend)
    uint16_t count;     // entries persisted
    uint16_t entrySize; // sizeof(WarmNodeEntry), format guard
    uint32_t crc;       // crc32 over count * entrySize bytes
};
static_assert(sizeof(WarmStoreHeader) == 16, "header layout is part of the persistence format");

#define WARM_STORE_MAGIC 0x314D5257u // "WRM1"

#if !defined(NRF52840_XXAA) && defined(FSCom)
static const char *warmFileName = "/prefs/warm.dat";
#endif

static inline bool keyIsSet(const uint8_t key[32])
{
    for (int i = 0; i < 32; i++)
        if (key[i])
            return true;
    return false;
}

WarmNodeStore::WarmNodeStore()
{
#if defined(ARCH_ESP32) && defined(BOARD_HAS_PSRAM)
    entries = static_cast<WarmNodeEntry *>(ps_calloc(WARM_NODE_COUNT, sizeof(WarmNodeEntry)));
    if (entries) {
        entriesFromPsram = true;
    } else {
        LOG_WARN("WarmStore: PSRAM allocation failed, falling back to heap");
        entries = new WarmNodeEntry[WARM_NODE_COUNT]();
    }
#else
    entries = new WarmNodeEntry[WARM_NODE_COUNT]();
#endif
}

WarmNodeStore::~WarmNodeStore()
{
    if (!entries)
        return;
    if (entriesFromPsram)
        free(entries);
    else
        delete[] entries;
    entries = nullptr;
}

WarmNodeEntry *WarmNodeStore::find(NodeNum num) const
{
    if (!entries || !num)
        return nullptr;
    for (size_t i = 0; i < WARM_NODE_COUNT; i++)
        if (entries[i].num == num)
            return &entries[i];
    return nullptr;
}

bool WarmNodeStore::absorb(NodeNum num, uint32_t lastHeard, const uint8_t *key32)
{
    if (!entries || !num)
        return false;

    const bool candidateKeyed = key32 && keyIsSet(key32);

    WarmNodeEntry *slot = find(num);
    if (!slot) {
        // Pick a victim: any empty slot, else the oldest keyless entry, else
        // (only for keyed candidates) the oldest keyed entry.
        WarmNodeEntry *oldestKeyless = nullptr, *oldestKeyed = nullptr;
        for (size_t i = 0; i < WARM_NODE_COUNT; i++) {
            WarmNodeEntry &e = entries[i];
            if (!e.num) {
                slot = &e;
                break;
            }
            if (keyIsSet(e.public_key)) {
                if (!oldestKeyed || e.last_heard < oldestKeyed->last_heard)
                    oldestKeyed = &e;
            } else {
                if (!oldestKeyless || e.last_heard < oldestKeyless->last_heard)
                    oldestKeyless = &e;
            }
        }
        if (!slot)
            slot = oldestKeyless ? oldestKeyless : (candidateKeyed ? oldestKeyed : nullptr);
        if (!slot)
            return false; // store full of keyed entries and the candidate has no key
    }

    slot->num = num;
    slot->last_heard = lastHeard;
    if (candidateKeyed)
        memcpy(slot->public_key, key32, 32);
    else
        memset(slot->public_key, 0, 32);
    dirty = true;
    return true;
}

bool WarmNodeStore::take(NodeNum num, WarmNodeEntry &out)
{
    WarmNodeEntry *e = find(num);
    if (!e)
        return false;
    out = *e;
    memset(e, 0, sizeof(*e));
    dirty = true;
    return true;
}

bool WarmNodeStore::copyKey(NodeNum num, uint8_t out[32]) const
{
    const WarmNodeEntry *e = find(num);
    if (!e || !keyIsSet(e->public_key))
        return false;
    memcpy(out, e->public_key, 32);
    return true;
}

bool WarmNodeStore::contains(NodeNum num) const
{
    return find(num) != nullptr;
}

void WarmNodeStore::remove(NodeNum num)
{
    WarmNodeEntry *e = find(num);
    if (e) {
        memset(e, 0, sizeof(*e));
        dirty = true;
    }
}

void WarmNodeStore::clear()
{
    if (!entries)
        return;
    memset(entries, 0, WARM_NODE_COUNT * sizeof(WarmNodeEntry));
    dirty = true;
}

size_t WarmNodeStore::count() const
{
    size_t n = 0;
    if (entries)
        for (size_t i = 0; i < WARM_NODE_COUNT; i++)
            if (entries[i].num)
                n++;
    return n;
}

// Compact occupied slots to the front of `dst`; returns the count.
static uint16_t packEntries(const WarmNodeEntry *src, WarmNodeEntry *dst)
{
    uint16_t n = 0;
    for (size_t i = 0; i < WARM_NODE_COUNT; i++)
        if (src[i].num)
            dst[n++] = src[i];
    return n;
}

bool WarmNodeStore::saveIfDirty()
{
    if (!dirty)
        return true;
    bool ok = save();
    if (ok)
        dirty = false;
    return ok;
}

#ifdef NRF52840_XXAA

// ---- Raw internal-flash ping-pong backend (nRF52840) -----------------------
// Two 16 KB copies just below the LittleFS partition, in app-region flash
// reclaimed by LTO. flash_nrf5x_* is the same SoftDevice-safe HAL InternalFS
// uses; spiLock serializes us against LittleFS's shared page cache.

static bool readHeader(uint32_t addr, WarmStoreHeader &h)
{
    flash_nrf5x_read(&h, addr, sizeof(h));
    return h.magic == WARM_STORE_MAGIC && h.entrySize == sizeof(WarmNodeEntry) && h.count <= WARM_NODE_COUNT &&
           sizeof(WarmStoreHeader) + (size_t)h.count * sizeof(WarmNodeEntry) <= WARM_FLASH_COPY_BYTES;
}

void WarmNodeStore::load()
{
    if (!entries)
        return;
    concurrency::LockGuard g(spiLock);

    uint32_t bestSeq = 0;
    int best = -1;
    for (int i = 0; i < 2; i++) {
        WarmStoreHeader h;
        if (!readHeader(WARM_FLASH_COPY_ADDR(i), h))
            continue;
        // Validate the CRC before trusting the copy
        std::vector<WarmNodeEntry> buf(h.count);
        if (h.count)
            flash_nrf5x_read(buf.data(), WARM_FLASH_COPY_ADDR(i) + sizeof(h), h.count * sizeof(WarmNodeEntry));
        if (crc32Buffer(buf.data(), h.count * sizeof(WarmNodeEntry)) != h.crc)
            continue;
        if (best < 0 || (int32_t)(h.seq - bestSeq) > 0) {
            best = i;
            bestSeq = h.seq;
        }
    }
    if (best < 0) {
        LOG_INFO("WarmStore: no valid flash copy found, starting empty");
        return;
    }

    WarmStoreHeader h;
    readHeader(WARM_FLASH_COPY_ADDR(best), h);
    if (h.count)
        flash_nrf5x_read(entries, WARM_FLASH_COPY_ADDR(best) + sizeof(h), h.count * sizeof(WarmNodeEntry));
    LOG_INFO("WarmStore: loaded %u warm nodes (copy %d, seq %u)", h.count, best, (unsigned)h.seq);
}

bool WarmNodeStore::save()
{
    if (!entries)
        return false;
    if (!powerHAL_isPowerLevelSafe()) {
        LOG_ERROR("Error: trying to save WarmStore on unsafe device power level.");
        return false;
    }
    concurrency::LockGuard g(spiLock);

    // Figure out which copy is current so we overwrite the other one
    uint32_t curSeq = 0;
    int cur = -1;
    for (int i = 0; i < 2; i++) {
        WarmStoreHeader h;
        if (readHeader(WARM_FLASH_COPY_ADDR(i), h) && (cur < 0 || (int32_t)(h.seq - curSeq) > 0)) {
            cur = i;
            curSeq = h.seq;
        }
    }
    const int target = (cur == 0) ? 1 : 0;
    const uint32_t addr = WARM_FLASH_COPY_ADDR(target);

    std::vector<WarmNodeEntry> packed(WARM_NODE_COUNT);
    WarmStoreHeader h;
    h.magic = WARM_STORE_MAGIC;
    h.seq = curSeq + 1;
    h.count = packEntries(entries, packed.data());
    h.entrySize = sizeof(WarmNodeEntry);
    h.crc = crc32Buffer(packed.data(), h.count * sizeof(WarmNodeEntry));

    // Entries first, header last: a torn write fails CRC and load() falls back
    // to the other copy.
    if (h.count)
        flash_nrf5x_write(addr + sizeof(h), packed.data(), h.count * sizeof(WarmNodeEntry));
    flash_nrf5x_write(addr, &h, sizeof(h));
    flash_nrf5x_flush();
    LOG_DEBUG("WarmStore: saved %u warm nodes (copy %d, seq %u)", h.count, target, (unsigned)h.seq);
    return true;
}

#elif defined(FSCom)

// ---- File backend (ESP32 / RP2040 / portduino / non-840 nRF52) -------------

void WarmNodeStore::load()
{
    if (!entries)
        return;
    concurrency::LockGuard g(spiLock);
    auto f = FSCom.open(warmFileName, FILE_O_READ);
    if (!f)
        return;
    WarmStoreHeader h;
    bool ok = (size_t)f.read((uint8_t *)&h, sizeof(h)) == sizeof(h) && h.magic == WARM_STORE_MAGIC &&
              h.entrySize == sizeof(WarmNodeEntry) && h.count <= WARM_NODE_COUNT;
    if (ok && h.count) {
        const size_t len = (size_t)h.count * sizeof(WarmNodeEntry);
        ok = (size_t)f.read((uint8_t *)entries, len) == len && crc32Buffer(entries, len) == h.crc;
        if (!ok)
            memset(entries, 0, WARM_NODE_COUNT * sizeof(WarmNodeEntry));
    }
    f.close();
    if (ok)
        LOG_INFO("WarmStore: loaded %u warm nodes from %s", h.count, warmFileName);
    else
        LOG_WARN("WarmStore: %s invalid, starting empty", warmFileName);
}

bool WarmNodeStore::save()
{
    if (!entries)
        return false;
    if (!powerHAL_isPowerLevelSafe()) {
        LOG_ERROR("Error: trying to save WarmStore on unsafe device power level.");
        return false;
    }

    std::vector<WarmNodeEntry> packed(WARM_NODE_COUNT);
    WarmStoreHeader h;
    h.magic = WARM_STORE_MAGIC;
    h.seq = 0;
    h.count = packEntries(entries, packed.data());
    h.entrySize = sizeof(WarmNodeEntry);
    h.crc = crc32Buffer(packed.data(), h.count * sizeof(WarmNodeEntry));

    spiLock->lock();
    FSCom.mkdir("/prefs");
    spiLock->unlock();

    auto f = SafeFile(warmFileName, false);
    f.write((const uint8_t *)&h, sizeof(h));
    f.write((const uint8_t *)packed.data(), h.count * sizeof(WarmNodeEntry));
    bool ok = f.close();
    if (!ok)
        LOG_ERROR("WarmStore: can't write %s", warmFileName);
    else
        LOG_DEBUG("WarmStore: saved %u warm nodes to %s", h.count, warmFileName);
    return ok;
}

#else

void WarmNodeStore::load() {}
bool WarmNodeStore::save()
{
    return true;
}

#endif

#endif // WARM_NODE_COUNT > 0
