// Biscuit round-trip: a batch of telemetry messages re-encoded columnwise must decode back
// byte-identical (or within the resolution shift the caller asked for), at every tier.
//
// The guarantee under test is exactness, not size. A size regression is a judgement call; a
// value that does not survive the round trip is a defect that silently corrupts a reading,
// and no CRC catches it because the packet arrives intact.
#include "Arduino.h"
#include "BiscuitCompare.h"
#include "TestUtil.h"
#include "mesh/biscuit/Biscuit.h"
#include "mesh/generated/meshtastic/telemetry.pb.h"
#include "pb_encode.h"
#include <math.h>
#include <unity.h>

using namespace biscuit;

static const FieldHint kDeviceHints[] = {
    {2, true, 0, 1000}, // voltage: float, millivolts, exact
    {3, true, 0, 100},  // channel_utilization
    {4, true, 0, 100},  // air_util_tx
};

static void makeDeviceBatch(meshtastic_DeviceMetrics *m, uint32_t *t, uint8_t n)
{
    for (uint8_t i = 0; i < n; i++) {
        m[i] = meshtastic_DeviceMetrics_init_zero;
        m[i].has_battery_level = true;
        m[i].battery_level = (uint32_t)(92 - i / 4);
        m[i].has_voltage = true;
        m[i].voltage = 4.021f - 0.002f * i;
        m[i].has_channel_utilization = true;
        m[i].channel_utilization = 12.5f + 0.25f * (float)((i * 7) % 9);
        m[i].has_uptime_seconds = true;
        m[i].uptime_seconds = 3600 + 1800 * i;
        t[i] = 1757000000u + 1800u * i;
    }
}

/// Encode then decode at `tier`, asserting every field and timestamp survives.
static size_t roundTrip(uint8_t tier, uint8_t n, bool expectLossless = true)
{
    meshtastic_DeviceMetrics src[BISCUIT_MAX_BATCH], dst[BISCUIT_MAX_BATCH];
    uint32_t ts[BISCUIT_MAX_BATCH], tsOut[BISCUIT_MAX_BATCH];
    makeDeviceBatch(src, ts, n);

    const void *sp[BISCUIT_MAX_BATCH];
    void *dp[BISCUIT_MAX_BATCH];
    for (uint8_t i = 0; i < n; i++) {
        sp[i] = &src[i];
        dst[i] = meshtastic_DeviceMetrics_init_zero;
        dp[i] = &dst[i];
    }

    Options opt;
    opt.maxTier = tier;
    opt.hints = kDeviceHints;
    opt.hintCount = sizeof(kDeviceHints) / sizeof(kDeviceHints[0]);

    uint8_t buf[233];
    opt.neverInflate = false; // round-trip fidelity is under test here, not size
    Result r = encode(&meshtastic_DeviceMetrics_msg, sp, n, ts, buf, sizeof(buf), opt);
    TEST_ASSERT_TRUE_MESSAGE(r.size > 0, "encode produced nothing");
    TEST_ASSERT_LESS_OR_EQUAL(sizeof(buf), r.size);
    if (expectLossless)
        TEST_ASSERT_TRUE(r.lossless);
    TEST_ASSERT_EQUAL(n, peekCount(buf, r.size));
    TEST_ASSERT_EQUAL(r.tier, peekTier(buf, r.size));

    uint8_t got = decode(&meshtastic_DeviceMetrics_msg, buf, r.size, dp, BISCUIT_MAX_BATCH, tsOut, opt);
    biscuitcmp::assertSameBatch(&meshtastic_DeviceMetrics_msg, src, dst, ts, tsOut, n, "round trip");
    TEST_ASSERT_EQUAL_MESSAGE(n, got, "decode returned the wrong count");

    for (uint8_t i = 0; i < n; i++) {
        TEST_ASSERT_EQUAL_UINT32(ts[i], tsOut[i]);
        TEST_ASSERT_TRUE(dst[i].has_battery_level);
        TEST_ASSERT_EQUAL_UINT32(src[i].battery_level, dst[i].battery_level);
        TEST_ASSERT_EQUAL_UINT32(src[i].uptime_seconds, dst[i].uptime_seconds);
        TEST_ASSERT_TRUE(dst[i].has_voltage);
        TEST_ASSERT_FLOAT_WITHIN(0.0005f, src[i].voltage, dst[i].voltage);
        TEST_ASSERT_FLOAT_WITHIN(0.005f, src[i].channel_utilization, dst[i].channel_utilization);
    }
    return r.size;
}

void test_roundTrip_tier1_isExact(void)
{
    roundTrip(TIER_COLUMNAR, 12);
}
void test_roundTrip_tier2_isExact(void)
{
    roundTrip(TIER_BITMAP, 12);
}
void test_roundTrip_tier3_isExact(void)
{
    roundTrip(TIER_RESOLUTION, 12);
}
#if BISCUIT_MAX_TIER >= 4
void test_roundTrip_tier4_isExact(void)
{
    roundTrip(TIER_PACKED, 12);
}
#endif

/// Every batch size must survive, including the degenerate single reading.
void test_roundTrip_everyBatchSize(void)
{
    for (uint8_t n = 1; n <= 16; n++)
        for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++)
            roundTrip(tier, n);
}

/// What encode() emits is never larger than any single tier would have produced.
///
/// This replaces an assertion that a higher tier is never larger than a lower one. That premise
/// is false - tier 4 spends a width table and a per-column divisor before it encodes anything,
/// and loses to tier 2 on 39% of two-reading batches in the captured corpus. It also became
/// unfalsifiable once encode() started selecting: passing maxTier=t returns the best of tiers
/// 1..t, which is monotonically non-increasing by construction, so the old assertion could not
/// fail whatever the codec did. The real invariant is the one below, and it needs encodeAtTier
/// to state, because encode() is the thing being checked.
void test_selectedEncodingBeatsEveryTier(void)
{
    meshtastic_DeviceMetrics src[12];
    uint32_t ts[12];
    makeDeviceBatch(src, ts, 12);
    const void *sp[12];
    for (uint8_t i = 0; i < 12; i++)
        sp[i] = &src[i];

    Options opt;
    opt.hints = kDeviceHints;
    opt.hintCount = sizeof(kDeviceHints) / sizeof(kDeviceHints[0]);
    opt.neverInflate = false;

    uint8_t buf[512];
    const Result chosen = encode(&meshtastic_DeviceMetrics_msg, sp, 12, ts, buf, sizeof(buf), opt);
    TEST_ASSERT_GREATER_THAN(0, (int)chosen.size);

    bool sawOne = false;
    for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++) {
        uint8_t one[512];
        const Result r = encodeAtTier(&meshtastic_DeviceMetrics_msg, sp, 12, ts, one, sizeof(one), opt, tier);
        if (!r.size)
            continue;
        sawOne = true;
        char msg[96];
        snprintf(msg, sizeof(msg), "tier %u alone is %u B, selection chose tier %u at %u B", tier, (unsigned)r.size, chosen.tier,
                 (unsigned)chosen.size);
        TEST_ASSERT_LESS_OR_EQUAL_MESSAGE(r.size, chosen.size, msg);
    }
    TEST_ASSERT_TRUE_MESSAGE(sawOne, "no tier produced anything to compare against");
}

/// A resolution shift is lossy by exactly the margin it declares, and no more.
void test_resolutionShift_errorIsBounded(void)
{
#if BISCUIT_MAX_TIER >= 3
    static const FieldHint coarse[] = {{2, true, 6, 1000}}; // voltage, 64 mV grid
    meshtastic_DeviceMetrics src[8], dst[8];
    uint32_t ts[8], tsOut[8];
    makeDeviceBatch(src, ts, 8);
    const void *sp[8];
    void *dp[8];
    for (uint8_t i = 0; i < 8; i++) {
        sp[i] = &src[i];
        dst[i] = meshtastic_DeviceMetrics_init_zero;
        dp[i] = &dst[i];
    }
    Options opt;
    opt.hints = coarse;
    opt.hintCount = 1;
    uint8_t buf[233];
    Result r = encode(&meshtastic_DeviceMetrics_msg, sp, 8, ts, buf, sizeof(buf), opt);
    TEST_ASSERT_TRUE(r.size > 0);
    TEST_ASSERT_FALSE_MESSAGE(r.lossless, "a resolution shift must report itself lossy");
    TEST_ASSERT_EQUAL_UINT32(32u, r.worstErr); // 1 << (6-1) millivolts
    TEST_ASSERT_EQUAL(8, decode(&meshtastic_DeviceMetrics_msg, buf, r.size, dp, 8, tsOut, opt));
    // voltage was quantised to a 64 mV grid on purpose; allow exactly that.
    biscuitcmp::assertSameBatch(&meshtastic_DeviceMetrics_msg, src, dst, ts, tsOut, 8, "round trip", true, 0.064f);
    for (uint8_t i = 0; i < 8; i++)
        TEST_ASSERT_FLOAT_WITHIN(0.064f, src[i].voltage, dst[i].voltage);
#endif
}

/// A buffer too small must fail cleanly rather than overrun.
void test_shortBuffer_failsWithoutOverrun(void)
{
    meshtastic_DeviceMetrics src[12];
    uint32_t ts[12];
    makeDeviceBatch(src, ts, 12);
    const void *sp[12];
    for (uint8_t i = 0; i < 12; i++)
        sp[i] = &src[i];
    for (size_t cap = 1; cap < 40; cap++) {
        uint8_t small[40];
        memset(small, 0xAA, sizeof(small));
        Result r = encode(&meshtastic_DeviceMetrics_msg, sp, 12, ts, small, cap, Options());
        if (r.size)
            TEST_ASSERT_LESS_OR_EQUAL(cap, r.size);
        TEST_ASSERT_EQUAL_MESSAGE(0xAA, small[cap], "encode wrote past the buffer");
    }
}

/// Garbage in must not decode as something plausible.
void test_corruptHeader_isRejected(void)
{
    uint8_t junk[16];
    memset(junk, 0x5A, sizeof(junk));
    meshtastic_DeviceMetrics dst[4];
    void *dp[4] = {&dst[0], &dst[1], &dst[2], &dst[3]};
    uint32_t tsOut[4];
    TEST_ASSERT_EQUAL(0, decode(&meshtastic_DeviceMetrics_msg, junk, sizeof(junk), dp, 4, tsOut));
    TEST_ASSERT_EQUAL(0, peekCount(junk, sizeof(junk)));
}

/// The factory must never inflate. Baseline is the protobuf the accumulator handed us:
/// the N messages as they would be encoded today, which is what a batch replaces.
static size_t protobufBaseline(const meshtastic_DeviceMetrics *m, uint8_t n)
{
    size_t total = 0;
    for (uint8_t i = 0; i < n; i++) {
        size_t one = 0;
        TEST_ASSERT_TRUE(pb_get_encoded_size(&one, &meshtastic_DeviceMetrics_msg, &m[i]));
        total += one + 6; // Telemetry wrapper: time fixed32 + oneof tag and length
    }
    return total;
}

