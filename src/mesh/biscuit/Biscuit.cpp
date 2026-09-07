#include "Biscuit.h"

#if MESHTASTIC_BISCUIT_ENABLED

#include "pb_common.h"
#include "pb_encode.h"
#include <string.h>

namespace biscuit
{

namespace
{

// ---------------------------------------------------------------- varint / zigzag

size_t putVarint(uint8_t *p, size_t cap, size_t at, uint64_t v)
{
    while (v >= 0x80) {
        if (at >= cap)
            return SIZE_MAX;
        p[at++] = (uint8_t)(v | 0x80);
        v >>= 7;
    }
    if (at >= cap)
        return SIZE_MAX;
    p[at++] = (uint8_t)v;
    return at;
}

size_t getVarint(const uint8_t *p, size_t len, size_t at, uint64_t *out)
{
    uint64_t v = 0;
    uint8_t shift = 0;
    while (at < len) {
        uint8_t b = p[at++];
        v |= (uint64_t)(b & 0x7F) << shift;
        if (!(b & 0x80)) {
            *out = v;
            return at;
        }
        shift += 7;
        if (shift > 63)
            break;
    }
    return SIZE_MAX;
}

inline uint64_t zz(int64_t v)
{
    return ((uint64_t)v << 1) ^ (uint64_t)(v >> 63);
}
inline int64_t unzz(uint64_t v)
{
    return (int64_t)(v >> 1) ^ -(int64_t)(v & 1);
}
inline uint8_t varintLen(uint64_t v)
{
    uint8_t n = 1;
    while (v >= 0x80) {
        v >>= 7;
        n++;
    }
    return n;
}

// ---------------------------------------------------------------- field access

/// Columns we can encode. Anything else (strings, bytes, submessages) is skipped.
bool encodable(pb_type_t t)
{
    if (PB_ATYPE(t) != PB_ATYPE_STATIC)
        return false;
    if (PB_HTYPE(t) == PB_HTYPE_REPEATED)
        return false;
    switch (PB_LTYPE(t)) {
    case PB_LTYPE_BOOL:
    case PB_LTYPE_VARINT:
    case PB_LTYPE_UVARINT:
    case PB_LTYPE_SVARINT:
    case PB_LTYPE_FIXED32:
        return true;
    default:
        return false;
    }
}

/// The family this tag leads, or null. Only the first tag of a family owns the column.
const FieldFamily *familyLedBy(const Options &opt, uint8_t tag)
{
    for (uint8_t i = 0; i < opt.familyCount; i++)
        if (opt.families[i].count > 1 && opt.families[i].tags[0] == tag)
            return &opt.families[i];
    return nullptr;
}

/// True if the tag belongs to a family but does not lead it, so its column is folded away.
bool inFamilyButNotLeader(const Options &opt, uint8_t tag)
{
    for (uint8_t i = 0; i < opt.familyCount; i++)
        for (uint8_t j = 1; j < opt.families[i].count; j++)
            if (opt.families[i].tags[j] == tag)
                return true;
    return false;
}

const FieldHint *findHint(const Options &opt, uint8_t tag)
{
    for (uint8_t i = 0; i < opt.hintCount; i++)
        if (opt.hints[i].tag == tag)
            return &opt.hints[i];
    return nullptr;
}

/// True when the field is present: optional fields carry a has_ flag, singular ones always are.
bool fieldPresent(const pb_field_iter_t &it)
{
    if (PB_HTYPE(it.type) == PB_HTYPE_OPTIONAL && it.pSize)
        return *(const bool *)it.pSize;
    return true;
}

int64_t readScalar(const pb_field_iter_t &it, const FieldHint *h, const Options &opt)
{
    switch (PB_LTYPE(it.type)) {
    case PB_LTYPE_BOOL:
        return *(const bool *)it.pData ? 1 : 0;
    case PB_LTYPE_VARINT:
    case PB_LTYPE_SVARINT:
        return it.data_size == 8 ? *(const int64_t *)it.pData : *(const int32_t *)it.pData;
    case PB_LTYPE_UVARINT:
        return it.data_size == 8 ? (int64_t) * (const uint64_t *)it.pData : (int64_t) * (const uint32_t *)it.pData;
    case PB_LTYPE_FIXED32:
        if ((h && h->isFloat) || (!h && opt.fixed32IsFloat)) {
            float f;
            memcpy(&f, it.pData, sizeof(f));
            uint16_t sc = (h && h->scale) ? h->scale : opt.floatScale;
            return (int64_t)(f < 0 ? f * sc - 0.5f : f * sc + 0.5f);
        }
        return (int64_t) * (const uint32_t *)it.pData;
    default:
        return 0;
    }
}

void writeScalar(pb_field_iter_t &it, const FieldHint *h, const Options &opt, int64_t v)
{
    if (PB_HTYPE(it.type) == PB_HTYPE_OPTIONAL && it.pSize)
        *(bool *)it.pSize = true;
    switch (PB_LTYPE(it.type)) {
    case PB_LTYPE_BOOL:
        *(bool *)it.pData = v != 0;
        return;
    case PB_LTYPE_VARINT:
    case PB_LTYPE_SVARINT:
        if (it.data_size == 8)
            *(int64_t *)it.pData = v;
        else
            *(int32_t *)it.pData = (int32_t)v;
        return;
    case PB_LTYPE_UVARINT:
        if (it.data_size == 8)
            *(uint64_t *)it.pData = (uint64_t)v;
        else
            *(uint32_t *)it.pData = (uint32_t)v;
        return;
    case PB_LTYPE_FIXED32:
        if ((h && h->isFloat) || (!h && opt.fixed32IsFloat)) {
            uint16_t sc = (h && h->scale) ? h->scale : opt.floatScale;
            float f = (float)v / (float)sc;
            memcpy(it.pData, &f, sizeof(f));
        } else {
            *(uint32_t *)it.pData = (uint32_t)v;
        }
        return;
    default:
        return;
    }
}

// ---------------------------------------------------------------- column encodings

enum ColCode : uint8_t {
    COL_RAW = 0,      ///< zigzag varints, absolute
    COL_DELTA = 1,    ///< zigzag varints, first absolute then differences
    COL_CONST = 2,    ///< one value, repeated
    COL_RAGGED = 0x4, ///< flag, not a code: a presence varint follows. Bit 3 is still spare.
};

/// Every reading present. A column matching this carries no mask.
inline uint32_t fullMask(uint8_t n)
{
    return n >= 32 ? 0xFFFFFFFFu : (uint32_t)(((uint32_t)1 << n) - 1);
}

inline uint8_t popcount32(uint32_t x)
{
    uint8_t c = 0;
    for (; x; x >>= 1)
        c = (uint8_t)(c + (x & 1u));
    return c;
}

/// One column's working set. Gathered, encoded and discarded before the next, so peak
/// scratch is a single column rather than the whole table - 194 B against 3.1 KB at the
/// default caps, which matters on nRF52 where the store already costs stack.
struct Column {
    uint8_t tag;
    uint8_t resLog2;
    uint8_t rows;                     ///< values held: the popcounts of every channel, summed
    uint8_t chans;                    ///< 1, or the family's channel count
    uint32_t present[4];              ///< per channel, bit i set when reading i carries it
    int64_t v[BISCUIT_MAX_BATCH * 4]; ///< a family of up to 4 channels, channel-major
};

/// Values a column holds: every channel's present readings, summed.
inline uint8_t rowsOfMasks(const uint32_t *present, uint8_t chans)
{
    uint8_t rows = 0;
    for (uint8_t ch = 0; ch < chans; ch++)
        rows = (uint8_t)(rows + popcount32(present[ch]));
    return rows;
}

/// True when any channel is missing any reading, so masks must travel.
inline bool isRagged(const uint32_t *present, uint8_t chans, uint8_t n)
{
    for (uint8_t ch = 0; ch < chans; ch++)
        if (present[ch] != fullMask(n))
            return true;
    return false;
}

#if BISCUIT_MAX_TIER >= 4
/// Largest divisor every value shares. Sensor readings are often integer multiples of a
/// hardware quantum - an INA226 bus LSB is 1.25 mV, an INA3221 shunt step 400 uA - so
/// dividing it out is exact and shrinks every value, unlike a decimal shift which rounds.
int64_t gcdOfColumn(const int64_t *v, uint8_t n)
{
    int64_t g = 0;
    for (uint8_t i = 0; i < n; i++) {
        int64_t a = v[i] < 0 ? -v[i] : v[i], b = g;
        while (a) { // Euclid
            int64_t t = b % a;
            b = a;
            a = t;
        }
        g = b;
    }
    return g > 1 ? g : 1;
}
#endif

size_t sizeRaw(const int64_t *v, uint8_t n)
{
    size_t s = 0;
    for (uint8_t i = 0; i < n; i++)
        s += varintLen(zz(v[i]));
    return s;
}
size_t sizeDelta(const int64_t *v, uint8_t n)
{
    size_t s = varintLen(zz(v[0]));
    for (uint8_t i = 1; i < n; i++)
        s += varintLen(zz(v[i] - v[i - 1]));
    return s;
}

#if BISCUIT_MAX_TIER >= 4
/// Width minimising packed bits plus escape varints. Escape = all-ones at that width.
uint8_t chooseWidth(const int64_t *v, uint8_t n, size_t *bytesOut)
{
    uint8_t bestW = 1;
    size_t best = SIZE_MAX;
    for (uint8_t w = 1; w <= 32; w++) {
        uint64_t esc = (w >= 64) ? UINT64_MAX : ((uint64_t)1 << w) - 1;
        size_t extra = 0;
        for (uint8_t i = 1; i < n; i++) {
            uint64_t d = zz(v[i] - v[i - 1]);
            if (d >= esc)
                extra += varintLen(d);
        }
        size_t tot = (((size_t)(n - 1) * w) + 7) / 8 + extra;
        if (tot < best) {
            best = tot;
            bestW = w;
        }
    }
    *bytesOut = best;
    return bestW;
}

struct BitWriter {
    uint8_t *buf;
    size_t cap, bit;
    bool put(uint64_t v, uint8_t w)
    {
        while (w--) {
            size_t byte = bit >> 3;
            if (byte >= cap)
                return false;
            if ((v >> w) & 1)
                buf[byte] |= (uint8_t)(0x80u >> (bit & 7));
            bit++;
        }
        return true;
    }
    size_t bytes() const { return (bit + 7) / 8; }
};

struct BitReader {
    const uint8_t *buf;
    size_t cap, bit;
    bool get(uint8_t w, uint64_t *out)
    {
        uint64_t v = 0;
        while (w--) {
            size_t byte = bit >> 3;
            if (byte >= cap)
                return false;
            v = (v << 1) | ((buf[byte] >> (7 - (bit & 7))) & 1);
            bit++;
        }
        *out = v;
        return true;
    }
};
#endif // BISCUIT_MAX_TIER >= 4

/// Gather one column channel-major, applying the resolution shift. Each channel keeps its own
/// presence mask, so a family survives both a sensor that drops out mid-batch and a channel the
/// node never populates - the 93% of PowerMetrics senders that report ch3 alone.
bool gatherColumn(const pb_msgdesc_t *desc, const void *const *msgs, uint8_t n, const Options &opt, uint8_t tier, uint8_t tag,
                  Column &col)
{
    const FieldHint *h = findHint(opt, tag);
    col.tag = tag;
    col.resLog2 = (tier >= TIER_RESOLUTION && h) ? h->resLog2 : 0;
    const FieldFamily *fam = familyLedBy(opt, tag);
    // Channel-major: every sample of channel 1, then channel 2, and so on. Reading-major
    // would make each delta a channel-to-channel difference, which does not compress.
    const uint8_t chans = fam ? fam->count : 1;
    col.chans = chans;
    col.rows = 0;
    for (uint8_t ch = 0; ch < 4; ch++)
        col.present[ch] = 0;
    for (uint8_t ch = 0; ch < chans; ch++) {
        const uint8_t tg = fam ? fam->tags[ch] : tag;
        const FieldHint *ch_h = fam ? findHint(opt, tg) : h;
        for (uint8_t i = 0; i < n; i++) {
            pb_field_iter_t jt;
            if (!pb_field_iter_begin_const(&jt, desc, msgs[i]) || !pb_field_iter_find(&jt, tg) || !fieldPresent(jt))
                continue;
            col.present[ch] |= (uint32_t)1 << i;
            col.v[col.rows] = readScalar(jt, ch_h, opt);
            if (col.resLog2)
                col.v[col.rows] >>= col.resLog2;
            col.rows++;
        }
    }
    return col.rows > 0;
}

/// A column's channel count. Both ends read it from the shared family table, so it needs no
/// wire byte; which readings each channel answered for does travel, because only the sender
/// knows it.
uint8_t chansOf(const Options &opt, uint8_t tag)
{
    const FieldFamily *fam = familyLedBy(opt, tag);
    return fam ? fam->count : 1;
}

/// Quantised timestamp. opt.timeRes is 1 - exact - unless the user has chosen a coarser
/// stamp, which only tier 3 and above honour.
int64_t timeQuantum(const Options &opt, uint8_t tier)
{
    return (tier >= TIER_RESOLUTION && opt.timeRes > 1) ? (int64_t)opt.timeRes : 1;
}

static_assert(BISCUIT_MAX_TIER <= 7, "the tier must fit in three bits of the header byte");
static_assert(BISCUIT_MAX_BATCH <= 31, "n must fit in five bits, the top three holding repeats");

/// One byte standing for everything the receiver must know that is NOT on the wire: the family
/// table and how each field's float becomes an integer. Two ends that disagree then fail rather
/// than swapping plausible wrong numbers - a scale mismatch silently moves a decimal point.
///
/// resLog2 is deliberately excluded. It is a user resolution choice that varies per batch and
/// travels in the packet, so a receiver with different resolution settings must still decode.
uint8_t profileSignature(const Options &opt)
{
    uint8_t sig = (uint8_t)(opt.fixed32IsFloat ? 0xA5 : 0x5A);
    sig = (uint8_t)(sig * 31u + (opt.floatScale & 0xFF));
    sig = (uint8_t)(sig * 31u + (opt.floatScale >> 8));
    for (uint8_t i = 0; i < opt.hintCount; i++) {
        sig = (uint8_t)(sig * 31u + opt.hints[i].tag);
        sig = (uint8_t)(sig * 31u + (opt.hints[i].isFloat ? 1u : 0u));
        sig = (uint8_t)(sig * 31u + (opt.hints[i].scale & 0xFF));
        sig = (uint8_t)(sig * 31u + (opt.hints[i].scale >> 8));
    }
    sig = (uint8_t)(sig * 31u + opt.familyCount);
    for (uint8_t i = 0; i < opt.familyCount; i++) {
        sig = (uint8_t)(sig * 31u + opt.families[i].count);
        for (uint8_t j = 0; j < opt.families[i].count; j++)
            sig = (uint8_t)(sig * 31u + opt.families[i].tags[j]);
    }
    return sig;
}
} // namespace

// ---------------------------------------------------------------- encode

/// One batch at one tier. encode() below drives this once per candidate tier and keeps the
/// smallest, so this never chooses a tier itself.
Result encodeAtTier(const pb_msgdesc_t *desc, const void *const *msgs, uint8_t n, const uint32_t *times, uint8_t *out, size_t cap,
                    const Options &opt, uint8_t tier)
{
    Result r;
    if (!desc || !msgs || !times || !out || n == 0 || n > BISCUIT_MAX_BATCH)
        return r;
    if (tier > BISCUIT_MAX_TIER)
        tier = BISCUIT_MAX_TIER;
    if (tier < TIER_COLUMNAR)
        return r;

    // What the accumulator would have sent unencoded, for the never-inflate check below.
    for (uint8_t i = 0; i < n; i++) {
        size_t one = 0;
        if (pb_get_encoded_size(&one, desc, msgs[i]))
            r.baseline += one + 6; // Telemetry wrapper: time fixed32 plus the oneof tag and length
    }

    // Pass 1: which tags qualify. A field must be encodable and carried by at least one
    // reading; gaps travel as a presence mask. Only the tag list is kept here.
    uint8_t tags[BISCUIT_MAX_COLUMNS];
    uint8_t nCols = 0;
    pb_field_iter_t it;
    if (!pb_field_iter_begin_const(&it, desc, msgs[0]))
        return r;
    do {
        if (!encodable(it.type) || it.tag > 64)
            continue;
        // A family rides in its leader's column, so its other channels claim no column of their own.
        if (inFamilyButNotLeader(opt, (uint8_t)it.tag))
            continue;
        if (nCols >= BISCUIT_MAX_COLUMNS)
            break;
        // One reading of one channel is enough. Every gap - a sensor that stopped answering, or
        // a channel the node never wired up - travels as a mask rather than costing the column.
        const FieldFamily *fam1 = familyLedBy(opt, (uint8_t)it.tag);
        const uint8_t chans1 = fam1 ? fam1->count : 1;
        bool seen = false;
        for (uint8_t ch = 0; ch < chans1 && !seen; ch++) {
            const uint8_t tg = fam1 ? fam1->tags[ch] : (uint8_t)it.tag;
            for (uint8_t i = 0; i < n; i++) {
                pb_field_iter_t jt;
                if (pb_field_iter_begin_const(&jt, desc, msgs[i]) && pb_field_iter_find(&jt, tg) && fieldPresent(jt)) {
                    seen = true;
                    break;
                }
            }
        }
        if (seen)
            tags[nCols++] = (uint8_t)it.tag;
    } while (pb_field_iter_next(&it));

    if (nCols == 0)
        return r;

    size_t at = 0;
    if (cap < 4)
        return r;
    // Byte 0: version, then the tier with bit 3 spare for the families flag. Byte 1: n, which
    // needs five bits, with the declared repeat count in the top three.
    out[at++] = (uint8_t)((VERSION << 4) | (tier & 0x07));
    if (opt.repeats > 7 || n > 31)
        return r; // caller asked for more than the header can declare
    out[at++] = (uint8_t)(n | (opt.repeats << 5));
    at = putVarint(out, cap, at, opt.context);
    if (at == SIZE_MAX)
        return r;
    // A coarser stamp is lossy in the time domain only; worstErr stays a value-domain figure.
    const int64_t tq = timeQuantum(opt, tier);
    if (tq > 1)
        r.lossless = false;
    at = putVarint(out, cap, at, (uint64_t)((int64_t)times[0] / tq));
    if (at == SIZE_MAX)
        return r;

    // Column identification: a bitmap from tier 2, an explicit tag list below it.
    if (tier >= TIER_BITMAP) {
        uint64_t mask = 0;
        for (uint8_t c = 0; c < nCols; c++)
            mask |= (uint64_t)1 << (tags[c] - 1);
        at = putVarint(out, cap, at, mask);
    } else {
        at = putVarint(out, cap, at, nCols);
        for (uint8_t c = 0; c < nCols && at != SIZE_MAX; c++)
            at = putVarint(out, cap, at, tags[c]);
    }
    if (at == SIZE_MAX)
        return r;

    // Everything the receiver needs that is not on the wire, in one byte, so a mismatch fails
    // instead of decoding into plausible wrong numbers.
    if (at >= cap)
        return r;
    out[at++] = profileSignature(opt);
    // Time column: gaps, or second differences from tier 2.
    for (uint8_t i = 1; i < n && at != SIZE_MAX; i++) {
        int64_t g = (int64_t)times[i] / tq - (int64_t)times[i - 1] / tq;
        if (tier >= TIER_BITMAP && i > 1)
            g -= (int64_t)times[i - 1] / tq - (int64_t)times[i - 2] / tq;
        at = putVarint(out, cap, at, zz(g));
    }
    if (at == SIZE_MAX)
        return r;

    Column col; // reused for every column; see the note on Column above

#if BISCUIT_MAX_TIER >= 4
    if (tier >= TIER_PACKED) {
        // Tier 4 drops per-column framing entirely: a 5-bit width table, one divisor and seed
        // per column, then every column's deltas concatenated into a single unaligned bit area
        // with the escapes trailing it. Framing is what dominates a wide message at small n.
        uint8_t widths[BISCUIT_MAX_COLUMNS], resl[BISCUIT_MAX_COLUMNS];
        uint32_t pres[BISCUIT_MAX_COLUMNS][4];
        uint8_t chn[BISCUIT_MAX_COLUMNS];
        int64_t seeds[BISCUIT_MAX_COLUMNS], divs[BISCUIT_MAX_COLUMNS];
        size_t totalBits = 0;
        for (uint8_t c = 0; c < nCols; c++) {
            if (!gatherColumn(desc, msgs, n, opt, tier, tags[c], col))
                return r;
            if (col.resLog2) {
                r.lossless = false;
                uint32_t e = (uint32_t)1 << (col.resLog2 - 1);
                if (e > r.worstErr)
                    r.worstErr = e;
            }
            // A quantum the values already share divides out exactly. Skipped where a
            // resolution shift applies, since that has already scaled the column.
            divs[c] = col.resLog2 ? 1 : gcdOfColumn(col.v, col.rows);
            if (divs[c] > 1)
                for (uint8_t i = 0; i < col.rows; i++)
                    col.v[i] /= divs[c];
            seeds[c] = col.v[0];
            resl[c] = col.resLog2;
            chn[c] = col.chans;
            for (uint8_t ch = 0; ch < 4; ch++)
                pres[c][ch] = col.present[ch];
            size_t unused = 0;
            widths[c] = chooseWidth(col.v, col.rows, &unused);
            totalBits += (size_t)(col.rows - 1) * widths[c];
        }

        // Tier 4 has no column header, so the two things tiers 1-3 keep in the column code byte
        // - the resolution shift and the presence mask - live here instead. Reading either from
        // the receiver's own configuration is how a mismatch returns silently wrong numbers.
        //
        // One flags varint gates both, and is a single zero byte in the ordinary case. Bit 0
        // means a 4-bit resolution table follows, covering every column; bit 1 means a ragged
        // section follows, naming the ragged columns and then their masks.
        uint64_t flags = 0;
        for (uint8_t c = 0; c < nCols; c++)
            if (resl[c]) {
                flags |= 1;
                break;
            }
        uint64_t ragMask = 0;
        for (uint8_t c = 0; c < nCols; c++)
            if (isRagged(pres[c], chn[c], n))
                ragMask |= (uint64_t)1 << c;
        if (ragMask)
            flags |= 2;
        at = putVarint(out, cap, at, flags);
        if (at == SIZE_MAX)
            return r;
        if (flags & 1) {
            const size_t rtabBytes = ((size_t)nCols * 4 + 7) / 8;
            if (at + rtabBytes > cap)
                return r;
            memset(out + at, 0, rtabBytes);
            BitWriter rtab{out + at, rtabBytes, 0};
            for (uint8_t c = 0; c < nCols; c++)
                if (!rtab.put((uint64_t)(resl[c] & 0x0F), 4))
                    return r;
            at += rtabBytes;
        }
        if (flags & 2) {
            at = putVarint(out, cap, at, ragMask);
            for (uint8_t c = 0; c < nCols && at != SIZE_MAX; c++)
                if (ragMask & ((uint64_t)1 << c))
                    for (uint8_t ch = 0; ch < chn[c] && at != SIZE_MAX; ch++)
                        at = putVarint(out, cap, at, pres[c][ch]);
            if (at == SIZE_MAX)
                return r;
        }

        // Width table: five bits per column, holding width - 1 so 1..32 fits.
        size_t wtabBytes = ((size_t)nCols * 5 + 7) / 8;
        if (at + wtabBytes > cap)
            return r;
        memset(out + at, 0, wtabBytes);
        BitWriter wtab{out + at, wtabBytes, 0};
        for (uint8_t c = 0; c < nCols; c++)
            if (!wtab.put((uint64_t)(widths[c] - 1), 5))
                return r;
        at += wtabBytes;

        for (uint8_t c = 0; c < nCols && at != SIZE_MAX; c++) {
            at = putVarint(out, cap, at, (uint64_t)divs[c]);
            if (at != SIZE_MAX)
                at = putVarint(out, cap, at, zz(seeds[c]));
        }
        if (at == SIZE_MAX)
            return r;

        // Escapes trail the bit area, so its length has to be known before either is written.
        const size_t bitBytes = (totalBits + 7) / 8;
        if (at + bitBytes > cap)
            return r;
        memset(out + at, 0, bitBytes);
        BitWriter bw{out + at, bitBytes, 0};
        size_t escAt = at + bitBytes;
        for (uint8_t c = 0; c < nCols; c++) {
            if (!gatherColumn(desc, msgs, n, opt, tier, tags[c], col))
                return r;
            if (divs[c] > 1)
                for (uint8_t i = 0; i < col.rows; i++)
                    col.v[i] /= divs[c];
            const uint64_t esc = ((uint64_t)1 << widths[c]) - 1;
            for (uint8_t i = 1; i < col.rows; i++) {
                const uint64_t d = zz(col.v[i] - col.v[i - 1]);
                if (!bw.put(d >= esc ? esc : d, widths[c]))
                    return r;
                if (d >= esc) {
                    escAt = putVarint(out, cap, escAt, d);
                    if (escAt == SIZE_MAX)
                        return r;
                }
            }
        }
        at = escAt;
    } else
#endif
        for (uint8_t c = 0; c < nCols; c++) {
            if (!gatherColumn(desc, msgs, n, opt, tier, tags[c], col))
                return r;
            if (col.resLog2) {
                r.lossless = false;
                uint32_t e = (uint32_t)1 << (col.resLog2 - 1);
                if (e > r.worstErr)
                    r.worstErr = e;
            }
            size_t sr = sizeRaw(col.v, col.rows), sd = sizeDelta(col.v, col.rows);
            bool isConst = true;
            for (uint8_t i = 1; i < col.rows; i++)
                if (col.v[i] != col.v[0]) {
                    isConst = false;
                    break;
                }
            uint8_t code = isConst ? COL_CONST : (sd <= sr ? COL_DELTA : COL_RAW);
            const bool ragged = isRagged(col.present, col.chans, n);
            if (ragged)
                code |= COL_RAGGED;
            if (at >= cap)
                return r;
            out[at++] = (uint8_t)(code | (col.resLog2 << 4));
            if (ragged)
                for (uint8_t ch = 0; ch < col.chans && at != SIZE_MAX; ch++)
                    at = putVarint(out, cap, at, col.present[ch]);
            if (at == SIZE_MAX)
                return r;

            if (code == COL_CONST) {
                at = putVarint(out, cap, at, zz(col.v[0]));
            } else if (code == COL_RAW) {
                for (uint8_t i = 0; i < col.rows && at != SIZE_MAX; i++)
                    at = putVarint(out, cap, at, zz(col.v[i]));
            } else {
                at = putVarint(out, cap, at, zz(col.v[0]));
                for (uint8_t i = 1; i < col.rows && at != SIZE_MAX; i++)
                    at = putVarint(out, cap, at, zz(col.v[i] - col.v[i - 1]));
            }
            if (at == SIZE_MAX)
                return r;
        }
    // A batch of one, or a message wide enough that column headers dominate, can encode larger
    // than the protobuf it replaces. Report that rather than shipping a worse packet.
    if (opt.neverInflate && r.baseline && at >= r.baseline) {
        r.size = 0;
        r.tier = 0;
        return r;
    }
    r.size = at;
    r.tier = tier;
    r.context = opt.context;
    return r;
}

/**
 * A higher tier is not always a smaller packet. Tier 4 spends a width table and a divisor and
 * seed per column before it encodes anything, and on a short batch that fixed cost outweighs
 * the framing it removes - measured on captured traffic, tier 4 is larger than tier 2 for 39%
 * of two-reading batches, by about two bytes. Tier 2's column bitmap likewise loses to tier 1's
 * explicit tag list when the only column has a high field number.
 *
 * So encode every tier up to the cap and keep the smallest. The tier travels in the header
 * already, so this costs the receiver nothing and needs no wire change; it costs the sender a
 * few passes over a short array before a transmission that is orders of magnitude dearer.
 * `maxTier` is a ceiling, not an instruction.
 */
Result encode(const pb_msgdesc_t *desc, const void *const *msgs, uint8_t n, const uint32_t *times, uint8_t *out, size_t cap,
              const Options &opt)
{
    const uint8_t top = opt.maxTier < BISCUIT_MAX_TIER ? opt.maxTier : (uint8_t)BISCUIT_MAX_TIER;
    Result best;
    uint8_t bestTier = 0;
    for (uint8_t t = TIER_COLUMNAR; t <= top; t++) {
        const Result r = encodeAtTier(desc, msgs, n, times, out, cap, opt, t);
        // baseline is a property of the batch, not the tier, so keep it even when nothing fits.
        if (r.baseline > best.baseline)
            best.baseline = r.baseline;
        if (r.size && (!bestTier || r.size < best.size)) {
            const size_t baseline = best.baseline;
            best = r;
            best.baseline = baseline;
            bestTier = t;
        }
    }
    if (!bestTier) {
        best.size = 0;
        best.tier = 0;
        return best;
    }
    // The loop left the last candidate in the buffer, which is not necessarily the winner.
    const size_t baseline = best.baseline;
    best = encodeAtTier(desc, msgs, n, times, out, cap, opt, bestTier);
    best.baseline = baseline;
    return best;
}

// ---------------------------------------------------------------- decode

uint8_t decode(const pb_msgdesc_t *desc, const uint8_t *in, size_t len, void *const *msgs, uint8_t maxN, uint32_t *times,
               const Options &opt, uint32_t *contextOut)
{
    if (!desc || !in || !msgs || !times || len < 4)
        return 0;
    if ((in[0] >> 4) != VERSION)
        return 0;
    const uint8_t tier = in[0] & 0x07;
    // A tier this build cannot decode must be refused, not reinterpreted. Below tier 4 the
    // packed branch is compiled out entirely, so a tier-4 packet would fall into the tier 1-3
    // column loop and read its bit area as column code bytes - three byte values in four are a
    // valid code, so it would decode plausible rubbish and report success.
    if (tier < TIER_COLUMNAR || tier > BISCUIT_MAX_TIER)
        return 0;
    const uint8_t n = in[1] & 0x1F;
    if (n == 0 || n > maxN || n > BISCUIT_MAX_BATCH)
        return 0;

    size_t at = 2;
    uint64_t ctx;
    at = getVarint(in, len, at, &ctx);
    if (at == SIZE_MAX)
        return 0;
    if (contextOut)
        *contextOut = (uint32_t)ctx;
    uint64_t t0;
    at = getVarint(in, len, at, &t0);
    if (at == SIZE_MAX)
        return 0;

    uint8_t tags[BISCUIT_MAX_COLUMNS];
    uint8_t nCols = 0;
    if (tier >= TIER_BITMAP) {
        uint64_t mask;
        at = getVarint(in, len, at, &mask);
        if (at == SIZE_MAX)
            return 0;
        for (uint8_t b = 0; b < 64 && nCols < BISCUIT_MAX_COLUMNS; b++)
            if (mask & ((uint64_t)1 << b))
                tags[nCols++] = (uint8_t)(b + 1);
    } else {
        uint64_t c;
        at = getVarint(in, len, at, &c);
        if (at == SIZE_MAX || c > BISCUIT_MAX_COLUMNS)
            return 0;
        nCols = (uint8_t)c;
        for (uint8_t i = 0; i < nCols; i++) {
            uint64_t tg;
            at = getVarint(in, len, at, &tg);
            if (at == SIZE_MAX)
                return 0;
            tags[i] = (uint8_t)tg;
        }
    }

    // The sender must have interpreted values the way we will. Families, scales and float
    // handling are all in here; a mismatch is refused rather than silently misread.
    if (at >= len || in[at++] != profileSignature(opt))
        return 0;
    // Gaps are in quanta, so each stamp is rebuilt from the running quantised count.
    const int64_t tq = timeQuantum(opt, tier);
    int64_t qt = (int64_t)t0;
    times[0] = (uint32_t)(qt * tq);
    int64_t prevGap = 0;
    for (uint8_t i = 1; i < n; i++) {
        uint64_t z;
        at = getVarint(in, len, at, &z);
        if (at == SIZE_MAX)
            return 0;
        int64_t g = unzz(z);
        if (tier >= TIER_BITMAP && i > 1)
            g += prevGap;
        qt += g;
        times[i] = (uint32_t)(qt * tq);
        prevGap = g;
    }

    // Scatter one column's values back, skipping the readings its presence mask says never
    // carried it - those keep has_field false rather than being invented. Channel ch owns the
    // block starting at ch * popcount(present), matching the encoder's channel-major layout;
    // a full mask and one channel is the plain case.
    auto scatter = [&](uint8_t tag, const int64_t *v, uint8_t chans, uint8_t resLog2, const uint32_t *present) -> bool {
        const FieldFamily *fam = familyLedBy(opt, tag);
        uint16_t base = 0;
        for (uint8_t ch = 0; ch < chans; ch++) {
            const uint8_t tg = fam ? fam->tags[ch] : tag;
            const FieldHint *h = findHint(opt, tg);
            uint8_t k = 0;
            for (uint8_t i = 0; i < n; i++) {
                if (!((present[ch] >> i) & 1u))
                    continue;
                pb_field_iter_t jt;
                if (!pb_field_iter_begin(&jt, desc, msgs[i]) || !pb_field_iter_find(&jt, tg))
                    return false;
                writeScalar(jt, h, opt, resLog2 ? (v[base + k] << resLog2) : v[base + k]);
                k++;
            }
            base = (uint16_t)(base + k); // channels are packed back to back, gaps removed
        }
        return true;
    };

#if BISCUIT_MAX_TIER >= 4
    if (tier >= TIER_PACKED) {
        uint8_t widths[BISCUIT_MAX_COLUMNS], resl[BISCUIT_MAX_COLUMNS];
        uint32_t pres[BISCUIT_MAX_COLUMNS][4];
        for (uint8_t c = 0; c < BISCUIT_MAX_COLUMNS; c++)
            for (uint8_t ch = 0; ch < 4; ch++)
                pres[c][ch] = fullMask(n);
        // Resolution shifts and presence masks come off the wire, not from our own hints, so a
        // receiver configured differently from the sender rebuilds the same values.
        uint64_t flags = 0;
        at = getVarint(in, len, at, &flags);
        if (at == SIZE_MAX)
            return 0;
        memset(resl, 0, sizeof(resl));
        if (flags & 1) {
            const size_t rtabBytes = ((size_t)nCols * 4 + 7) / 8;
            if (at + rtabBytes > len)
                return 0;
            BitReader rtab{in + at, rtabBytes, 0};
            for (uint8_t c = 0; c < nCols; c++) {
                uint64_t v;
                if (!rtab.get(4, &v))
                    return 0;
                resl[c] = (uint8_t)v;
            }
            at += rtabBytes;
        }
        if (flags & 2) {
            uint64_t ragMask = 0;
            at = getVarint(in, len, at, &ragMask);
            if (at == SIZE_MAX)
                return 0;
            for (uint8_t c = 0; c < nCols; c++)
                if (ragMask & ((uint64_t)1 << c)) {
                    const uint8_t chans = chansOf(opt, tags[c]);
                    uint32_t any = 0;
                    for (uint8_t ch = 0; ch < chans; ch++) {
                        uint64_t m;
                        at = getVarint(in, len, at, &m);
                        if (at == SIZE_MAX || (m & ~(uint64_t)fullMask(n)))
                            return 0;
                        pres[c][ch] = (uint32_t)m;
                        any |= (uint32_t)m;
                    }
                    if (!any)
                        return 0; // a column with no value anywhere is not a column
                }
        }

        const size_t wtabBytes = ((size_t)nCols * 5 + 7) / 8;
        if (at + wtabBytes > len)
            return 0;
        BitReader wtab{in + at, wtabBytes, 0};
        for (uint8_t c = 0; c < nCols; c++) {
            uint64_t w;
            if (!wtab.get(5, &w))
                return 0;
            widths[c] = (uint8_t)w + 1;
        }
        at += wtabBytes;

        int64_t seeds[BISCUIT_MAX_COLUMNS], divs[BISCUIT_MAX_COLUMNS];
        size_t totalBits = 0;
        for (uint8_t c = 0; c < nCols; c++) {
            uint64_t g, s;
            at = getVarint(in, len, at, &g);
            if (at == SIZE_MAX || g == 0)
                return 0;
            at = getVarint(in, len, at, &s);
            if (at == SIZE_MAX)
                return 0;
            divs[c] = (int64_t)g;
            seeds[c] = unzz(s);
            totalBits += (size_t)(rowsOfMasks(pres[c], chansOf(opt, tags[c])) - 1) * widths[c];
        }

        const size_t bitBytes = (totalBits + 7) / 8;
        if (at + bitBytes > len)
            return 0;
        BitReader br{in + at, bitBytes, 0};
        size_t escAt = at + bitBytes;
        for (uint8_t c = 0; c < nCols; c++) {
            const uint8_t chans = chansOf(opt, tags[c]);
            const uint8_t rows = rowsOfMasks(pres[c], chans);
            if (rows == 0)
                return 0;
            const uint64_t esc = ((uint64_t)1 << widths[c]) - 1;
            int64_t v[BISCUIT_MAX_BATCH * 4];
            int64_t acc = seeds[c];
            v[0] = acc * divs[c];
            for (uint8_t i = 1; i < rows; i++) {
                uint64_t d;
                if (!br.get(widths[c], &d))
                    return 0;
                if (d == esc) {
                    escAt = getVarint(in, len, escAt, &d);
                    if (escAt == SIZE_MAX)
                        return 0;
                }
                acc += unzz(d);
                v[i] = acc * divs[c];
            }
            if (!scatter(tags[c], v, chans, resl[c], pres[c]))
                return 0;
        }
        at = escAt;
    } else
#endif
        for (uint8_t c = 0; c < nCols; c++) {
            if (at >= len)
                return 0;
            const uint8_t codeByte = in[at++];
            // Bit 3 is reserved and must be zero, so a corrupted byte is refused rather than
            // quietly reinterpreted as a code the sender did not write.
            if (codeByte & 0x08)
                return 0;
            const uint8_t code = codeByte & 0x03, resLog2 = codeByte >> 4;
            const uint8_t chans = chansOf(opt, tags[c]);
            uint32_t present[4] = {fullMask(n), fullMask(n), fullMask(n), fullMask(n)};
            if (codeByte & COL_RAGGED) {
                uint32_t any = 0;
                for (uint8_t ch = 0; ch < chans; ch++) {
                    uint64_t m;
                    at = getVarint(in, len, at, &m);
                    if (at == SIZE_MAX || (m & ~(uint64_t)fullMask(n)))
                        return 0;
                    present[ch] = (uint32_t)m;
                    any |= (uint32_t)m;
                }
                if (!any)
                    return 0;
            }
            const uint8_t rows = rowsOfMasks(present, chans);
            if (rows == 0)
                return 0;
            int64_t v[BISCUIT_MAX_BATCH * 4];
            uint64_t z;
            if (code == COL_CONST) {
                at = getVarint(in, len, at, &z);
                if (at == SIZE_MAX)
                    return 0;
                for (uint8_t i = 0; i < rows; i++)
                    v[i] = unzz(z);
            } else if (code == COL_RAW) {
                for (uint8_t i = 0; i < rows; i++) {
                    at = getVarint(in, len, at, &z);
                    if (at == SIZE_MAX)
                        return 0;
                    v[i] = unzz(z);
                }
            } else if (code == COL_DELTA) {
                at = getVarint(in, len, at, &z);
                if (at == SIZE_MAX)
                    return 0;
                v[0] = unzz(z);
                for (uint8_t i = 1; i < rows; i++) {
                    at = getVarint(in, len, at, &z);
                    if (at == SIZE_MAX)
                        return 0;
                    v[i] = v[i - 1] + unzz(z);
                }
            } else {
                return 0;
            }
            if (!scatter(tags[c], v, chans, resLog2, present))
                return 0;
        }
    return n;
}

bool peekContext(const uint8_t *in, size_t len, uint32_t *contextOut)
{
    if (!in || len < 3 || (in[0] >> 4) != VERSION)
        return false;
    uint64_t ctx;
    if (getVarint(in, len, 2, &ctx) == SIZE_MAX)
        return false;
    if (contextOut)
        *contextOut = (uint32_t)ctx;
    return true;
}

uint8_t peekCount(const uint8_t *in, size_t len)
{
    return (in && len >= 2 && (in[0] >> 4) == VERSION) ? (uint8_t)(in[1] & 0x1F) : 0;
}
uint8_t peekTier(const uint8_t *in, size_t len)
{
    return (in && len >= 1 && (in[0] >> 4) == VERSION) ? (uint8_t)(in[0] & 0x07) : 0;
}
uint8_t peekRepeats(const uint8_t *in, size_t len)
{
    return (in && len >= 2 && (in[0] >> 4) == VERSION) ? (uint8_t)(in[1] >> 5) : 0;
}

} // namespace biscuit

#endif // MESHTASTIC_BISCUIT_ENABLED
