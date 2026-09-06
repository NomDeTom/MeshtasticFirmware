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
    COL_RAW = 0,   ///< zigzag varints, absolute
    COL_DELTA = 1, ///< zigzag varints, first absolute then differences
    COL_CONST = 2, ///< one value, repeated
#if BISCUIT_MAX_TIER >= 4
    COL_PACKED = 3, ///< seed varint, width byte, fixed-width deltas with an escape
#endif
#if BISCUIT_MAX_TIER >= 3
    COL_GCD = 4, ///< divisor varint, then zigzag deltas of value/divisor. Lossless.
#endif
};

/// One column's working set. Gathered, encoded and discarded before the next, so peak
/// scratch is a single column rather than the whole table - 194 B against 3.1 KB at the
/// default caps, which matters on nRF52 where the store already costs stack.
struct Column {
    uint8_t tag;
    uint8_t resLog2;
    uint8_t rows;                     ///< values held: n, or n x family size when stacked
    int64_t v[BISCUIT_MAX_BATCH * 4]; ///< a family of up to 4 channels, channel-major
};

#if BISCUIT_MAX_TIER >= 3
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

size_t sizeGcdDelta(const int64_t *v, uint8_t n, int64_t g)
{
    size_t s = varintLen(g) + varintLen(zz(v[0] / g));
    for (uint8_t i = 1; i < n; i++)
        s += varintLen(zz(v[i] / g - v[i - 1] / g));
    return s;
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

} // namespace

// ---------------------------------------------------------------- encode