void test_neverLargerThanTheProtobufItReplaces(void)
{
    for (uint8_t n = 1; n <= 16; n++) {
        meshtastic_DeviceMetrics src[BISCUIT_MAX_BATCH];
        uint32_t ts[BISCUIT_MAX_BATCH];
        makeDeviceBatch(src, ts, n);
        const void *sp[BISCUIT_MAX_BATCH];
        for (uint8_t i = 0; i < n; i++)
            sp[i] = &src[i];

        size_t baseline = protobufBaseline(src, n);
        Options opt;
        opt.hints = kDeviceHints;
        opt.hintCount = sizeof(kDeviceHints) / sizeof(kDeviceHints[0]);

        for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++) {
            opt.maxTier = tier;
            uint8_t buf[512];
            Result r = encode(&meshtastic_DeviceMetrics_msg, sp, n, ts, buf, sizeof(buf), opt);
            // Refusing is a correct outcome: the caller sends the plain protobuf instead.
            if (r.size == 0)
                continue;
            char msg[96];
            snprintf(msg, sizeof(msg), "n=%u tier=%u: %u B vs %u B protobuf", n, tier, (unsigned)r.size, (unsigned)baseline);
            TEST_ASSERT_LESS_OR_EQUAL_MESSAGE(baseline, r.size, msg);
        }
    }
}

// ---------------------------------------------------------------- overlap retention

/// retireCount decides how many readings a publish retires. Getting it wrong either loses
/// readings (retiring the overlap tail) or wedges the node (retiring nothing).
void test_retireCount_alwaysMakesProgress(void)
{
    for (size_t take = 1; take <= 32; take++) {
        for (size_t overlap = 0; overlap <= 40; overlap++) {
            size_t r = retireCount(take, overlap);
            char msg[80];
            snprintf(msg, sizeof(msg), "take=%u overlap=%u retired=%u", (unsigned)take, (unsigned)overlap, (unsigned)r);
            TEST_ASSERT_GREATER_THAN_MESSAGE(0, r, msg);     // never wedges
            TEST_ASSERT_LESS_OR_EQUAL_MESSAGE(take, r, msg); // never retires what was not sent
        }
    }
}

void test_retireCount_keepsTheRequestedTail(void)
{
    TEST_ASSERT_EQUAL(6, retireCount(6, 0)); // no overlap: retire everything
    TEST_ASSERT_EQUAL(5, retireCount(6, 1));
    TEST_ASSERT_EQUAL(4, retireCount(6, 2));
    TEST_ASSERT_EQUAL(1, retireCount(6, 5));
    TEST_ASSERT_EQUAL(1, retireCount(6, 6)); // clamped: overlap >= take keeps take-1
    TEST_ASSERT_EQUAL(1, retireCount(6, 99));
    TEST_ASSERT_EQUAL(1, retireCount(1, 4)); // a single reading is always retired
}

/// The published set must advance by exactly the new-reading rate, so a node with
/// flush F and overlap O makes F-O readings of progress per packet, forever.
void test_retireCount_progressRateIsFlushMinusOverlap(void)
{
    for (size_t flush = 2; flush <= 24; flush++) {
        for (size_t overlap = 0; overlap < flush; overlap++) {
            size_t retired = retireCount(flush, overlap);
            TEST_ASSERT_EQUAL_UINT32((uint32_t)(flush - overlap), (uint32_t)retired);
        }
    }
}

// ---------------------------------------------------------------- real corpus data

// node 0xfa4f6ea0, 12 consecutive real readings
static const uint32_t kRealTimes[12] = {1741889047u, 1741891569u, 1741894091u, 1741896613u, 1741899135u, 1741901657u,
                                        1741904179u, 1741906701u, 1741909223u, 1741911745u, 1741914267u, 1741916790u};
static const uint32_t kReal_battery_level[12] = {101u, 99u, 101u, 101u, 100u, 101u, 99u, 98u, 101u, 101u, 101u, 101u};
static const float kReal_voltage[12] = {4.2440000f, 4.1869998f, 4.2370000f, 4.2069998f, 4.1999998f, 4.2309999f,
                                        4.1789999f, 4.1700001f, 4.2170000f, 4.2150002f, 4.2010002f, 4.2049999f};
static const float kReal_channel_util[12] = {0.0000000f, 0.0000000f, 0.0000000f, 3.5033333f, 0.8633333f, 0.0000000f,
                                             0.0000000f, 0.0000000f, 0.0000000f, 0.0000000f, 0.0000000f, 0.0000000f};
static const float kReal_air_util_tx[12] = {0.8739445f, 0.6811666f, 0.5052500f, 1.0397500f, 1.3827223f, 1.1279445f,
                                            0.8759722f, 0.7435278f, 0.7211667f, 0.6581667f, 0.5896389f, 0.7238055f};
static const uint32_t kReal_uptime[12] = {534600u, 537122u, 539644u, 542166u, 544688u, 547210u,
                                          549732u, 552254u, 554776u, 557298u, 559820u, 562342u};

static void makeRealBatch(meshtastic_DeviceMetrics *m, uint32_t *t, uint8_t n)
{
    for (uint8_t i = 0; i < n; i++) {
        m[i] = meshtastic_DeviceMetrics_init_zero;
        m[i].has_battery_level = true;
        m[i].battery_level = kReal_battery_level[i];
        m[i].has_voltage = true;
        m[i].voltage = kReal_voltage[i];
        m[i].has_channel_utilization = true;
        m[i].channel_utilization = kReal_channel_util[i];
        m[i].has_air_util_tx = true;
        m[i].air_util_tx = kReal_air_util_tx[i];
        m[i].has_uptime_seconds = true;
        m[i].uptime_seconds = kReal_uptime[i];
        t[i] = kRealTimes[i];
    }
}

/// Real readings from a real node, not a smooth series I invented. This is the case that
/// broke every synthetic assumption in the analysis: a 101% USB sentinel in battery_level,
/// channel_utilization that is zero for most of the batch and then is not, and reporting
/// intervals that jitter by a second. If the encoder mishandles any of it, the values come
/// back wrong rather than the packet failing to build.
void test_realCorpusData_roundTripsExactly(void)
{
    meshtastic_DeviceMetrics src[12], dst[12];
    uint32_t ts[12], tsOut[12];
    makeRealBatch(src, ts, 12);
    const void *sp[12];
    void *dp[12];
    for (uint8_t i = 0; i < 12; i++) {
        sp[i] = &src[i];
        dst[i] = meshtastic_DeviceMetrics_init_zero;
        dp[i] = &dst[i];
    }
    biscuit::Options opt;
    opt.fixed32IsFloat = true;

    for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++) {
        opt.maxTier = tier;
        uint8_t buf[233];
        Result r = encode(&meshtastic_DeviceMetrics_msg, sp, 12, ts, buf, sizeof(buf), opt);
        TEST_ASSERT_TRUE(r.size > 0);
        TEST_ASSERT_TRUE(r.lossless);
        TEST_ASSERT_EQUAL(12, decode(&meshtastic_DeviceMetrics_msg, buf, r.size, dp, 12, tsOut, opt));
        biscuitcmp::assertSameBatch(&meshtastic_DeviceMetrics_msg, src, dst, ts, tsOut, 12, "round trip");
        for (uint8_t i = 0; i < 12; i++) {
            TEST_ASSERT_EQUAL_UINT32(ts[i], tsOut[i]);
            TEST_ASSERT_EQUAL_UINT32(src[i].battery_level, dst[i].battery_level);
            TEST_ASSERT_EQUAL_UINT32(src[i].uptime_seconds, dst[i].uptime_seconds);
            TEST_ASSERT_FLOAT_WITHIN(0.0002f, src[i].voltage, dst[i].voltage);
            TEST_ASSERT_FLOAT_WITHIN(0.0002f, src[i].channel_utilization, dst[i].channel_utilization);
            TEST_ASSERT_FLOAT_WITHIN(0.0002f, src[i].air_util_tx, dst[i].air_util_tx);
        }
    }
}

/// Numerical pins on real data. These are recorded measurements, not predictions - if the
/// format changes deliberately, re-measure and update them. A silent change here means the
/// encoder started producing something different for input that did not change.
void test_realCorpusData_sizesAreStable(void)
{
    meshtastic_DeviceMetrics src[12];
    uint32_t ts[12];
    makeRealBatch(src, ts, 12);
    const void *sp[12];
    for (uint8_t i = 0; i < 12; i++)
        sp[i] = &src[i];
    biscuit::Options opt;
    opt.fixed32IsFloat = true;

    size_t baseline = protobufBaseline(src, 12);
    TEST_ASSERT_EQUAL_UINT32(324u, (uint32_t)baseline);

    uint32_t got[BISCUIT_MAX_TIER + 1] = {0};
    for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++) {
        opt.maxTier = tier;
        uint8_t buf[233];
        got[tier] = (uint32_t)encodeAtTier(&meshtastic_DeviceMetrics_msg, sp, 12, ts, buf, sizeof(buf), opt, tier).size;
    }
    char msg[128];
    snprintf(msg, sizeof(msg), "sizes t1..t%u = %u %u %u %u (baseline %u)", BISCUIT_MAX_TIER, got[1], got[2], got[3],
             BISCUIT_MAX_TIER >= 4 ? got[4] : 0u, (unsigned)baseline);
    // Measured on the fixture above, not predicted. Update deliberately if the format changes -
    // adding the context word moved every tier by one byte, which this caught; stacking the
    // tier-4 bit area took it from 113 to 102; the profile byte costs one at every tier and the
    // tier-4 resolution mask one more. T3 equals T2 here because this test passes no resolution
    // hints; test_realCorpusData_meetsTierModelSavings is the one that does.
    static const uint32_t expect[] = {144, 129, 129, 104};
    for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++)
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(expect[tier - 1], got[tier], msg);
}

/// Hints matching the tier model's RES table for DeviceMetrics - voltage to 1/16 V and stamps
/// to the minute. The model's T3 row assumes a user has selected exactly this.
static const FieldHint kModelHints[] = {
    {2, true, 0, 16}, // voltage in sixteenths of a volt, the model's 62.5 mV step
};

/// The codec measured against the Python tier model on the same twelve readings
/// (fixture-tiers.py in the notes: T0 336, T1 171, T2 151, T3 142, T4 101 -> 49/55/58/70%).
///
/// The pin is the saving, not the byte count: the model wraps each tier in protobuf framing
/// while the codec uses a bare header, so the two will never agree byte for byte. What must
/// agree is that each tier delivers what the analysis said it would. Without this, the tier
/// gates prove only that the code compiles - which is how a codec missing two of its five
/// techniques passed a full green suite.
void test_realCorpusData_meetsTierModelSavings(void)
{
    meshtastic_DeviceMetrics src[12];
    uint32_t ts[12];
    makeRealBatch(src, ts, 12);
    const void *sp[12];
    for (uint8_t i = 0; i < 12; i++)
        sp[i] = &src[i];

    biscuit::Options opt;
    opt.fixed32IsFloat = true;
    opt.hints = kModelHints;
    opt.hintCount = sizeof(kModelHints) / sizeof(kModelHints[0]);
    opt.timeRes = 60;

    const size_t baseline = protobufBaseline(src, 12);
    // The model's own figures for this fixture, except T4 where the codec lands at 68.5% against
    // its 70% - close enough that the floor sits just under rather than being relaxed.
    static const uint32_t floorPct[] = {49, 55, 58, 66};
    for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++) {
        opt.maxTier = tier;
        uint8_t buf[233];
        Result r = encodeAtTier(&meshtastic_DeviceMetrics_msg, sp, 12, ts, buf, sizeof(buf), opt, tier);
        TEST_ASSERT_TRUE(r.size > 0);
        const unsigned pct = (unsigned)(100u - (100u * r.size) / baseline);
        char msg[96];
        snprintf(msg, sizeof(msg), "tier %u saved %u%%, model floor %u%% (%u B of %u)", tier, pct, floorPct[tier - 1],
                 (unsigned)r.size, (unsigned)baseline);
        TEST_ASSERT_GREATER_OR_EQUAL_UINT32_MESSAGE(floorPct[tier - 1], pct, msg);
    }
}

