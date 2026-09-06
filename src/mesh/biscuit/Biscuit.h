#pragma once

#include "pb.h"
#include <stddef.h>
#include <stdint.h>

/**
 * Biscuit: columnar re-encoding of a batch of identical protobuf messages.
 *
 * Takes N instances of any nanopb message plus their capture times, transposes them into
 * columns, and encodes each column with the cheapest technique available at the compiled
 * tier. Decoding reconstructs the original messages exactly, or to a declared resolution
 * when the caller asks for one.
 *
 * Tiers are cumulative and cost real flash/RAM, so each is a compile guard:
 *   1 columnar + zigzag delta          protobuf-native, no bit manipulation
 *   2 + presence bitmap, delta-of-delta time
 *   3 + per-column resolution shift    lossy only where the caller sets a shift
 *   4 + fixed-width bit packing        needs the bit codec
 */

// Master gate. 0 compiles the codec out entirely - no encoder, no decoder, no module - so
// the flash and RAM it costs can be measured against a build without it.
#ifndef MESHTASTIC_BISCUIT_ENABLED
#ifdef USERPREFS_BISCUIT_ENABLED
#define MESHTASTIC_BISCUIT_ENABLED USERPREFS_BISCUIT_ENABLED
#else
#define MESHTASTIC_BISCUIT_ENABLED 0
#endif
#endif

#ifndef BISCUIT_MAX_TIER
#define BISCUIT_MAX_TIER 4
#endif

#ifndef BISCUIT_MAX_BATCH
#define BISCUIT_MAX_BATCH 24
#endif

#ifndef BISCUIT_MAX_COLUMNS
#define BISCUIT_MAX_COLUMNS 16
#endif

namespace biscuit
{

/// Wire format version. Bumped only for an incompatible layout change.
static constexpr uint8_t VERSION = 1;

enum Tier : uint8_t {
    TIER_COLUMNAR = 1,
    TIER_BITMAP = 2,
    TIER_RESOLUTION = 3,
    TIER_PACKED = 4,
};

/// Per-tag encoding hint. Tags outside the supplied array are treated as exact integers.
struct FieldHint {
    uint8_t tag;
    bool isFloat;    ///< PB_LTYPE_FIXED32 holding a float rather than a fixed32
    uint8_t resLog2; ///< right-shift applied before encoding; 0 = exact. Tier 3+.
    uint16_t scale;  ///< float -> integer multiplier, e.g. 1000 for millivolts. 0 = 10000.
};

struct Options {
    uint8_t maxTier = BISCUIT_MAX_TIER; ///< cap the tier below what is compiled in
    const FieldHint *hints = nullptr;
    uint8_t hintCount = 0;
    /// Refuse to emit when the result would be no smaller than the protobuf it replaces.
    /// A batch of one, or of very wide messages, can encode larger; the caller sends plain.
    bool neverInflate = true;
    /// Treat every PB_LTYPE_FIXED32 field as a float. True for all telemetry messages, where
    /// no fixed32 carries anything else - avoids a per-tag table for every metrics type.
    bool fixed32IsFloat = false;
    /// Multiplier used to make a float an integer when no per-tag hint overrides it.
    /// 10000 keeps four decimals, which is finer than any telemetry sensor resolves.
    uint16_t floatScale = 10000;
    /// Opaque word carried verbatim in the payload and handed back by decode(). The codec
    /// gives it no meaning; callers use it to record what the batch was, so a receiver can
    /// rebuild the original packet. BaseTelemetryModule packs the source portnum and the
    /// TelemetryRecord variant tag into it.
    uint32_t context = 0;
};

struct Result {
    size_t size = 0;       ///< bytes written; 0 means it failed, did not fit, or would inflate
    size_t baseline = 0;   ///< protobuf bytes this batch would have cost unencoded
    uint8_t tier = 0;      ///< tier actually used
    bool lossless = true;  ///< false if any column carried a resolution shift
    uint32_t worstErr = 0; ///< largest absolute error introduced, in scaled units
    uint32_t context = 0;  ///< the caller's context word, echoed back by decode()
};

/**
 * Encode a batch. All messages must share `desc`. `times` are absolute capture seconds.
 * Returns size 0 if the batch does not fit in `cap` or contains no encodable columns.
 */
Result encode(const pb_msgdesc_t *desc, const void *const *msgs, uint8_t n, const uint32_t *times, uint8_t *out, size_t cap,
              const Options &opt = Options());

/**
 * Decode a batch previously produced by encode(). `msgs` must point at n zero-initialised
 * messages of the same type. Returns the number of messages written, 0 on failure.
 */
uint8_t decode(const pb_msgdesc_t *desc, const uint8_t *in, size_t len, void *const *msgs, uint8_t maxN, uint32_t *times,
               const Options &opt = Options(), uint32_t *contextOut = nullptr);

/// The context word without decoding the body, so a receiver can route on it first.
/// Returns false if the header is not ours.
bool peekContext(const uint8_t *in, size_t len, uint32_t *contextOut);

/**
 * Readings to retire after publishing `take` of them, leaving up to `overlap` to ride again in
 * the next packet so one lost packet does not take its readings with it.
 *
 * Always retires at least one. A caller that sets overlap >= take would otherwise resend the
 * same batch forever, and `take` is not known until encode time, so the compile-time check in
 * BaseTelemetryModule.h cannot catch it.
 */
constexpr size_t retireCount(size_t take, size_t overlap)
{
    return (overlap == 0 || take <= 1) ? take : take - (overlap >= take ? take - 1 : overlap);
}

/// Readings this batch holds, without decoding it. 0 if the header is not ours.
uint8_t peekCount(const uint8_t *in, size_t len);

/// Tier used, without decoding. 0 if the header is not ours.
uint8_t peekTier(const uint8_t *in, size_t len);

} // namespace biscuit