Result encode(const pb_msgdesc_t *desc, const void *const *msgs, uint8_t n, const uint32_t *times, uint8_t *out, size_t cap,
              const Options &opt)
{
    Result r;
    if (!desc || !msgs || !times || !out || n == 0 || n > BISCUIT_MAX_BATCH)
        return r;

    uint8_t tier = opt.maxTier < BISCUIT_MAX_TIER ? opt.maxTier : (uint8_t)BISCUIT_MAX_TIER;
    if (tier < TIER_COLUMNAR)
        return r;

    // What the accumulator would have sent unencoded, for the never-inflate check below.
    for (uint8_t i = 0; i < n; i++) {
        size_t one = 0;
        if (pb_get_encoded_size(&one, desc, msgs[i]))
            r.baseline += one + 6; // Telemetry wrapper: time fixed32 plus the oneof tag and length
    }

    // Pass 1: which tags qualify. A field must be encodable and present in every reading.
    // Only the tag list is kept; values are gathered per column in pass 2.
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
        // A leader qualifies only if every channel of its family is present in every reading.
        const FieldFamily *fam1 = familyLedBy(opt, (uint8_t)it.tag);
        const uint8_t chans1 = fam1 ? fam1->count : 1;
        bool ok = true;
        for (uint8_t ch = 0; ch < chans1 && ok; ch++) {
            const uint8_t tg = fam1 ? fam1->tags[ch] : (uint8_t)it.tag;
            for (uint8_t i = 0; i < n; i++) {
                pb_field_iter_t jt;
                if (!pb_field_iter_begin_const(&jt, desc, msgs[i]) || !pb_field_iter_find(&jt, tg) || !fieldPresent(jt)) {
                    ok = false;
                    break;
                }
            }
        }
        if (ok)
            tags[nCols++] = (uint8_t)it.tag;
    } while (pb_field_iter_next(&it));

    if (nCols == 0)
        return r;

    size_t at = 0;
    if (cap < 4)
        return r;
    out[at++] = (uint8_t)((VERSION << 4) | tier);
    out[at++] = n;
    at = putVarint(out, cap, at, opt.context);
    if (at == SIZE_MAX)
        return r;
    at = putVarint(out, cap, at, times[0]);
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

    // Time column: gaps, or second differences from tier 2.
    for (uint8_t i = 1; i < n && at != SIZE_MAX; i++) {
        int64_t g = (int64_t)times[i] - (int64_t)times[i - 1];
        if (tier >= TIER_BITMAP && i > 1)
            g -= (int64_t)times[i - 1] - (int64_t)times[i - 2];
        at = putVarint(out, cap, at, zz(g));
    }
    if (at == SIZE_MAX)
        return r;

    Column col; // reused for every column; see the note on Column above
    for (uint8_t c = 0; c < nCols; c++) {
        const FieldHint *h = findHint(opt, tags[c]);
        col.tag = tags[c];
        col.resLog2 = (tier >= TIER_RESOLUTION && h) ? h->resLog2 : 0;
        const FieldFamily *fam = familyLedBy(opt, col.tag);
        // Channel-major: every sample of channel 1, then channel 2, and so on. Reading-major
        // would make each delta a channel-to-channel difference, which does not compress.
        const uint8_t chans = fam ? fam->count : 1;
        col.rows = 0;
        for (uint8_t ch = 0; ch < chans; ch++) {
            const uint8_t tg = fam ? fam->tags[ch] : col.tag;
            const FieldHint *ch_h = fam ? findHint(opt, tg) : h;
            for (uint8_t i = 0; i < n; i++) {
                pb_field_iter_t jt;
                if (!pb_field_iter_begin_const(&jt, desc, msgs[i]) || !pb_field_iter_find(&jt, tg) || !fieldPresent(jt))
                    return r;
                col.v[col.rows] = readScalar(jt, ch_h, opt);
                if (col.resLog2)
                    col.v[col.rows] >>= col.resLog2;
                col.rows++;
            }
        }
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
#if BISCUIT_MAX_TIER >= 3
        int64_t gdiv = 1;
        if (tier >= TIER_RESOLUTION && !isConst) {
            gdiv = gcdOfColumn(col.v, col.rows);
            if (gdiv > 1 && sizeGcdDelta(col.v, col.rows, gdiv) < (code == COL_DELTA ? sd : sr))
                code = COL_GCD;
        }
#endif
#if BISCUIT_MAX_TIER >= 4
        size_t packedBytes = SIZE_MAX;
        uint8_t width = 0;
        if (tier >= TIER_PACKED && !isConst && col.rows > 1) {
            width = chooseWidth(col.v, col.rows, &packedBytes);
            packedBytes += varintLen(zz(col.v[0])) + 1; // seed + width byte
            if (packedBytes < (code == COL_DELTA ? sd : sr))
                code = COL_PACKED;
        }
#endif
        if (at >= cap)
            return r;
        out[at++] = (uint8_t)(code | (col.resLog2 << 4));

        if (code == COL_CONST) {
            at = putVarint(out, cap, at, zz(col.v[0]));
        } else if (code == COL_RAW) {
            for (uint8_t i = 0; i < col.rows && at != SIZE_MAX; i++)
                at = putVarint(out, cap, at, zz(col.v[i]));
        } else if (code == COL_DELTA) {
            at = putVarint(out, cap, at, zz(col.v[0]));
            for (uint8_t i = 1; i < col.rows && at != SIZE_MAX; i++)
                at = putVarint(out, cap, at, zz(col.v[i] - col.v[i - 1]));
        }
#if BISCUIT_MAX_TIER >= 3
        else if (code == COL_GCD) {
            at = putVarint(out, cap, at, (uint64_t)gdiv);
            if (at != SIZE_MAX)
                at = putVarint(out, cap, at, zz(col.v[0] / gdiv));
            for (uint8_t i = 1; i < col.rows && at != SIZE_MAX; i++)
                at = putVarint(out, cap, at, zz(col.v[i] / gdiv - col.v[i - 1] / gdiv));
        }
#endif
#if BISCUIT_MAX_TIER >= 4
        else {
            at = putVarint(out, cap, at, zz(col.v[0]));
            if (at == SIZE_MAX || at >= cap)
                return r;
            out[at++] = width;
            uint64_t esc = ((uint64_t)1 << width) - 1;
            BitWriter bw{out + at, cap - at, 0};
            memset(out + at, 0, cap - at);
            for (uint8_t i = 1; i < col.rows; i++) {
                uint64_t d = zz(col.v[i] - col.v[i - 1]);
                if (!bw.put(d >= esc ? esc : d, width))
                    return r;
            }
            at += bw.bytes();
            for (uint8_t i = 1; i < col.rows && at != SIZE_MAX; i++) {
                uint64_t d = zz(col.v[i] - col.v[i - 1]);
                if (d >= esc)
                    at = putVarint(out, cap, at, d);
            }
        }
#endif
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

// ---------------------------------------------------------------- decode

uint8_t decode(const pb_msgdesc_t *desc, const uint8_t *in, size_t len, void *const *msgs, uint8_t maxN, uint32_t *times,
               const Options &opt, uint32_t *contextOut)
{
    if (!desc || !in || !msgs || !times || len < 4)
        return 0;
    if ((in[0] >> 4) != VERSION)
        return 0;
    uint8_t tier = in[0] & 0x0F;
    uint8_t n = in[1];
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

    times[0] = (uint32_t)t0;
    int64_t prevGap = 0;
    for (uint8_t i = 1; i < n; i++) {
        uint64_t z;
        at = getVarint(in, len, at, &z);
        if (at == SIZE_MAX)
            return 0;
        int64_t g = unzz(z);
        if (tier >= TIER_BITMAP && i > 1)
            g += prevGap;
        times[i] = (uint32_t)((int64_t)times[i - 1] + g);
        prevGap = g;
    }

    for (uint8_t c = 0; c < nCols; c++) {
        if (at >= len)
            return 0;
        uint8_t codeByte = in[at++];
        uint8_t code = codeByte & 0x0F, resLog2 = codeByte >> 4;
        // The family table is shared by both ends, so the channel count needs no wire byte.
        const FieldFamily *fam = familyLedBy(opt, tags[c]);
        const uint8_t chans = fam ? fam->count : 1;
        const uint16_t rows = (uint16_t)n * chans;
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
        }
#if BISCUIT_MAX_TIER >= 3
        else if (code == COL_GCD) {
            uint64_t g;
            at = getVarint(in, len, at, &g);
            if (at == SIZE_MAX || g == 0)
                return 0;
            at = getVarint(in, len, at, &z);
            if (at == SIZE_MAX)
                return 0;
            int64_t acc = unzz(z);
            v[0] = acc * (int64_t)g;
            for (uint8_t i = 1; i < rows; i++) {
                at = getVarint(in, len, at, &z);
                if (at == SIZE_MAX)
                    return 0;
                acc += unzz(z);
                v[i] = acc * (int64_t)g;
            }
        }
#endif
#if BISCUIT_MAX_TIER >= 4
        else if (code == COL_PACKED) {
            at = getVarint(in, len, at, &z);
            if (at == SIZE_MAX || at >= len)
                return 0;
            v[0] = unzz(z);
            uint8_t width = in[at++];
            if (width == 0 || width > 32)
                return 0;
            uint64_t esc = ((uint64_t)1 << width) - 1;
            size_t bits = (size_t)(rows - 1) * width;
            size_t nbytes = (bits + 7) / 8;
            if (at + nbytes > len)
                return 0;
            BitReader br{in + at, nbytes, 0};
            uint64_t d[BISCUIT_MAX_BATCH * 4];
            uint8_t nEsc = 0;
            for (uint8_t i = 1; i < rows; i++) {
                if (!br.get(width, &d[i]))
                    return 0;
                if (d[i] == esc)
                    nEsc++;
            }
            at += nbytes;
            for (uint8_t i = 1; i < rows; i++) {
                if (d[i] == esc) {
                    at = getVarint(in, len, at, &z);
                    if (at == SIZE_MAX)
                        return 0;
                    d[i] = z;
                }
            }
            (void)nEsc;
            for (uint8_t i = 1; i < rows; i++)
                v[i] = v[i - 1] + unzz(d[i]);
        }
#endif
        else {
            return 0;
        }

        // Scatter back: channel ch owns rows [ch*n, ch*n + n). With one channel this is the
        // plain case; the family layout must match the encoder's channel-major order exactly.
        for (uint8_t ch = 0; ch < chans; ch++) {
            const uint8_t tg = fam ? fam->tags[ch] : tags[c];
            const FieldHint *h = findHint(opt, tg);
            for (uint8_t i = 0; i < n; i++) {
                pb_field_iter_t jt;
                if (!pb_field_iter_begin(&jt, desc, msgs[i]) || !pb_field_iter_find(&jt, tg))
                    return 0;
                const int64_t val = v[(uint16_t)ch * n + i];
                writeScalar(jt, h, opt, resLog2 ? (val << resLog2) : val);
            }
        }
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
    return (in && len >= 2 && (in[0] >> 4) == VERSION) ? in[1] : 0;
}
uint8_t peekTier(const uint8_t *in, size_t len)
{
    return (in && len >= 1 && (in[0] >> 4) == VERSION) ? (uint8_t)(in[0] & 0x0F) : 0;
}

} // namespace biscuit

#endif // MESHTASTIC_BISCUIT_ENABLED