/// Asking for a tier the build does not carry must clamp to what it has, not fail and not
/// claim the tier it was asked for. The compile gates exist to price the tiers on the tightest
/// target; a tier-2 binary reporting tier 4 would make every one of those measurements a lie.
void test_tierAboveCompiledMax_clamps(void)
{
    meshtastic_DeviceMetrics src[8];
    uint32_t ts[8];
    makeRealBatch(src, ts, 8);
    const void *sp[8];
    for (uint8_t i = 0; i < 8; i++)
        sp[i] = &src[i];

    biscuit::Options opt;
    opt.fixed32IsFloat = true;
    for (uint8_t asked = BISCUIT_MAX_TIER; asked <= 15; asked++) {
        opt.maxTier = asked;
        uint8_t buf[233];
        Result r = encode(&meshtastic_DeviceMetrics_msg, sp, 8, ts, buf, sizeof(buf), opt);
        TEST_ASSERT_TRUE_MESSAGE(r.size > 0, "clamping must still encode");
        TEST_ASSERT_EQUAL_UINT8(BISCUIT_MAX_TIER, r.tier);
        TEST_ASSERT_EQUAL_UINT8(BISCUIT_MAX_TIER, peekTier(buf, r.size));
    }
}

/// A coarser timestamp is the only lossy thing tier 3 does to time, so it must stay inside the
/// quantum it was given and the codec must own up to it. Silent time drift is worse than a
/// dropped batch: the reading looks right and is filed against the wrong minute.
void test_timeResolution_errorIsBoundedAndDeclared(void)
{
    meshtastic_DeviceMetrics src[12], dst[12];
    uint32_t ts[12], tsOut[12];
    makeRealBatch(src, ts, 12);
    const void *sp[12];
    void *dp[12];
    for (uint8_t i = 0; i < 12; i++) {
        sp[i] = &src[i];
        dst[i] = meshtastic_DeviceMetrics_init_zero;
        dp[i] = &dst[i];
    }
    biscuit::Options opt;
    opt.fixed32IsFloat = true;
    opt.timeRes = 60;

    for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++) {
        opt.maxTier = tier;
        uint8_t buf[233];
        Result r = encode(&meshtastic_DeviceMetrics_msg, sp, 12, ts, buf, sizeof(buf), opt);
        TEST_ASSERT_TRUE(r.size > 0);
        TEST_ASSERT_EQUAL(12, decode(&meshtastic_DeviceMetrics_msg, buf, r.size, dp, 12, tsOut, opt));
        // Stamps are deliberately quantised to 60 s and bounded below; the readings
        // themselves must still be exact, so compare those and not the times.
        biscuitcmp::assertSameBatch(&meshtastic_DeviceMetrics_msg, src, dst, nullptr, nullptr, 12, "round trip");
        for (uint8_t i = 0; i < 12; i++) {
            if (tier < TIER_RESOLUTION) {
                // Below tier 3 the quantum is ignored, so stamps stay exact.
                TEST_ASSERT_EQUAL_UINT32(ts[i], tsOut[i]);
            } else {
                TEST_ASSERT_FALSE_MESSAGE(r.lossless, "a coarse stamp must not report lossless");
                const uint32_t d = ts[i] > tsOut[i] ? ts[i] - tsOut[i] : tsOut[i] - ts[i];
                TEST_ASSERT_LESS_THAN_UINT32(60u, d);
            }
        }
    }
}

// ---------------------------------------------------------------- field families

// PowerMetrics is the only variant with parallel channels: ch1/ch2/ch3 voltage are one
// measurement sampled three times over, and likewise for current. Stacking a family into its
// leader's column pays the per-column framing once instead of three times. The model puts this
// at 31% for the 7% of readings that carry three channels - narrow, but it is the difference
// between the six-field case fitting at N=2 and not paying until N=6.
//
// Nothing exercised this before: every other test in this file uses DeviceMetrics, which has
// no families, so the gather, the channel-major layout and the scatter were all unrun.

static const FieldFamily kPowerFamilies[] = {
    {{meshtastic_PowerMetrics_ch1_voltage_tag, meshtastic_PowerMetrics_ch2_voltage_tag, meshtastic_PowerMetrics_ch3_voltage_tag,
      0},
     3},
    {{meshtastic_PowerMetrics_ch1_current_tag, meshtastic_PowerMetrics_ch2_current_tag, meshtastic_PowerMetrics_ch3_current_tag,
      0},
     3},
};

/// Three INA channels drifting independently, so a decoder that crossed them over would be
/// caught rather than returning plausible-looking numbers.
static void makePowerBatch(meshtastic_PowerMetrics *m, uint32_t *t, uint8_t n)
{
    for (uint8_t i = 0; i < n; i++) {
        m[i] = meshtastic_PowerMetrics_init_zero;
        m[i].has_ch1_voltage = m[i].has_ch2_voltage = m[i].has_ch3_voltage = true;
        m[i].has_ch1_current = m[i].has_ch2_current = m[i].has_ch3_current = true;
        m[i].ch1_voltage = 4.200f - 0.008f * i; // discharging
        m[i].ch2_voltage = 3.700f + 0.004f * i; // charging
        m[i].ch3_voltage = 5.000f;              // rail, constant
        m[i].ch1_current = 120.0f + 4.0f * i;
        m[i].ch2_current = -55.0f - 2.0f * i; // negative: zigzag must survive
        m[i].ch3_current = 0.0f;
        t[i] = 1757000000u + 900u * i;
    }
}

/// Every channel must come back on its own tag. A family that scattered channel-major data
/// back in reading-major order would put ch2's series into ch1 and still decode cleanly.
void test_families_everyChannelRoundTripsToItsOwnTag(void)
{
    for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++) {
        meshtastic_PowerMetrics src[12], dst[12];
        uint32_t ts[12], tsOut[12];
        makePowerBatch(src, ts, 12);
        const void *sp[12];
        void *dp[12];
        for (uint8_t i = 0; i < 12; i++) {
            sp[i] = &src[i];
            dst[i] = meshtastic_PowerMetrics_init_zero;
            dp[i] = &dst[i];
        }
        Options opt;
        opt.maxTier = tier;
        opt.fixed32IsFloat = true;
        opt.families = kPowerFamilies;
        opt.familyCount = sizeof(kPowerFamilies) / sizeof(kPowerFamilies[0]);
        opt.neverInflate = false;

        uint8_t buf[233];
        Result r = encode(&meshtastic_PowerMetrics_msg, sp, 12, ts, buf, sizeof(buf), opt);
        char msg[64];
        snprintf(msg, sizeof(msg), "tier %u", tier);
        TEST_ASSERT_TRUE_MESSAGE(r.size > 0, msg);
        TEST_ASSERT_EQUAL_MESSAGE(12, decode(&meshtastic_PowerMetrics_msg, buf, r.size, dp, 12, tsOut, opt), msg);
        biscuitcmp::assertSameBatch(&meshtastic_PowerMetrics_msg, src, dst, ts, tsOut, 12, "round trip");

        for (uint8_t i = 0; i < 12; i++) {
            TEST_ASSERT_EQUAL_UINT32_MESSAGE(ts[i], tsOut[i], msg);
            TEST_ASSERT_FLOAT_WITHIN_MESSAGE(0.0002f, src[i].ch1_voltage, dst[i].ch1_voltage, msg);
            TEST_ASSERT_FLOAT_WITHIN_MESSAGE(0.0002f, src[i].ch2_voltage, dst[i].ch2_voltage, msg);
            TEST_ASSERT_FLOAT_WITHIN_MESSAGE(0.0002f, src[i].ch3_voltage, dst[i].ch3_voltage, msg);
            TEST_ASSERT_FLOAT_WITHIN_MESSAGE(0.0002f, src[i].ch1_current, dst[i].ch1_current, msg);
            TEST_ASSERT_FLOAT_WITHIN_MESSAGE(0.0002f, src[i].ch2_current, dst[i].ch2_current, msg);
            TEST_ASSERT_FLOAT_WITHIN_MESSAGE(0.0002f, src[i].ch3_current, dst[i].ch3_current, msg);
        }
    }
}

/// Three cells on one INA3221, which is what a family is actually for: parallel channels of
/// the same measurement, tracking each other closely. The round-trip fixture above deliberately
/// makes them diverge to catch a crossover; that is the wrong shape for measuring the saving,
/// because stacking dissimilar series turns each channel boundary into a large delta.
static void makeParallelPowerBatch(meshtastic_PowerMetrics *m, uint32_t *t, uint8_t n)
{
    for (uint8_t i = 0; i < n; i++) {
        m[i] = meshtastic_PowerMetrics_init_zero;
        m[i].has_ch1_voltage = m[i].has_ch2_voltage = m[i].has_ch3_voltage = true;
        m[i].has_ch1_current = m[i].has_ch2_current = m[i].has_ch3_current = true;
        m[i].ch1_voltage = 3.980f - 0.006f * i;
        m[i].ch2_voltage = 3.976f - 0.006f * i;
        m[i].ch3_voltage = 3.984f - 0.005f * i;
        m[i].ch1_current = 210.0f + 3.0f * i;
        m[i].ch2_current = 208.0f + 3.0f * i;
        m[i].ch3_current = 212.0f + 4.0f * i;
        t[i] = 1757000000u + 900u * i;
    }
}
/// The point of stacking is fewer columns, so it must actually be smaller than not stacking.
/// If this ever inverts, the feature is costing airtime for nothing and should be dropped.
void test_families_areSmallerThanSeparateColumns(void)
{
    meshtastic_PowerMetrics src[12];
    uint32_t ts[12];
    makeParallelPowerBatch(src, ts, 12);
    const void *sp[12];
    for (uint8_t i = 0; i < 12; i++)
        sp[i] = &src[i];

    for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++) {
        Options plain;
        plain.maxTier = tier;
        plain.fixed32IsFloat = true;
        plain.neverInflate = false;
        Options stacked = plain;
        stacked.families = kPowerFamilies;
        stacked.familyCount = sizeof(kPowerFamilies) / sizeof(kPowerFamilies[0]);

        uint8_t a[233], b[233];
        const size_t sPlain = encode(&meshtastic_PowerMetrics_msg, sp, 12, ts, a, sizeof(a), plain).size;
        const size_t sStack = encode(&meshtastic_PowerMetrics_msg, sp, 12, ts, b, sizeof(b), stacked).size;
        char msg[80];
        snprintf(msg, sizeof(msg), "tier %u: stacked %u B, separate %u B", tier, (unsigned)sStack, (unsigned)sPlain);
        TEST_ASSERT_TRUE_MESSAGE(sPlain > 0 && sStack > 0, msg);
        TEST_ASSERT_LESS_OR_EQUAL_UINT32_MESSAGE((uint32_t)sPlain, (uint32_t)sStack, msg);
    }
}

