#pragma once

// Full-message comparison for Biscuit round-trip tests.
//
// A hand-written list of field assertions proves that everything expected came back. It does not
// prove that nothing unexpected did: a decoder that writes a value into a field the fixture never
// populated is invisible to it, at every tier. This walks the message descriptor instead, so
// every field of the type is checked whether the fixture knew about it or not.

#include "mesh/biscuit/Biscuit.h"
#include "pb.h"
#include "pb_common.h"
#include <math.h>
#include <stdio.h>
#include <string.h>
#include <unity.h>

namespace biscuitcmp
{

/// The same rule the codec uses to decide what it can carry. Anything else it leaves untouched,
/// so the decoder must not have written it.
inline bool cmpEncodable(pb_type_t t)
{
    if (PB_ATYPE(t) != PB_ATYPE_STATIC || PB_HTYPE(t) == PB_HTYPE_REPEATED)
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

inline bool cmpPresent(const pb_field_iter_t &it)
{
    if (PB_HTYPE(it.type) == PB_HTYPE_OPTIONAL && it.pSize)
        return *(const bool *)it.pSize;
    return true;
}

inline int64_t cmpInt(const pb_field_iter_t &it)
{
    switch (PB_LTYPE(it.type)) {
    case PB_LTYPE_BOOL:
        return *(const bool *)it.pData ? 1 : 0;
    case PB_LTYPE_VARINT:
    case PB_LTYPE_SVARINT:
        return it.data_size == 8 ? *(const int64_t *)it.pData : *(const int32_t *)it.pData;
    case PB_LTYPE_UVARINT:
        return it.data_size == 8 ? (int64_t) * (const uint64_t *)it.pData : (int64_t) * (const uint32_t *)it.pData;
    default:
        return 0;
    }
}

/// Rounding to the scale costs half a unit of it; the encoder then does that multiply in float32,
/// so above value*scale = 2^24 the product loses its low bits and the error becomes proportional
/// to the value instead. `extra` widens both for a batch that declared a resolution shift.
inline float cmpTol(float want, float extra)
{
    const float rel = fabsf(want) * 1.2e-7f;
    float t = rel > 0.0002f ? rel : 0.0002f;
    return extra > t ? extra : t;
}

/**
 * Assert two messages of one type are the same reading.
 *
 * Every encodable scalar must match in presence and in value; every field the codec cannot carry
 * must be absent from `got`, because the decoder had no business writing one. `extra` widens the
 * float tolerance for a batch encoded with a resolution hint.
 */
inline unsigned assertSameReading(const pb_msgdesc_t *desc, const void *want, const void *got, const char *ctx,
                                  bool fixed32IsFloat = true, float extra = 0.0f)
{
    pb_field_iter_t a, b;
    char msg[192];
    unsigned compared = 0;
    if (!pb_field_iter_begin_const(&a, desc, want) || !pb_field_iter_begin_const(&b, desc, got)) {
        snprintf(msg, sizeof(msg), "%s: cannot walk the descriptor", ctx);
        TEST_FAIL_MESSAGE(msg);
        return 0;
    }
    do {
        const uint32_t tag = a.tag;
        const bool pa = cmpPresent(a), pb_ = cmpPresent(b);
        if (!cmpEncodable(a.type) || tag > 64) {
            // Nothing the codec carries, so the decoder must have left it alone. A field only
            // `want` has is the fixture's business; one only `got` has is a defect.
            if (pb_ && !pa) {
                snprintf(msg, sizeof(msg), "%s: tag %u invented by the decoder", ctx, (unsigned)tag);
                TEST_FAIL_MESSAGE(msg);
            }
            continue;
        }
        if (pa != pb_) {
            snprintf(msg, sizeof(msg), "%s: tag %u presence %d became %d", ctx, (unsigned)tag, (int)pa, (int)pb_);
            TEST_FAIL_MESSAGE(msg);
            continue;
        }
        if (!pa)
            continue;
        compared++;
        if (PB_LTYPE(a.type) == PB_LTYPE_FIXED32 && fixed32IsFloat) {
            float fa, fb;
            memcpy(&fa, a.pData, sizeof(fa));
            memcpy(&fb, b.pData, sizeof(fb));
            snprintf(msg, sizeof(msg), "%s: tag %u %.6f became %.6f", ctx, (unsigned)tag, (double)fa, (double)fb);
            TEST_ASSERT_FLOAT_WITHIN_MESSAGE(cmpTol(fa, extra), fa, fb, msg);
        } else if (PB_LTYPE(a.type) == PB_LTYPE_FIXED32) {
            uint32_t ua, ub;
            memcpy(&ua, a.pData, sizeof(ua));
            memcpy(&ub, b.pData, sizeof(ub));
            snprintf(msg, sizeof(msg), "%s: tag %u %u became %u", ctx, (unsigned)tag, ua, ub);
            TEST_ASSERT_EQUAL_UINT32_MESSAGE(ua, ub, msg);
        } else {
            const int64_t ia = cmpInt(a), ib = cmpInt(b);
            snprintf(msg, sizeof(msg), "%s: tag %u %lld became %lld", ctx, (unsigned)tag, (long long)ia, (long long)ib);
            TEST_ASSERT_EQUAL_INT64_MESSAGE(ia, ib, msg);
        }
    } while (pb_field_iter_next(&a) && pb_field_iter_next(&b));
    return compared;
}

/// A whole batch: every reading, and its timestamp.
template <typename T>
inline void assertSameBatch(const pb_msgdesc_t *desc, const T *want, const T *got, const uint32_t *wantTs, const uint32_t *gotTs,
                            uint8_t n, const char *ctx, bool fixed32IsFloat = true, float extra = 0.0f)
{
    char msg[224];
    unsigned compared = 0;
    for (uint8_t i = 0; i < n; i++) {
        snprintf(msg, sizeof(msg), "%s reading %u", ctx, i);
        if (wantTs && gotTs)
            TEST_ASSERT_EQUAL_UINT32_MESSAGE(wantTs[i], gotTs[i], msg);
        compared += assertSameReading(desc, &want[i], &got[i], msg, fixed32IsFloat, extra);
    }
    // A comparison that compared nothing is worse than no comparison: it reads as coverage.
    if (n) {
        snprintf(msg, sizeof(msg), "%s: the descriptor walk compared no fields at all", ctx);
        TEST_ASSERT_GREATER_THAN_UINT32_MESSAGE(0, compared, msg);
    }
}

} // namespace biscuitcmp