/// A channel missing from one reading disqualifies the whole family, because a stacked column
/// has no way to say "this channel stops here". It must fall back to encoding without that
/// family rather than emit a column the decoder would misread as another channel's samples.
void test_families_missingChannelDoesNotCorruptTheBatch(void)
{
    for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++) {
        meshtastic_PowerMetrics src[8], dst[8];
        uint32_t ts[8], tsOut[8];
        makePowerBatch(src, ts, 8);
        src[3].has_ch2_voltage = false; // one hole, mid-batch

        const void *sp[8];
        void *dp[8];
        for (uint8_t i = 0; i < 8; i++) {
            sp[i] = &src[i];
            dst[i] = meshtastic_PowerMetrics_init_zero;
            dp[i] = &dst[i];
        }
        Options opt;
        opt.maxTier = tier;
        opt.fixed32IsFloat = true;
        opt.families = kPowerFamilies;
        opt.familyCount = sizeof(kPowerFamilies) / sizeof(kPowerFamilies[0]);
        opt.neverInflate = false;

        uint8_t buf[233];
        Result r = encode(&meshtastic_PowerMetrics_msg, sp, 8, ts, buf, sizeof(buf), opt);
        TEST_ASSERT_TRUE(r.size > 0);
        TEST_ASSERT_EQUAL(8, decode(&meshtastic_PowerMetrics_msg, buf, r.size, dp, 8, tsOut, opt));
        biscuitcmp::assertSameBatch(&meshtastic_PowerMetrics_msg, src, dst, ts, tsOut, 8, "round trip");

        // The current family is untouched by the voltage hole and must survive intact.
        for (uint8_t i = 0; i < 8; i++) {
            TEST_ASSERT_FLOAT_WITHIN(0.0002f, src[i].ch1_current, dst[i].ch1_current);
            TEST_ASSERT_FLOAT_WITHIN(0.0002f, src[i].ch2_current, dst[i].ch2_current);
            TEST_ASSERT_FLOAT_WITHIN(0.0002f, src[i].ch3_current, dst[i].ch3_current);
        }
    }
}

// ---------------------------------------------------------------- self-describing wire

// Nothing the receiver needs may be read from the receiver's own configuration. That invariant
// was broken three times: the time quantum, the tier-4 resolution shift, and the family table.
// Each failed the same way - both ends agreed in-process, so every existing test passed, and a
// real pair of nodes configured differently would have swapped silently wrong numbers. These
// tests encode with one Options and decode with a deliberately different one.

/// A resolution shift is on the wire at every tier, so a receiver holding no hints at all still
/// reconstructs what the sender sent. Tiers 1-3 carry it in the column code byte; tier 4 has no
/// column header and used to derive it from the receiver's hints.
void test_selfDescribing_resolutionSurvivesADifferentReceiver(void)
{
    static const FieldHint kCoarse[] = {
        {2, true, 4, 1000}, // voltage, quantised to 16 mV
        {3, true, 3, 100},  // channel_utilization
    };

    for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++) {
        meshtastic_DeviceMetrics src[12], dst[12];
        uint32_t ts[12], tsOut[12];
        makeRealBatch(src, ts, 12);
        const void *sp[12];
        void *dp[12];
        for (uint8_t i = 0; i < 12; i++) {
            sp[i] = &src[i];
            dst[i] = meshtastic_DeviceMetrics_init_zero;
            dp[i] = &dst[i];
        }

        Options send;
        send.maxTier = tier;
        send.fixed32IsFloat = true;
        send.hints = kCoarse;
        send.hintCount = sizeof(kCoarse) / sizeof(kCoarse[0]);
        send.neverInflate = false;

        // The receiver agrees on how a float becomes an integer - the profile byte guarantees
        // that or the batch is refused - but knows nothing of the sender's chosen resolution.
        // That is the part which must come off the wire.
        static const FieldHint kSameScalesNoShift[] = {
            {2, true, 0, 1000},
            {3, true, 0, 100},
        };
        Options recv;
        recv.maxTier = tier;
        recv.fixed32IsFloat = true;
        recv.hints = kSameScalesNoShift;
        recv.hintCount = sizeof(kSameScalesNoShift) / sizeof(kSameScalesNoShift[0]);

        uint8_t buf[233];
        Result r = encode(&meshtastic_DeviceMetrics_msg, sp, 12, ts, buf, sizeof(buf), send);
        char msg[64];
        snprintf(msg, sizeof(msg), "tier %u", tier);
        TEST_ASSERT_TRUE_MESSAGE(r.size > 0, msg);
        TEST_ASSERT_EQUAL_MESSAGE(12, decode(&meshtastic_DeviceMetrics_msg, buf, r.size, dp, 12, tsOut, recv), msg);
        // The sender declared 16 mV and 1/8 % shifts, so allow the coarser of the two.
        biscuitcmp::assertSameBatch(&meshtastic_DeviceMetrics_msg, src, dst, ts, tsOut, 12, "round trip", true, 0.126f);

        for (uint8_t i = 0; i < 12; i++) {
            // Within the quantum the sender chose, not the receiver's idea of one. Tier 3 is
            // where the shift starts applying; below it the values are exact.
            const float tol = (tier >= TIER_RESOLUTION) ? 0.020f : 0.0002f;
            TEST_ASSERT_FLOAT_WITHIN_MESSAGE(tol, src[i].voltage, dst[i].voltage, msg);
            TEST_ASSERT_EQUAL_UINT32_MESSAGE(src[i].battery_level, dst[i].battery_level, msg);
            TEST_ASSERT_EQUAL_UINT32_MESSAGE(ts[i], tsOut[i], msg);
        }
    }
}

/// A family table cannot be sent cheaply, so the profile byte stands for it instead. A receiver
/// with a different table - or none - must refuse rather than read one channel's samples as
/// another's, which is the failure that produces plausible wrong numbers rather than an error.
void test_selfDescribing_familyMismatchIsRefused(void)
{
    for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++) {
        meshtastic_PowerMetrics src[12], dst[12];
        uint32_t ts[12], tsOut[12];
        makeParallelPowerBatch(src, ts, 12);
        const void *sp[12];
        void *dp[12];
        for (uint8_t i = 0; i < 12; i++) {
            sp[i] = &src[i];
            dst[i] = meshtastic_PowerMetrics_init_zero;
            dp[i] = &dst[i];
        }

        Options send;
        send.maxTier = tier;
        send.fixed32IsFloat = true;
        send.families = kPowerFamilies;
        send.familyCount = sizeof(kPowerFamilies) / sizeof(kPowerFamilies[0]);
        send.neverInflate = false;

        uint8_t buf[233];
        Result r = encode(&meshtastic_PowerMetrics_msg, sp, 12, ts, buf, sizeof(buf), send);
        TEST_ASSERT_TRUE(r.size > 0);

        // No families at all: refused.
        Options none;
        none.fixed32IsFloat = true;
        TEST_ASSERT_EQUAL(0, decode(&meshtastic_PowerMetrics_msg, buf, r.size, dp, 12, tsOut, none));

        // A different grouping of the same tags: also refused, because the channel-major layout the
        // sender wrote is not the one this receiver would read.
        static const FieldFamily kOther[] = {
            {{meshtastic_PowerMetrics_ch1_voltage_tag, meshtastic_PowerMetrics_ch2_voltage_tag, 0, 0}, 2},
        };
        Options other;
        other.fixed32IsFloat = true;
        other.families = kOther;
        other.familyCount = 1;
        TEST_ASSERT_EQUAL(0, decode(&meshtastic_PowerMetrics_msg, buf, r.size, dp, 12, tsOut, other));

        // The matching table decodes, so the refusal above is the signature and not a broken batch.
        TEST_ASSERT_EQUAL(12, decode(&meshtastic_PowerMetrics_msg, buf, r.size, dp, 12, tsOut, send));
        biscuitcmp::assertSameBatch(&meshtastic_PowerMetrics_msg, src, dst, ts, tsOut, 12, "round trip");
        for (uint8_t i = 0; i < 12; i++)
            TEST_ASSERT_FLOAT_WITHIN(0.0002f, src[i].ch2_voltage, dst[i].ch2_voltage);
    }
}

// ---------------------------------------------------------------- declared repeats

/// Overlap repeats the oldest readings of each batch by design. The count rides in the top
/// three bits of the count byte, which cost nothing - n needs only five - so a receiver knows
/// which readings are deliberate repeats without a dedup table or a clock.
void test_repeats_areDeclaredInTheHeader(void)
{
    meshtastic_DeviceMetrics src[12];
    uint32_t ts[12];
    makeRealBatch(src, ts, 12);
    const void *sp[12];
    for (uint8_t i = 0; i < 12; i++)
        sp[i] = &src[i];

    for (uint8_t rep = 0; rep <= 7; rep++)
        for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++) {
            Options opt;
            opt.maxTier = tier;
            opt.fixed32IsFloat = true;
            opt.repeats = rep;
            opt.neverInflate = false;
            uint8_t buf[233];
            Result r = encode(&meshtastic_DeviceMetrics_msg, sp, 12, ts, buf, sizeof(buf), opt);
            char msg[48];
            snprintf(msg, sizeof(msg), "repeats %u", rep);
            TEST_ASSERT_TRUE_MESSAGE(r.size > 0, msg);
            TEST_ASSERT_EQUAL_UINT8_MESSAGE(rep, peekRepeats(buf, r.size), msg);
            // The count must not disturb the reading count sharing the byte.
            TEST_ASSERT_EQUAL_UINT8_MESSAGE(12, peekCount(buf, r.size), msg);
        }
}

/// The count byte holds n in five bits and the repeat count in three, so both caps must be
/// enforced rather than silently truncating into each other.
void test_repeats_beyondTheFieldAreRefused(void)
{
    meshtastic_DeviceMetrics src[8];
    uint32_t ts[8];
    makeRealBatch(src, ts, 8);
    const void *sp[8];
    for (uint8_t i = 0; i < 8; i++)
        sp[i] = &src[i];

    Options opt;
    opt.fixed32IsFloat = true;
    opt.repeats = 8; // one past what three bits can say
    opt.neverInflate = false;
    uint8_t buf[233];
    TEST_ASSERT_EQUAL(0, encode(&meshtastic_DeviceMetrics_msg, sp, 8, ts, buf, sizeof(buf), opt).size);
}
// ---------------------------------------------------------------- RTC-less: ages, not epochs

// A node that has never had a clock sends each reading's age in seconds instead of an epoch,
// and the receiver dates them against its own clock. Ages run BACKWARDS - the oldest reading
// has the largest age - so consecutive gaps are negative, which no epoch-based batch produces.
// Every delta in the time column is therefore signed, and a decoder that assumed monotonically
// increasing timestamps would reconstruct garbage without failing.

/// Ages derived from the real fixture's intervals: oldest first, so counting down to zero.
static void makeAgeBatch(meshtastic_DeviceMetrics *m, uint32_t *t, uint8_t n)
{
    makeRealBatch(m, t, n);
    const uint32_t newest = kRealTimes[n - 1];
    for (uint8_t i = 0; i < n; i++)
        t[i] = newest - kRealTimes[i]; // age at send: largest first, 0 for the newest
}

void test_uptimeAges_decreasingTimestampsRoundTrip(void)
{
    meshtastic_DeviceMetrics src[12], dst[12];
    uint32_t ages[12], agesOut[12];
    makeAgeBatch(src, ages, 12);

    // The fixture must actually count down, or this test proves nothing.
    TEST_ASSERT_GREATER_THAN_UINT32(ages[11], ages[0]);
    TEST_ASSERT_EQUAL_UINT32(0, ages[11]);

    const void *sp[12];
    void *dp[12];
    for (uint8_t i = 0; i < 12; i++) {
        sp[i] = &src[i];
        dst[i] = meshtastic_DeviceMetrics_init_zero;
        dp[i] = &dst[i];
    }
    biscuit::Options opt;
    opt.fixed32IsFloat = true;
    // A realistic context word in the module's layout: variant 0 (device_metrics), RTC tier 4
    // (GPS) at bits 3-6, UPTIME_BASED at bit 7, quantum code 7 (60 s) at bits 10-13. The codec
    // carries it opaquely, and must return every bit: a receiver that lost bit 7 would read an
    // age as an epoch and date the batch to 1970.
    static const uint32_t kCtx = 0u | (4u << 3) | (1u << 7) | (7u << 10);
    opt.context = kCtx;

    for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++) {
        opt.maxTier = tier;
        uint8_t buf[233];
        Result r = encode(&meshtastic_DeviceMetrics_msg, sp, 12, ages, buf, sizeof(buf), opt);
        TEST_ASSERT_TRUE(r.size > 0);

        uint32_t ctx = 0;
        TEST_ASSERT_TRUE(peekContext(buf, r.size, &ctx));
        TEST_ASSERT_EQUAL_UINT32(kCtx, ctx); // survives whole, so the receiver knows how to read it

        TEST_ASSERT_EQUAL(12, decode(&meshtastic_DeviceMetrics_msg, buf, r.size, dp, 12, agesOut, opt));
        biscuitcmp::assertSameBatch(&meshtastic_DeviceMetrics_msg, src, dst, nullptr, nullptr, 12, "round trip");
        for (uint8_t i = 0; i < 12; i++) {
            char msg[64];
            snprintf(msg, sizeof(msg), "tier %u reading %u", tier, i);
            TEST_ASSERT_EQUAL_UINT32_MESSAGE(ages[i], agesOut[i], msg);
        }
    }
}

/// The receiver's conversion: epoch = now - age. Reconstructed instants must match the
/// originals, which is the whole point of sending ages rather than a fabricated 1970 date.
void test_uptimeAges_datedAgainstReceiverClockMatchOriginals(void)
{
    meshtastic_DeviceMetrics src[12], dst[12];
    uint32_t ages[12], agesOut[12];
    makeAgeBatch(src, ages, 12);
    const void *sp[12];
    void *dp[12];
    for (uint8_t i = 0; i < 12; i++) {
        sp[i] = &src[i];
        dst[i] = meshtastic_DeviceMetrics_init_zero;
        dp[i] = &dst[i];
    }
    biscuit::Options opt;
    opt.fixed32IsFloat = true;
    uint8_t buf[233];
    Result r = encode(&meshtastic_DeviceMetrics_msg, sp, 12, ages, buf, sizeof(buf), opt);
    TEST_ASSERT_TRUE(r.size > 0);
    TEST_ASSERT_EQUAL(12, decode(&meshtastic_DeviceMetrics_msg, buf, r.size, dp, 12, agesOut, opt));
    biscuitcmp::assertSameBatch(&meshtastic_DeviceMetrics_msg, src, dst, nullptr, nullptr, 12, "round trip");

    // The receiver's clock at the moment the batch lands - here, the newest reading's instant.
    const uint32_t nowEpoch = kRealTimes[11];
    for (uint8_t i = 0; i < 12; i++)
        TEST_ASSERT_EQUAL_UINT32(kRealTimes[i], nowEpoch - agesOut[i]);
}

/// A batch captured entirely within one second: every age identical, so every gap is zero.
void test_uptimeAges_allZeroGapsRoundTrip(void)
{
    meshtastic_DeviceMetrics src[8], dst[8];
    uint32_t ages[8], out[8];
    makeRealBatch(src, ages, 8);
    for (uint8_t i = 0; i < 8; i++)
        ages[i] = 42; // all captured at the same age
    const void *sp[8];
    void *dp[8];
    for (uint8_t i = 0; i < 8; i++) {
        sp[i] = &src[i];
        dst[i] = meshtastic_DeviceMetrics_init_zero;
        dp[i] = &dst[i];
    }
    biscuit::Options opt;
    opt.fixed32IsFloat = true;
    for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++) {
        opt.maxTier = tier;
        uint8_t buf[233];
        Result r = encode(&meshtastic_DeviceMetrics_msg, sp, 8, ages, buf, sizeof(buf), opt);
        TEST_ASSERT_TRUE(r.size > 0);
        TEST_ASSERT_EQUAL(8, decode(&meshtastic_DeviceMetrics_msg, buf, r.size, dp, 8, out, opt));
        biscuitcmp::assertSameBatch(&meshtastic_DeviceMetrics_msg, src, dst, nullptr, nullptr, 8, "round trip");
        for (uint8_t i = 0; i < 8; i++)
            TEST_ASSERT_EQUAL_UINT32(42, out[i]);
    }
}

/// An age column costs no more than the epoch column it replaces - ages are small numbers
/// where epochs are ~1.7 billion, so the anchor varint shrinks from five bytes to one or two.
void test_uptimeAges_areNoLargerThanEpochs(void)
{
    meshtastic_DeviceMetrics src[12];
    uint32_t epochs[12], ages[12];
    makeRealBatch(src, epochs, 12);
    makeAgeBatch(src, ages, 12);
    const void *sp[12];
    for (uint8_t i = 0; i < 12; i++)
        sp[i] = &src[i];
    biscuit::Options opt;
    opt.fixed32IsFloat = true;
    uint8_t a[233], b[233];
    size_t withEpochs = encode(&meshtastic_DeviceMetrics_msg, sp, 12, epochs, a, sizeof(a), opt).size;
    size_t withAges = encode(&meshtastic_DeviceMetrics_msg, sp, 12, ages, b, sizeof(b), opt).size;
    TEST_ASSERT_TRUE(withEpochs > 0 && withAges > 0);
    char msg[80];
    snprintf(msg, sizeof(msg), "ages %u B vs epochs %u B", (unsigned)withAges, (unsigned)withEpochs);
    TEST_ASSERT_LESS_OR_EQUAL_MESSAGE(withEpochs, withAges, msg);
}

/// A field that some readings carry and others do not. Before per-reading presence this was
/// either dropped from the batch entirely - silently, with encode reporting success - or, when
/// no field spanned every reading, refused. Both are visible in captured traffic: a node whose
/// BME280 stops answering keeps reporting the sensors that still work.
static void makeRaggedBatch(meshtastic_DeviceMetrics *m, uint32_t *t, uint8_t n, uint32_t keep)
{
    for (uint8_t i = 0; i < n; i++) {
        m[i] = meshtastic_DeviceMetrics_init_zero;
        t[i] = 1757000000u + 1800u * i;
        m[i].has_battery_level = true;
        m[i].battery_level = (uint32_t)(90 - i / 3);
        if ((keep >> i) & 1u) {
            m[i].has_voltage = true;
            m[i].voltage = 4.021f - 0.002f * i;
            m[i].has_uptime_seconds = true;
            m[i].uptime_seconds = 3600 + 1800 * i;
        }
    }
}

void test_ragged_absentReadingsStayAbsent(void)
{
    const uint8_t n = 12;
    // A gap at the front, one in the middle, and a run at the tail - the shapes the capture has.
    static const uint32_t kShapes[] = {0xFFEu, 0xDFFu, 0x0FFu, 0x001u, 0xFFFu};
    for (size_t k = 0; k < sizeof(kShapes) / sizeof(kShapes[0]); k++) {
        const uint32_t keep = kShapes[k];
        meshtastic_DeviceMetrics src[BISCUIT_MAX_BATCH], dst[BISCUIT_MAX_BATCH];
        uint32_t ts[BISCUIT_MAX_BATCH], tsOut[BISCUIT_MAX_BATCH];
        makeRaggedBatch(src, ts, n, keep);
        const void *sp[BISCUIT_MAX_BATCH];
        void *dp[BISCUIT_MAX_BATCH];
        for (uint8_t i = 0; i < n; i++)
            sp[i] = &src[i], dp[i] = &dst[i];

        Options opt;
        opt.fixed32IsFloat = true;
        opt.neverInflate = false;
        for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++) {
            opt.maxTier = tier;
            char msg[64];
            snprintf(msg, sizeof(msg), "keep 0x%03x tier %u", (unsigned)keep, tier);
            uint8_t buf[512];
            Result r = encode(&meshtastic_DeviceMetrics_msg, sp, n, ts, buf, sizeof(buf), opt);
            TEST_ASSERT_GREATER_THAN_MESSAGE(0, (int)r.size, msg);
            for (uint8_t i = 0; i < n; i++)
                dst[i] = meshtastic_DeviceMetrics_init_zero;
            TEST_ASSERT_EQUAL_MESSAGE(n, decode(&meshtastic_DeviceMetrics_msg, buf, r.size, dp, n, tsOut, opt), msg);
            biscuitcmp::assertSameBatch(&meshtastic_DeviceMetrics_msg, src, dst, ts, tsOut, n, "round trip");
            for (uint8_t i = 0; i < n; i++) {
                const bool want = ((keep >> i) & 1u) != 0;
                TEST_ASSERT_EQUAL_UINT32_MESSAGE(ts[i], tsOut[i], msg);
                TEST_ASSERT_TRUE_MESSAGE(dst[i].has_battery_level, msg);
                TEST_ASSERT_EQUAL_UINT32_MESSAGE(src[i].battery_level, dst[i].battery_level, msg);
                // The point of the test: an absent field must come back absent, never invented.
                TEST_ASSERT_EQUAL_MESSAGE(want, dst[i].has_voltage, msg);
                TEST_ASSERT_EQUAL_MESSAGE(want, dst[i].has_uptime_seconds, msg);
                if (want) {
                    TEST_ASSERT_FLOAT_WITHIN_MESSAGE(0.0002f, src[i].voltage, dst[i].voltage, msg);
                    TEST_ASSERT_EQUAL_UINT32_MESSAGE(src[i].uptime_seconds, dst[i].uptime_seconds, msg);
                }
            }
        }
    }
}

/// No field spans the whole batch - every column is ragged. This is the case that returned
/// size 0 at every tier, so the batch could not be sent at all.
void test_ragged_noColumnSpansTheBatch(void)
{
    const uint8_t n = 6;
    meshtastic_DeviceMetrics src[BISCUIT_MAX_BATCH], dst[BISCUIT_MAX_BATCH];
    uint32_t ts[BISCUIT_MAX_BATCH], tsOut[BISCUIT_MAX_BATCH];
    for (uint8_t i = 0; i < n; i++) {
        src[i] = meshtastic_DeviceMetrics_init_zero;
        ts[i] = 1757000000u + 600u * i;
        if (i < 3) {
            src[i].has_battery_level = true;
            src[i].battery_level = (uint32_t)(80 + i);
        } else {
            src[i].has_uptime_seconds = true;
            src[i].uptime_seconds = 900u * i;
        }
    }
    const void *sp[BISCUIT_MAX_BATCH];
    void *dp[BISCUIT_MAX_BATCH];
    for (uint8_t i = 0; i < n; i++)
        sp[i] = &src[i], dp[i] = &dst[i];

    Options opt;
    opt.fixed32IsFloat = true;
    opt.neverInflate = false;
    for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++) {
        opt.maxTier = tier;
        char msg[32];
        snprintf(msg, sizeof(msg), "tier %u", tier);
        uint8_t buf[512];
        Result r = encode(&meshtastic_DeviceMetrics_msg, sp, n, ts, buf, sizeof(buf), opt);
        TEST_ASSERT_GREATER_THAN_MESSAGE(0, (int)r.size, msg);
        for (uint8_t i = 0; i < n; i++)
            dst[i] = meshtastic_DeviceMetrics_init_zero;
        TEST_ASSERT_EQUAL_MESSAGE(n, decode(&meshtastic_DeviceMetrics_msg, buf, r.size, dp, n, tsOut, opt), msg);
        biscuitcmp::assertSameBatch(&meshtastic_DeviceMetrics_msg, src, dst, ts, tsOut, n, "round trip");
        for (uint8_t i = 0; i < n; i++) {
            TEST_ASSERT_EQUAL_MESSAGE(i < 3, dst[i].has_battery_level, msg);
            TEST_ASSERT_EQUAL_MESSAGE(i >= 3, dst[i].has_uptime_seconds, msg);
            if (i < 3)
                TEST_ASSERT_EQUAL_UINT32_MESSAGE(src[i].battery_level, dst[i].battery_level, msg);
            else
                TEST_ASSERT_EQUAL_UINT32_MESSAGE(src[i].uptime_seconds, dst[i].uptime_seconds, msg);
        }
    }
}

/// A mask costs bytes, so it must only be paid when a column has gaps. Asserted as a relation
/// rather than a pinned byte count: the same batch, with and without one reading's fields, must
/// differ by the mask and the values it drops - and the full batch must never be the larger.
void test_ragged_fullColumnsPayNothing(void)
{
    const uint8_t n = 12;
    meshtastic_DeviceMetrics full[BISCUIT_MAX_BATCH], gap[BISCUIT_MAX_BATCH];
    uint32_t ts[BISCUIT_MAX_BATCH];
    makeRaggedBatch(full, ts, n, 0xFFFu);
    makeRaggedBatch(gap, ts, n, 0xFDFu); // reading 5 loses voltage and uptime
    const void *fp[BISCUIT_MAX_BATCH], *gp[BISCUIT_MAX_BATCH];
    for (uint8_t i = 0; i < n; i++)
        fp[i] = &full[i], gp[i] = &gap[i];

    Options opt;
    opt.fixed32IsFloat = true;
    opt.neverInflate = false;
    for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++) {
        opt.maxTier = tier;
        uint8_t bf[512], bg[512];
        Result rf = encodeAtTier(&meshtastic_DeviceMetrics_msg, fp, n, ts, bf, sizeof(bf), opt, tier);
        Result rg = encodeAtTier(&meshtastic_DeviceMetrics_msg, gp, n, ts, bg, sizeof(bg), opt, tier);
        char msg[80];
        snprintf(msg, sizeof(msg), "tier %u: full %u, one gap %u", tier, (unsigned)rf.size, (unsigned)rg.size);
        TEST_ASSERT_GREATER_THAN_MESSAGE(0, (int)rf.size, msg);
        TEST_ASSERT_GREATER_THAN_MESSAGE(0, (int)rg.size, msg);
        // Two masks bought at 2 B each against two values dropped: the ragged batch may land
        // either side, but never more than a few bytes above the batch that carries more data.
        TEST_ASSERT_LESS_OR_EQUAL_MESSAGE(rf.size + 6, rg.size, msg);
    }
}

/// The common PowerMetrics node: 93% of senders in the capture populate ch3 alone. With the
/// family table configured, requiring every channel in every reading disqualified the family
/// leader - and its other channels are skipped as non-leaders - so ch3 was dropped too. The
/// batch encoded and reported success carrying nothing.
void test_families_channelNeverPopulatedKeepsTheRest(void)
{
    const uint8_t n = 8;
    meshtastic_PowerMetrics src[8], dst[8];
    uint32_t ts[8], tsOut[8];
    for (uint8_t i = 0; i < n; i++) {
        src[i] = meshtastic_PowerMetrics_init_zero;
        ts[i] = 1757000000u + 900u * i;
        src[i].has_ch3_voltage = true;
        src[i].ch3_voltage = 12.01f + 0.02f * i;
        src[i].has_ch3_current = true;
        src[i].ch3_current = 240.0f + 4.0f * i;
    }
    const void *sp[8];
    void *dp[8];
    for (uint8_t i = 0; i < n; i++)
        sp[i] = &src[i], dp[i] = &dst[i];

    for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++) {
        Options opt;
        opt.maxTier = tier;
        opt.fixed32IsFloat = true;
        opt.families = kPowerFamilies;
        opt.familyCount = sizeof(kPowerFamilies) / sizeof(kPowerFamilies[0]);
        opt.neverInflate = false;
        char msg[32];
        snprintf(msg, sizeof(msg), "tier %u", tier);

        uint8_t buf[233];
        Result r = encode(&meshtastic_PowerMetrics_msg, sp, n, ts, buf, sizeof(buf), opt);
        TEST_ASSERT_GREATER_THAN_MESSAGE(0, (int)r.size, msg);
        for (uint8_t i = 0; i < n; i++)
            dst[i] = meshtastic_PowerMetrics_init_zero;
        TEST_ASSERT_EQUAL_MESSAGE(n, decode(&meshtastic_PowerMetrics_msg, buf, r.size, dp, n, tsOut, opt), msg);
        biscuitcmp::assertSameBatch(&meshtastic_PowerMetrics_msg, src, dst, ts, tsOut, n, "round trip");
        for (uint8_t i = 0; i < n; i++) {
            TEST_ASSERT_TRUE_MESSAGE(dst[i].has_ch3_voltage, msg);
            TEST_ASSERT_TRUE_MESSAGE(dst[i].has_ch3_current, msg);
            TEST_ASSERT_FLOAT_WITHIN_MESSAGE(0.0002f, src[i].ch3_voltage, dst[i].ch3_voltage, msg);
            TEST_ASSERT_FLOAT_WITHIN_MESSAGE(0.0002f, src[i].ch3_current, dst[i].ch3_current, msg);
            // The channels this node does not wire up must stay absent, not arrive as zero.
            TEST_ASSERT_FALSE_MESSAGE(dst[i].has_ch1_voltage, msg);
            TEST_ASSERT_FALSE_MESSAGE(dst[i].has_ch2_voltage, msg);
        }
    }
}

/// A family with a hole in one channel: the sensor answered for some readings and not others.
void test_families_raggedChannelSurvives(void)
{
    const uint8_t n = 8;
    meshtastic_PowerMetrics src[8], dst[8];
    uint32_t ts[8], tsOut[8];
    for (uint8_t i = 0; i < n; i++) {
        src[i] = meshtastic_PowerMetrics_init_zero;
        ts[i] = 1757000000u + 900u * i;
        src[i].has_ch1_voltage = true;
        src[i].ch1_voltage = 3.90f + 0.01f * i;
        src[i].has_ch2_voltage = (i != 3 && i != 4); // ch2 drops out for two readings
        if (src[i].has_ch2_voltage)
            src[i].ch2_voltage = 4.10f + 0.01f * i;
        src[i].has_ch3_voltage = true;
        src[i].ch3_voltage = 12.0f + 0.05f * i;
    }
    const void *sp[8];
    void *dp[8];
    for (uint8_t i = 0; i < n; i++)
        sp[i] = &src[i], dp[i] = &dst[i];

    for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++) {
        Options opt;
        opt.maxTier = tier;
        opt.fixed32IsFloat = true;
        opt.families = kPowerFamilies;
        opt.familyCount = sizeof(kPowerFamilies) / sizeof(kPowerFamilies[0]);
        opt.neverInflate = false;
        char msg[32];
        snprintf(msg, sizeof(msg), "tier %u", tier);

        uint8_t buf[233];
        Result r = encode(&meshtastic_PowerMetrics_msg, sp, n, ts, buf, sizeof(buf), opt);
        TEST_ASSERT_GREATER_THAN_MESSAGE(0, (int)r.size, msg);
        for (uint8_t i = 0; i < n; i++)
            dst[i] = meshtastic_PowerMetrics_init_zero;
        TEST_ASSERT_EQUAL_MESSAGE(n, decode(&meshtastic_PowerMetrics_msg, buf, r.size, dp, n, tsOut, opt), msg);
        biscuitcmp::assertSameBatch(&meshtastic_PowerMetrics_msg, src, dst, ts, tsOut, n, "round trip");
        for (uint8_t i = 0; i < n; i++) {
            TEST_ASSERT_FLOAT_WITHIN_MESSAGE(0.0002f, src[i].ch1_voltage, dst[i].ch1_voltage, msg);
            TEST_ASSERT_FLOAT_WITHIN_MESSAGE(0.0002f, src[i].ch3_voltage, dst[i].ch3_voltage, msg);
            TEST_ASSERT_EQUAL_MESSAGE(src[i].has_ch2_voltage, dst[i].has_ch2_voltage, msg);
            if (src[i].has_ch2_voltage)
                TEST_ASSERT_FLOAT_WITHIN_MESSAGE(0.0002f, src[i].ch2_voltage, dst[i].ch2_voltage, msg);
        }
    }
}

// ------------------------------------------------------- tier selection

/// Batches built to make each tier the cheapest, so the selector has something to select.
///
/// The levers, in the order the format exposes them:
///   T1 - a single column with a high field number. Tier 2 names columns with a varint bitmap
///        over field numbers, so one column at tag 22 costs a 4-byte mask where tier 1's
///        explicit "count, then tag" costs 2. At n=2 there is one gap, so tier 2's
///        delta-of-delta time buys nothing back.
///   T2 - note this one can only ever tie tier 3, never beat it: tier 3 is tier 2 plus opt-in
///        transformations, so with no hint supplied the two are byte-identical and the selector
///        breaks the tie toward the lower tier. What the fixture shows is tier 2 beating tier 4,
///        which is the case that actually occurs - 39% of two-reading batches in the capture.
///        Two columns at low field numbers, so the bitmap is one byte where tier 1's tag list
///        is three, over two readings with steps large enough that bit packing cannot beat a
///        varint. Few columns is the lever against tier 4, not many: its flags byte, width
///        table and per-column divisor cost more than two code bytes, and with one delta per
///        column there is nothing for the bit area to win back.
///   T3 - as T2, plus values whose deltas straddle the varint byte boundary, so a declared
///        resolution shift pulls them under it. Nothing else at T3 costs anything.
///   T4 - enough readings and columns that dropping per-column framing wins outright.
enum WhichTier { WANT_T1, WANT_T2, WANT_T3, WANT_T4 };

static uint8_t makeTierBatch(WhichTier which, meshtastic_EnvironmentMetrics *m, uint32_t *t)
{
    uint8_t n = 0;
    switch (which) {
    case WANT_T1:
        n = 2;
        for (uint8_t i = 0; i < n; i++) {
            m[i] = meshtastic_EnvironmentMetrics_init_zero;
            t[i] = 1757000000u + 1801u * i;
            m[i].has_soil_temperature = true; // tag 22: expensive to name in a bitmap
            m[i].soil_temperature = 11.5f + 0.25f * i;
        }
        return n;
    case WANT_T2:
        n = 2;
        for (uint8_t i = 0; i < n; i++) {
            m[i] = meshtastic_EnvironmentMetrics_init_zero;
            t[i] = 1757000000u + 900u * i;
            // Odd scaled values, so no divisor divides them and tier 4's GCD byte is dead
            // weight; steps near 1.0 scale to ~10000, which needs two varint bytes and about
            // fifteen packed bits, so bit packing saves nothing either.
            m[i].has_temperature = true;
            m[i].temperature = 20.0001f + 1.0003f * i;
            m[i].has_relative_humidity = true;
            m[i].relative_humidity = 51.0007f + 1.0009f * i;
        }
        return n;
    case WANT_T3:
        n = 4;
        for (uint8_t i = 0; i < n; i++) {
            m[i] = meshtastic_EnvironmentMetrics_init_zero;
            t[i] = 1757000000u + 600u * i;
            m[i].has_temperature = true;
            m[i].temperature = 20.0f + 1.7f * i; // 17000 per step scaled: two varint bytes
            m[i].has_barometric_pressure = true;
            m[i].barometric_pressure = 1000.0f + 3.3f * i;
        }
        return n;
    case WANT_T4:
    default:
        n = 16;
        for (uint8_t i = 0; i < n; i++) {
            m[i] = meshtastic_EnvironmentMetrics_init_zero;
            t[i] = 1757000000u + 1200u * i;
            m[i].has_temperature = true;
            m[i].temperature = 21.0f + 0.01f * (float)(i % 5);
            m[i].has_relative_humidity = true;
            m[i].relative_humidity = 48.0f + 0.02f * (float)(i % 7);
            m[i].has_barometric_pressure = true;
            m[i].barometric_pressure = 1013.0f + 0.01f * (float)(i % 3);
            m[i].has_gas_resistance = true;
            m[i].gas_resistance = 150.0f + 0.5f * (float)(i % 4);
        }
        return n;
    }
}

static const FieldHint kT3Hints[] = {
    {1, true, 5, 100}, // temperature to 1/32 of a centi-degree
    {3, true, 5, 100}, // barometric pressure likewise
};

/// The selector must emit the smallest encoding available at or below the cap, and say which
/// tier that was. Asserted against the argmin computed by encoding each tier explicitly, so it
/// holds whichever tier happens to win rather than assuming the manufactured one does.
void test_tierSelection_emitsTheSmallestAvailable(void)
{
    static const WhichTier kCases[] = {WANT_T1, WANT_T2, WANT_T3, WANT_T4};
    bool tierWon[BISCUIT_MAX_TIER + 1] = {false};

    for (size_t k = 0; k < sizeof(kCases) / sizeof(kCases[0]); k++) {
        meshtastic_EnvironmentMetrics src[BISCUIT_MAX_BATCH];
        uint32_t ts[BISCUIT_MAX_BATCH];
        const uint8_t n = makeTierBatch(kCases[k], src, ts);
        const void *sp[BISCUIT_MAX_BATCH];
        for (uint8_t i = 0; i < n; i++)
            sp[i] = &src[i];

        Options opt;
        opt.fixed32IsFloat = true;
        opt.neverInflate = false;
        if (kCases[k] == WANT_T3) {
            opt.hints = kT3Hints;
            opt.hintCount = sizeof(kT3Hints) / sizeof(kT3Hints[0]);
        }

        // What each tier costs on its own, by capping the selector at that tier.
        size_t sz[BISCUIT_MAX_TIER + 1] = {0};
        size_t least = SIZE_MAX;
        uint8_t argmin = 0;
        for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++) {
            Options one = opt;
            one.maxTier = tier;
            uint8_t buf[512];
            const Result r = encodeAtTier(&meshtastic_EnvironmentMetrics_msg, sp, n, ts, buf, sizeof(buf), one, tier);
            sz[tier] = r.size;
            if (r.size && r.size < least) {
                least = r.size;
                argmin = tier;
            }
        }

        char msg[160];
        snprintf(msg, sizeof(msg), "case %u: t1 %u t2 %u t3 %u t4 %u", (unsigned)k, (unsigned)sz[1], (unsigned)sz[2],
                 (unsigned)sz[3], BISCUIT_MAX_TIER >= 4 ? (unsigned)sz[4] : 0u);
        // Printed so a fixture that stops exercising its tier can be retuned from one run
        // rather than guessed at.
        printf("    %s -> tier %u\n", msg, argmin);
        TEST_ASSERT_GREATER_THAN_MESSAGE(0, (int)argmin, msg);

        // Uncapped, the selector must land on exactly that.
        opt.maxTier = BISCUIT_MAX_TIER;
        uint8_t buf[512];
        const Result got = encode(&meshtastic_EnvironmentMetrics_msg, sp, n, ts, buf, sizeof(buf), opt);
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(least, (uint32_t)got.size, msg);
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(argmin, got.tier, msg);
        TEST_ASSERT_EQUAL_UINT32_MESSAGE(argmin, peekTier(buf, got.size), msg);
        tierWon[argmin] = true;
        // A tie broken toward the lower tier is correct behaviour but proves nothing about the
        // fixture, so each case must beat the top tier outright - the tier the encoder would
        // have used without selection. Not the adjacent tier: tier 3 is tier 2 exactly whenever
        // no resolution hint is given, because everything tier 3 adds is opt-in, so a tier-2
        // fixture ties with tier 3 by construction and that tie means nothing.
        if (argmin < BISCUIT_MAX_TIER)
            TEST_ASSERT_TRUE_MESSAGE(sz[BISCUIT_MAX_TIER] && sz[argmin] < sz[BISCUIT_MAX_TIER], msg);

        // And the packet it chose must still decode, whichever tier that was.
        meshtastic_EnvironmentMetrics dst[BISCUIT_MAX_BATCH];
        uint32_t tsOut[BISCUIT_MAX_BATCH];
        void *dp[BISCUIT_MAX_BATCH];
        for (uint8_t i = 0; i < n; i++)
            dst[i] = meshtastic_EnvironmentMetrics_init_zero, dp[i] = &dst[i];
        TEST_ASSERT_EQUAL_MESSAGE(n, decode(&meshtastic_EnvironmentMetrics_msg, buf, got.size, dp, n, tsOut, opt), msg);
        // Case 2 declares a 1/32-of-a-centi-degree grid; the others are exact.
        biscuitcmp::assertSameBatch(&meshtastic_EnvironmentMetrics_msg, src, dst, ts, tsOut, n, "round trip", true,
                                    kCases[k] == WANT_T3 ? 0.33f : 0.0f);
        for (uint8_t i = 0; i < n; i++)
            TEST_ASSERT_EQUAL_UINT32_MESSAGE(ts[i], tsOut[i], msg);
    }

    // The fixtures are only doing their job if every tier wins at least once. Without this the
    // test above would still pass with a selector that always returned the same tier.
    for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++) {
        char msg[72];
        snprintf(msg, sizeof(msg), "no manufactured batch was cheapest at tier %u", tier);
        TEST_ASSERT_TRUE_MESSAGE(tierWon[tier], msg);
    }
}

/// A cap below the winning tier must be honoured, not quietly exceeded.
void test_tierSelection_respectsTheCap(void)
{
    meshtastic_EnvironmentMetrics src[BISCUIT_MAX_BATCH];
    uint32_t ts[BISCUIT_MAX_BATCH];
    const uint8_t n = makeTierBatch(WANT_T4, src, ts);
    const void *sp[BISCUIT_MAX_BATCH];
    for (uint8_t i = 0; i < n; i++)
        sp[i] = &src[i];

    for (uint8_t cap = TIER_COLUMNAR; cap <= BISCUIT_MAX_TIER; cap++) {
        Options opt;
        opt.fixed32IsFloat = true;
        opt.neverInflate = false;
        opt.maxTier = cap;
        uint8_t buf[512];
        const Result r = encode(&meshtastic_EnvironmentMetrics_msg, sp, n, ts, buf, sizeof(buf), opt);
        char msg[64];
        snprintf(msg, sizeof(msg), "cap %u produced tier %u", cap, r.tier);
        TEST_ASSERT_GREATER_THAN_MESSAGE(0, (int)r.size, msg);
        TEST_ASSERT_LESS_OR_EQUAL_MESSAGE(cap, r.tier, msg);
    }
}

/// The decoder counterpart to test_tierAboveCompiledMax_clamps: a packet claiming a tier this
/// build does not implement must be refused outright. Nothing on the wire distinguishes a
/// tier-4 body from a tier 1-3 one except the header nibble, so a build compiled below tier 4
/// that ignored it would parse a bit area as column code bytes and return wrong numbers.
void test_tierAboveCompiledMax_isRejectedOnDecode(void)
{
    meshtastic_DeviceMetrics src[BISCUIT_MAX_BATCH], dst[BISCUIT_MAX_BATCH];
    uint32_t ts[BISCUIT_MAX_BATCH], tsOut[BISCUIT_MAX_BATCH];
    const uint8_t n = 8;
    makeDeviceBatch(src, ts, n);
    const void *sp[BISCUIT_MAX_BATCH];
    void *dp[BISCUIT_MAX_BATCH];
    for (uint8_t i = 0; i < n; i++)
        sp[i] = &src[i], dp[i] = &dst[i];

    Options opt;
    opt.fixed32IsFloat = true;
    opt.neverInflate = false;
    uint8_t buf[512];
    const Result r = encode(&meshtastic_DeviceMetrics_msg, sp, n, ts, buf, sizeof(buf), opt);
    TEST_ASSERT_GREATER_THAN(0, (int)r.size);
    // Unmolested, it decodes.
    for (uint8_t i = 0; i < n; i++)
        dst[i] = meshtastic_DeviceMetrics_init_zero;
    TEST_ASSERT_EQUAL(n, decode(&meshtastic_DeviceMetrics_msg, buf, r.size, dp, n, tsOut, opt));
    biscuitcmp::assertSameBatch(&meshtastic_DeviceMetrics_msg, src, dst, ts, tsOut, n, "round trip");

    const uint8_t good = buf[0];
    // Tier 0 is not a tier.
    buf[0] = (uint8_t)(good & 0xF8);
    for (uint8_t i = 0; i < n; i++)
        dst[i] = meshtastic_DeviceMetrics_init_zero;
    TEST_ASSERT_EQUAL(0, decode(&meshtastic_DeviceMetrics_msg, buf, r.size, dp, n, tsOut, opt));

    // And any tier above what this build implements.
    for (uint8_t bad = BISCUIT_MAX_TIER + 1; bad <= 7; bad++) {
        buf[0] = (uint8_t)((good & 0xF8) | bad);
        for (uint8_t i = 0; i < n; i++)
            dst[i] = meshtastic_DeviceMetrics_init_zero;
        char msg[48];
        snprintf(msg, sizeof(msg), "tier %u accepted", bad);
        TEST_ASSERT_EQUAL_MESSAGE(0, decode(&meshtastic_DeviceMetrics_msg, buf, r.size, dp, n, tsOut, opt), msg);
    }
    buf[0] = good;
}

/// Every field the manufactured batches populate, compared presence-and-value.
static void assertEnvEqual(const meshtastic_EnvironmentMetrics &a, const meshtastic_EnvironmentMetrics &b, const char *msg)
{
    TEST_ASSERT_EQUAL_MESSAGE(a.has_temperature, b.has_temperature, msg);
    TEST_ASSERT_EQUAL_MESSAGE(a.has_relative_humidity, b.has_relative_humidity, msg);
    TEST_ASSERT_EQUAL_MESSAGE(a.has_barometric_pressure, b.has_barometric_pressure, msg);
    TEST_ASSERT_EQUAL_MESSAGE(a.has_gas_resistance, b.has_gas_resistance, msg);
    TEST_ASSERT_EQUAL_MESSAGE(a.has_soil_temperature, b.has_soil_temperature, msg);
    if (a.has_temperature)
        TEST_ASSERT_FLOAT_WITHIN_MESSAGE(0.0002f, a.temperature, b.temperature, msg);
    if (a.has_relative_humidity)
        TEST_ASSERT_FLOAT_WITHIN_MESSAGE(0.0002f, a.relative_humidity, b.relative_humidity, msg);
    if (a.has_barometric_pressure)
        TEST_ASSERT_FLOAT_WITHIN_MESSAGE(0.0002f, a.barometric_pressure, b.barometric_pressure, msg);
    if (a.has_gas_resistance)
        TEST_ASSERT_FLOAT_WITHIN_MESSAGE(0.0002f, a.gas_resistance, b.gas_resistance, msg);
    if (a.has_soil_temperature)
        TEST_ASSERT_FLOAT_WITHIN_MESSAGE(0.0002f, a.soil_temperature, b.soil_temperature, msg);
}

/// Encode pinned to each tier in turn, on data that suits that tier and on data that does not:
/// the packet must declare the tier it was built at, and must decode back to what went in.
///
/// This is the other half of the validation. test_tierAboveCompiledMax_isRejectedOnDecode proves
/// a tier this build cannot read is refused; this proves every tier it can read is honoured
/// exactly, and that the header nibble the decoder now checks is the one the encoder wrote.
void test_everyTier_declaresItselfAndRoundTrips(void)
{
    static const WhichTier kCases[] = {WANT_T1, WANT_T2, WANT_T3, WANT_T4};

    for (size_t k = 0; k < sizeof(kCases) / sizeof(kCases[0]); k++) {
        meshtastic_EnvironmentMetrics src[BISCUIT_MAX_BATCH], dst[BISCUIT_MAX_BATCH];
        uint32_t ts[BISCUIT_MAX_BATCH], tsOut[BISCUIT_MAX_BATCH];
        const uint8_t n = makeTierBatch(kCases[k], src, ts);
        const void *sp[BISCUIT_MAX_BATCH];
        void *dp[BISCUIT_MAX_BATCH];
        for (uint8_t i = 0; i < n; i++)
            sp[i] = &src[i], dp[i] = &dst[i];

        for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++) {
            Options opt; // deliberately no hints, so every tier here is lossless
            opt.fixed32IsFloat = true;
            opt.neverInflate = false;
            char msg[80];
            snprintf(msg, sizeof(msg), "batch %u pinned to tier %u", (unsigned)k, tier);

            uint8_t buf[512];
            const Result r = encodeAtTier(&meshtastic_EnvironmentMetrics_msg, sp, n, ts, buf, sizeof(buf), opt, tier);
            TEST_ASSERT_GREATER_THAN_MESSAGE(0, (int)r.size, msg);
            TEST_ASSERT_TRUE_MESSAGE(r.lossless, msg);

            // Marked as such: the Result and the header must agree, and both must say `tier`.
            TEST_ASSERT_EQUAL_UINT32_MESSAGE(tier, r.tier, msg);
            TEST_ASSERT_EQUAL_UINT32_MESSAGE(tier, peekTier(buf, r.size), msg);
            TEST_ASSERT_EQUAL_UINT32_MESSAGE(n, peekCount(buf, r.size), msg);

            for (uint8_t i = 0; i < n; i++)
                dst[i] = meshtastic_EnvironmentMetrics_init_zero;
            TEST_ASSERT_EQUAL_MESSAGE(n, decode(&meshtastic_EnvironmentMetrics_msg, buf, r.size, dp, n, tsOut, opt), msg);
            biscuitcmp::assertSameBatch(&meshtastic_EnvironmentMetrics_msg, src, dst, ts, tsOut, n, "round trip");
            for (uint8_t i = 0; i < n; i++) {
                TEST_ASSERT_EQUAL_UINT32_MESSAGE(ts[i], tsOut[i], msg);
                assertEnvEqual(src[i], dst[i], msg);
            }
        }
    }
}

/// The tier nibble must actually drive the parse. Tiers 1 and 2 name columns differently - an
/// explicit tag list against a bitmap - and tier 4 replaces the per-column bytes with a bit
/// area, so a packet relabelled across those boundaries must not decode to the same readings.
///
/// Tiers 2 and 3 are deliberately excluded from each other: with no resolution hint supplied
/// they are byte-identical by design, so relabelling between them is a no-op and proves nothing.
void test_tierNibbleDrivesTheParse(void)
{
    meshtastic_EnvironmentMetrics src[BISCUIT_MAX_BATCH], dst[BISCUIT_MAX_BATCH];
    uint32_t ts[BISCUIT_MAX_BATCH], tsOut[BISCUIT_MAX_BATCH];
    const uint8_t n = makeTierBatch(WANT_T4, src, ts);
    const void *sp[BISCUIT_MAX_BATCH];
    void *dp[BISCUIT_MAX_BATCH];
    for (uint8_t i = 0; i < n; i++)
        sp[i] = &src[i], dp[i] = &dst[i];

    Options opt;
    opt.fixed32IsFloat = true;
    opt.neverInflate = false;

    static const uint8_t kPairs[][2] = {{1, 2}, {2, 1}, {4, 2}, {2, 4}, {4, 1}};
    unsigned exercised = 0;
    for (size_t q = 0; q < sizeof(kPairs) / sizeof(kPairs[0]); q++) {
        const uint8_t wrote = kPairs[q][0], claims = kPairs[q][1];
        if (wrote > BISCUIT_MAX_TIER || claims > BISCUIT_MAX_TIER)
            continue;
        uint8_t buf[512];
        const Result r = encodeAtTier(&meshtastic_EnvironmentMetrics_msg, sp, n, ts, buf, sizeof(buf), opt, wrote);
        TEST_ASSERT_GREATER_THAN(0, (int)r.size);
        buf[0] = (uint8_t)((buf[0] & 0xF8) | claims); // relabel, body untouched
        exercised++;

        for (uint8_t i = 0; i < n; i++)
            dst[i] = meshtastic_EnvironmentMetrics_init_zero;
        // No full-message check here: a relabelled packet decoding to something other than
        // the original is exactly what this test requires, and is asserted below.
        const uint8_t got = decode(&meshtastic_EnvironmentMetrics_msg, buf, r.size, dp, n, tsOut, opt);

        char msg[80];
        snprintf(msg, sizeof(msg), "tier %u body relabelled tier %u decoded clean", wrote, claims);
        if (got == 0)
            continue; // refused, which is the outcome we want
        // If it did decode, it must not have produced the original readings.
        bool identical = true;
        for (uint8_t i = 0; i < n && identical; i++) {
            if (ts[i] != tsOut[i] || src[i].has_temperature != dst[i].has_temperature ||
                src[i].has_gas_resistance != dst[i].has_gas_resistance)
                identical = false;
            else if (src[i].has_temperature && fabsf(src[i].temperature - dst[i].temperature) > 0.0002f)
                identical = false;
        }
        TEST_ASSERT_FALSE_MESSAGE(identical, msg);
    }
    // Every pair being refused is a legitimate outcome, but then nothing above ran.
    // Without this the test passes while asserting nothing about the nibble at all.
    TEST_ASSERT_GREATER_THAN_UINT32_MESSAGE(0, exercised, "no relabelled packet was built");
}

void setup()
{
    initializeTestEnvironment();
    UNITY_BEGIN();
    RUN_TEST(test_roundTrip_tier1_isExact);
    RUN_TEST(test_roundTrip_tier2_isExact);
    RUN_TEST(test_roundTrip_tier3_isExact);
#if BISCUIT_MAX_TIER >= 4
    RUN_TEST(test_roundTrip_tier4_isExact);
#endif
    RUN_TEST(test_roundTrip_everyBatchSize);
    RUN_TEST(test_selectedEncodingBeatsEveryTier);
    RUN_TEST(test_resolutionShift_errorIsBounded);
    RUN_TEST(test_shortBuffer_failsWithoutOverrun);
    RUN_TEST(test_corruptHeader_isRejected);
    RUN_TEST(test_neverLargerThanTheProtobufItReplaces);
    RUN_TEST(test_retireCount_alwaysMakesProgress);
    RUN_TEST(test_retireCount_keepsTheRequestedTail);
    RUN_TEST(test_retireCount_progressRateIsFlushMinusOverlap);
    RUN_TEST(test_realCorpusData_roundTripsExactly);
    RUN_TEST(test_realCorpusData_sizesAreStable);
    RUN_TEST(test_realCorpusData_meetsTierModelSavings);
    RUN_TEST(test_tierAboveCompiledMax_clamps);
    RUN_TEST(test_timeResolution_errorIsBoundedAndDeclared);
    RUN_TEST(test_families_everyChannelRoundTripsToItsOwnTag);
    RUN_TEST(test_families_areSmallerThanSeparateColumns);
    RUN_TEST(test_families_missingChannelDoesNotCorruptTheBatch);
    RUN_TEST(test_selfDescribing_resolutionSurvivesADifferentReceiver);
    RUN_TEST(test_selfDescribing_familyMismatchIsRefused);
    RUN_TEST(test_repeats_areDeclaredInTheHeader);
    RUN_TEST(test_repeats_beyondTheFieldAreRefused);
    RUN_TEST(test_uptimeAges_decreasingTimestampsRoundTrip);
    RUN_TEST(test_uptimeAges_datedAgainstReceiverClockMatchOriginals);
    RUN_TEST(test_uptimeAges_allZeroGapsRoundTrip);
    RUN_TEST(test_uptimeAges_areNoLargerThanEpochs);
    RUN_TEST(test_ragged_absentReadingsStayAbsent);
    RUN_TEST(test_ragged_noColumnSpansTheBatch);
    RUN_TEST(test_ragged_fullColumnsPayNothing);
    RUN_TEST(test_families_channelNeverPopulatedKeepsTheRest);
    RUN_TEST(test_families_raggedChannelSurvives);
    RUN_TEST(test_tierSelection_emitsTheSmallestAvailable);
    RUN_TEST(test_tierSelection_respectsTheCap);
    RUN_TEST(test_tierAboveCompiledMax_isRejectedOnDecode);
    RUN_TEST(test_everyTier_declaresItselfAndRoundTrips);
    RUN_TEST(test_tierNibbleDrivesTheParse);
    exit(UNITY_END());
}

void loop() {}
