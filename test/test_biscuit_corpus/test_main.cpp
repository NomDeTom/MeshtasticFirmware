// Biscuit against captured traffic: 40 environment series and every air-quality series seen
// in 21 hours on the public MQTT broker, run through the codec at every tier.
//
// test_biscuit pins one hand-copied device-metrics node. This suite is the breadth case - real
// field sets, real reporting intervals, real gaps and real garbage - and it exists because the
// analysis behind the tier model was measured on device metrics and assumed to carry over.
#include "Arduino.h"
#include "BiscuitCompare.h"
#include "TestUtil.h"
#include "corpus_data.h"
#include "mesh/biscuit/Biscuit.h"
#include "mesh/generated/meshtastic/telemetry.pb.h"
#include <math.h>
#include <stdlib.h>
#include <unity.h>

using namespace biscuit;

/// Two errors, not one. Rounding to the scale costs half a unit of it; the encoder then does
/// that multiply in float32, so above value*scale = 2^24 the product loses its low bits and the
/// error becomes proportional to the value instead. A gas_resistance of 4079 lands 0.00025 out.
static float floatTol(float want)
{
    const float rel = fabsf(want) * 1.2e-7f; // 2^-23, one bit of headroom over float32's 2^-24
    return rel > 0.0002f ? rel : 0.0002f;
}

static void setEnv(meshtastic_EnvironmentMetrics *m, uint8_t tag, float v)
{
    switch (tag) {
    case 1:
        m->has_temperature = true, m->temperature = v;
        break;
    case 2:
        m->has_relative_humidity = true, m->relative_humidity = v;
        break;
    case 3:
        m->has_barometric_pressure = true, m->barometric_pressure = v;
        break;
    case 4:
        m->has_gas_resistance = true, m->gas_resistance = v;
        break;
    case 5:
        m->has_voltage = true, m->voltage = v;
        break;
    case 6:
        m->has_current = true, m->current = v;
        break;
    case 7:
        m->has_iaq = true, m->iaq = (uint32_t)v;
        break;
    case 8:
        m->has_distance = true, m->distance = v;
        break;
    case 9:
        m->has_lux = true, m->lux = v;
        break;
    case 10:
        m->has_white_lux = true, m->white_lux = v;
        break;
    case 11:
        m->has_ir_lux = true, m->ir_lux = v;
        break;
    case 12:
        m->has_uv_lux = true, m->uv_lux = v;
        break;
    case 13:
        m->has_wind_direction = true, m->wind_direction = (uint32_t)v;
        break;
    case 14:
        m->has_wind_speed = true, m->wind_speed = v;
        break;
    case 15:
        m->has_weight = true, m->weight = v;
        break;
    case 16:
        m->has_wind_gust = true, m->wind_gust = v;
        break;
    case 17:
        m->has_wind_lull = true, m->wind_lull = v;
        break;
    case 18:
        m->has_radiation = true, m->radiation = v;
        break;
    case 19:
        m->has_rainfall_1h = true, m->rainfall_1h = v;
        break;
    case 20:
        m->has_rainfall_24h = true, m->rainfall_24h = v;
        break;
    case 21:
        m->has_soil_moisture = true, m->soil_moisture = (uint32_t)v;
        break;
    case 22:
        m->has_soil_temperature = true, m->soil_temperature = v;
        break;
    default:
        TEST_FAIL_MESSAGE("fixture carries an EnvironmentMetrics tag the setter does not know");
    }
}

static float getEnv(const meshtastic_EnvironmentMetrics *m, uint8_t tag)
{
    switch (tag) {
    case 1:
        return m->has_temperature ? m->temperature : NAN;
    case 2:
        return m->has_relative_humidity ? m->relative_humidity : NAN;
    case 3:
        return m->has_barometric_pressure ? m->barometric_pressure : NAN;
    case 4:
        return m->has_gas_resistance ? m->gas_resistance : NAN;
    case 5:
        return m->has_voltage ? m->voltage : NAN;
    case 6:
        return m->has_current ? m->current : NAN;
    case 7:
        return m->has_iaq ? (float)m->iaq : NAN;
    case 8:
        return m->has_distance ? m->distance : NAN;
    case 9:
        return m->has_lux ? m->lux : NAN;
    case 10:
        return m->has_white_lux ? m->white_lux : NAN;
    case 11:
        return m->has_ir_lux ? m->ir_lux : NAN;
    case 12:
        return m->has_uv_lux ? m->uv_lux : NAN;
    case 13:
        return m->has_wind_direction ? (float)m->wind_direction : NAN;
    case 14:
        return m->has_wind_speed ? m->wind_speed : NAN;
    case 15:
        return m->has_weight ? m->weight : NAN;
    case 16:
        return m->has_wind_gust ? m->wind_gust : NAN;
    case 17:
        return m->has_wind_lull ? m->wind_lull : NAN;
    case 18:
        return m->has_radiation ? m->radiation : NAN;
    case 19:
        return m->has_rainfall_1h ? m->rainfall_1h : NAN;
    case 20:
        return m->has_rainfall_24h ? m->rainfall_24h : NAN;
    case 21:
        return m->has_soil_moisture ? (float)m->soil_moisture : NAN;
    case 22:
        return m->has_soil_temperature ? m->soil_temperature : NAN;
    }
    return NAN;
}

static void setAq(meshtastic_AirQualityMetrics *m, uint8_t tag, uint32_t v)
{
    switch (tag) {
    case 1:
        m->has_pm10_standard = true, m->pm10_standard = v;
        break;
    case 2:
        m->has_pm25_standard = true, m->pm25_standard = v;
        break;
    case 3:
        m->has_pm100_standard = true, m->pm100_standard = v;
        break;
    case 4:
        m->has_pm10_environmental = true, m->pm10_environmental = v;
        break;
    case 5:
        m->has_pm25_environmental = true, m->pm25_environmental = v;
        break;
    case 6:
        m->has_pm100_environmental = true, m->pm100_environmental = v;
        break;
    case 7:
        m->has_particles_03um = true, m->particles_03um = v;
        break;
    case 8:
        m->has_particles_05um = true, m->particles_05um = v;
        break;
    case 9:
        m->has_particles_10um = true, m->particles_10um = v;
        break;
    case 10:
        m->has_particles_25um = true, m->particles_25um = v;
        break;
    case 11:
        m->has_particles_50um = true, m->particles_50um = v;
        break;
    case 12:
        m->has_particles_100um = true, m->particles_100um = v;
        break;
    case 13:
        m->has_co2 = true, m->co2 = v;
        break;
    default:
        TEST_FAIL_MESSAGE("fixture carries an AirQualityMetrics tag the setter does not know");
    }
}

static uint32_t getAq(const meshtastic_AirQualityMetrics *m, uint8_t tag, bool *has)
{
    switch (tag) {
    case 1:
        return *has = m->has_pm10_standard, m->pm10_standard;
    case 2:
        return *has = m->has_pm25_standard, m->pm25_standard;
    case 3:
        return *has = m->has_pm100_standard, m->pm100_standard;
    case 4:
        return *has = m->has_pm10_environmental, m->pm10_environmental;
    case 5:
        return *has = m->has_pm25_environmental, m->pm25_environmental;
    case 6:
        return *has = m->has_pm100_environmental, m->pm100_environmental;
    case 7:
        return *has = m->has_particles_03um, m->particles_03um;
    case 8:
        return *has = m->has_particles_05um, m->particles_05um;
    case 9:
        return *has = m->has_particles_10um, m->particles_10um;
    case 10:
        return *has = m->has_particles_25um, m->particles_25um;
    case 11:
        return *has = m->has_particles_50um, m->particles_50um;
    case 12:
        return *has = m->has_particles_100um, m->particles_100um;
    case 13:
        return *has = m->has_co2, m->co2;
    }
    return *has = false, 0u;
}

static uint8_t buildEnvWindow(const EnvCorpusEntry &e, uint8_t from, uint8_t count, meshtastic_EnvironmentMetrics *m,
                              uint32_t *ts)
{
    if (from + count > e.s.n)
        count = (uint8_t)(e.s.n - from);
    if (count > BISCUIT_MAX_BATCH)
        count = BISCUIT_MAX_BATCH;
    for (uint8_t i = 0; i < count; i++) {
        const uint8_t r = (uint8_t)(from + i);
        m[i] = meshtastic_EnvironmentMetrics_init_zero;
        ts[i] = e.s.ts[r];
        for (uint8_t j = 0; j < e.s.fieldCount; j++)
            if (e.s.present[r] & (1u << j))
                setEnv(&m[i], e.s.tags[j], e.v[r * e.s.fieldCount + j]);
    }
    return count;
}

static uint8_t buildEnv(const EnvCorpusEntry &e, meshtastic_EnvironmentMetrics *m, uint32_t *ts)
{
    return buildEnvWindow(e, 0, e.s.n, m, ts);
}

static uint8_t buildAq(const AqCorpusEntry &e, meshtastic_AirQualityMetrics *m, uint32_t *ts)
{
    const uint8_t n = e.s.n < BISCUIT_MAX_BATCH ? e.s.n : BISCUIT_MAX_BATCH;
    for (uint8_t i = 0; i < n; i++) {
        m[i] = meshtastic_AirQualityMetrics_init_zero;
        ts[i] = e.s.ts[i];
        for (uint8_t j = 0; j < e.s.fieldCount; j++)
            if (e.s.present[i] & (1u << j))
                setAq(&m[i], e.s.tags[j], e.v[i * e.s.fieldCount + j]);
    }
    return n;
}

/// Presence must survive as well as value. The codec admits a field only when every reading
/// carries it, so a ragged series either loses that column or is refused outright.
void test_capturedEnvironment_roundTripsExactly(void)
{
    for (size_t k = 0; k < sizeof(kEnvCorpus) / sizeof(kEnvCorpus[0]); k++) {
        const EnvCorpusEntry &e = kEnvCorpus[k];
        meshtastic_EnvironmentMetrics src[BISCUIT_MAX_BATCH], dst[BISCUIT_MAX_BATCH];
        uint32_t ts[BISCUIT_MAX_BATCH], tsOut[BISCUIT_MAX_BATCH];
        const uint8_t n = buildEnv(e, src, ts);

        const void *sp[BISCUIT_MAX_BATCH];
        void *dp[BISCUIT_MAX_BATCH];
        for (uint8_t i = 0; i < n; i++)
            sp[i] = &src[i], dp[i] = &dst[i];

        Options opt;
        opt.fixed32IsFloat = true;
        opt.neverInflate = false; // fidelity is under test here, not size

        for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++) {
            opt.maxTier = tier;
            char msg[128];
            snprintf(msg, sizeof(msg), "series %u [%s] tier %u", (unsigned)k, e.s.fields, tier);
            uint8_t buf[1024];
            Result r = encodeAtTier(&meshtastic_EnvironmentMetrics_msg, sp, n, ts, buf, sizeof(buf), opt, tier);
            TEST_ASSERT_GREATER_THAN_MESSAGE(0, (int)r.size, msg);
            TEST_ASSERT_TRUE_MESSAGE(r.lossless, msg);

            for (uint8_t i = 0; i < n; i++)
                dst[i] = meshtastic_EnvironmentMetrics_init_zero;
            TEST_ASSERT_EQUAL_MESSAGE(n, decode(&meshtastic_EnvironmentMetrics_msg, buf, r.size, dp, n, tsOut, opt), msg);
            biscuitcmp::assertSameBatch(&meshtastic_EnvironmentMetrics_msg, src, dst, ts, tsOut, n, msg);

            for (uint8_t i = 0; i < n; i++) {
                TEST_ASSERT_EQUAL_UINT32_MESSAGE(ts[i], tsOut[i], msg);
                for (uint8_t j = 0; j < e.s.fieldCount; j++) {
                    const float want = getEnv(&src[i], e.s.tags[j]), got = getEnv(&dst[i], e.s.tags[j]);
                    TEST_ASSERT_EQUAL_MESSAGE(isnan(want), isnan(got), msg);
                    if (!isnan(want))
                        TEST_ASSERT_FLOAT_WITHIN_MESSAGE(floatTol(want), want, got, msg);
                }
            }
        }
    }
}

void test_capturedAirQuality_roundTripsExactly(void)
{
    for (size_t k = 0; k < sizeof(kAqCorpus) / sizeof(kAqCorpus[0]); k++) {
        const AqCorpusEntry &e = kAqCorpus[k];
        meshtastic_AirQualityMetrics src[BISCUIT_MAX_BATCH], dst[BISCUIT_MAX_BATCH];
        uint32_t ts[BISCUIT_MAX_BATCH], tsOut[BISCUIT_MAX_BATCH];
        const uint8_t n = buildAq(e, src, ts);

        const void *sp[BISCUIT_MAX_BATCH];
        void *dp[BISCUIT_MAX_BATCH];
        for (uint8_t i = 0; i < n; i++)
            sp[i] = &src[i], dp[i] = &dst[i];

        Options opt;
        opt.fixed32IsFloat = true;
        opt.neverInflate = false;

        for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++) {
            opt.maxTier = tier;
            char msg[128];
            snprintf(msg, sizeof(msg), "aq series %u tier %u", (unsigned)k, tier);
            uint8_t buf[1024];
            Result r = encodeAtTier(&meshtastic_AirQualityMetrics_msg, sp, n, ts, buf, sizeof(buf), opt, tier);
            TEST_ASSERT_GREATER_THAN_MESSAGE(0, (int)r.size, msg);

            for (uint8_t i = 0; i < n; i++)
                dst[i] = meshtastic_AirQualityMetrics_init_zero;
            TEST_ASSERT_EQUAL_MESSAGE(n, decode(&meshtastic_AirQualityMetrics_msg, buf, r.size, dp, n, tsOut, opt), msg);
            biscuitcmp::assertSameBatch(&meshtastic_AirQualityMetrics_msg, src, dst, ts, tsOut, n, msg);

            for (uint8_t i = 0; i < n; i++) {
                TEST_ASSERT_EQUAL_UINT32_MESSAGE(ts[i], tsOut[i], msg);
                for (uint8_t j = 0; j < e.s.fieldCount; j++) {
                    bool hw = false, hg = false;
                    const uint32_t want = getAq(&src[i], e.s.tags[j], &hw), got = getAq(&dst[i], e.s.tags[j], &hg);
                    TEST_ASSERT_EQUAL_MESSAGE(hw, hg, msg);
                    if (hw)
                        TEST_ASSERT_EQUAL_UINT32_MESSAGE(want, got, msg);
                }
            }
        }
    }
}

/// Totals over the whole corpus, printed rather than merely asserted: a per-series ratio is
/// noise, and the number that decides whether the codec is worth its flash is the aggregate.
void test_capturedCorpus_compressionByTier(void)
{
    size_t baseTotal = 0, tierTotal[BISCUIT_MAX_TIER + 1] = {0};
    unsigned refused[BISCUIT_MAX_TIER + 1] = {0}, series = 0, readings = 0;

    for (size_t k = 0; k < sizeof(kEnvCorpus) / sizeof(kEnvCorpus[0]); k++) {
        const EnvCorpusEntry &e = kEnvCorpus[k];
        meshtastic_EnvironmentMetrics src[BISCUIT_MAX_BATCH];
        uint32_t ts[BISCUIT_MAX_BATCH];
        const uint8_t n = buildEnv(e, src, ts);
        const void *sp[BISCUIT_MAX_BATCH];
        for (uint8_t i = 0; i < n; i++)
            sp[i] = &src[i];

        Options opt;
        opt.fixed32IsFloat = true;
        series++;
        readings += n;
        for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++) {
            opt.maxTier = tier;
            uint8_t buf[1024];
            Result r = encodeAtTier(&meshtastic_EnvironmentMetrics_msg, sp, n, ts, buf, sizeof(buf), opt, tier);
            if (tier == TIER_COLUMNAR)
                baseTotal += r.baseline;
            // A refusal means the caller sends the plain protobuf, so that is what it costs.
            tierTotal[tier] += r.size ? r.size : r.baseline;
            if (!r.size)
                refused[tier]++;
        }
    }

    printf("\n  captured environment corpus: %u series, %u readings\n", series, readings);
    printf("  %-28s%10s%9s%9s\n", "encoding", "bytes", "vs pb", "refused");
    printf("  %-28s%10u%9s%9s\n", "protobuf, one per reading", (unsigned)baseTotal, "-", "-");
    for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++) {
        char label[32];
        snprintf(label, sizeof(label), "biscuit tier %u", tier);
        printf("  %-28s%10u%8.0f%%%9u\n", label, (unsigned)tierTotal[tier],
               100.0 * (1.0 - (double)tierTotal[tier] / (double)baseTotal), refused[tier]);
    }

    // Weak on purpose. The point of this run is the printed table; pinning a percentage
    // before it has been read once would only record whatever the codec happens to do.
    for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++)
        TEST_ASSERT_LESS_THAN_UINT32(baseTotal, tierTotal[tier]);
    TEST_ASSERT_LESS_OR_EQUAL_UINT32(tierTotal[TIER_COLUMNAR], tierTotal[BISCUIT_MAX_TIER]);
}

static int byValue(const void *a, const void *b)
{
    const uint16_t x = *(const uint16_t *)a, y = *(const uint16_t *)b;
    return x < y ? -1 : (x > y ? 1 : 0);
}

/// Saving against batch size, as a distribution rather than a mean. What a node saves depends
/// on its field set, and when the question is "will this fit in one packet" the spread decides
/// it, not the average. Every window of every series contributes one sample.
void test_capturedCorpus_savingByBatchSize(void)
{
    static const uint8_t kN[] = {2, 3, 4, 6, 8, 12, 16};
    static uint16_t pct[4096], wire[4096], base[4096];
    static uint16_t perTier[BISCUIT_MAX_TIER + 1][4096];
    char byteRow[sizeof(kN) / sizeof(kN[0])][88], loseRow[sizeof(kN) / sizeof(kN[0])][88];

    // Each column is that tier alone, via encodeAtTier - encode() would report the best tier
    // at or below the cap, which is a different and cumulative question.
    printf("\n  saving against one protobuf per reading, p10 / median / p90 (per cent smaller)\n");
    printf("  %4s%8s", "N", "cases");
    for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++)
        printf("%18s", tier == 1 ? "tier 1" : (tier == 2 ? "tier 2" : (tier == 3 ? "tier 3" : "tier 4")));
    printf("\n");

    for (size_t q = 0; q < sizeof(kN) / sizeof(kN[0]); q++) {
        const uint8_t N = kN[q];
        char cell[BISCUIT_MAX_TIER + 1][24];
        uint16_t medWire[BISCUIT_MAX_TIER + 1] = {0}, medBase = 0;
        unsigned cases = 0, declined[BISCUIT_MAX_TIER + 1] = {0};
        for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++) {
            size_t got = 0;
            for (size_t k = 0; k < sizeof(kEnvCorpus) / sizeof(kEnvCorpus[0]); k++) {
                const EnvCorpusEntry &e = kEnvCorpus[k];
                if (e.s.n < N)
                    continue;
                for (uint8_t from = 0; from + N <= e.s.n && got < sizeof(pct) / sizeof(pct[0]); from++) {
                    meshtastic_EnvironmentMetrics src[BISCUIT_MAX_BATCH];
                    uint32_t ts[BISCUIT_MAX_BATCH];
                    const uint8_t n = buildEnvWindow(e, from, N, src, ts);
                    const void *sp[BISCUIT_MAX_BATCH];
                    for (uint8_t i = 0; i < n; i++)
                        sp[i] = &src[i];
                    Options opt;
                    opt.fixed32IsFloat = true;
                    opt.maxTier = tier;
                    uint8_t buf[1024];
                    Result r = encodeAtTier(&meshtastic_EnvironmentMetrics_msg, sp, n, ts, buf, sizeof(buf), opt, tier);
                    if (!r.baseline)
                        continue;
                    // A refusal costs the plain protobuf, which is a saving of nothing. Counted,
                    // because a percentile hides how often the codec declines at small N.
                    if (!r.size)
                        declined[tier]++;
                    const size_t sent = r.size ? r.size : r.baseline;
                    const double save = 100.0 * (1.0 - (double)sent / (double)r.baseline);
                    wire[got] = (uint16_t)sent;
                    perTier[tier][got] = (uint16_t)sent;
                    base[got] = (uint16_t)r.baseline;
                    pct[got++] = (uint16_t)(save < 0 ? 0 : save);
                }
            }
            cases = (unsigned)got;
            if (got) {
                qsort(wire, got, sizeof(wire[0]), byValue);
                qsort(base, got, sizeof(base[0]), byValue);
                medWire[tier] = wire[got / 2];
                medBase = base[got / 2];
                qsort(pct, got, sizeof(pct[0]), byValue);
                snprintf(cell[tier], sizeof(cell[tier]), "%u / %u / %u", pct[got / 10], pct[got / 2], pct[got - 1 - got / 10]);
            } else {
                medWire[tier] = 0;
                snprintf(cell[tier], sizeof(cell[tier]), "-");
            }
        }
        printf("  %4u%8u", N, cases);
        for (uint8_t tier = TIER_COLUMNAR; tier <= BISCUIT_MAX_TIER; tier++)
            printf("%18s", cell[tier]);
        printf("\n");
        // Median payload bytes too, so an on-air figure is measured rather than derived from a
        // percentage. On air adds 6 B of Data framing and the 16 B Meshtastic header, paid once.
        snprintf(byteRow[q], sizeof(byteRow[q]), "  %4u%10u%10u%10u%10u%10u%9u%9u", N, medBase, medWire[1], medWire[2],
                 medWire[3], BISCUIT_MAX_TIER >= 4 ? medWire[4] : 0, declined[1], BISCUIT_MAX_TIER >= 4 ? declined[4] : 0);
        // Does the top tier ever lose to a cheaper one on the same batch? The encoder emits at
        // the configured tier without comparing, so where it does the node sends the larger packet.
        unsigned worse = 0, excess = 0;
        for (unsigned i = 0; i < cases; i++)
            if (perTier[BISCUIT_MAX_TIER][i] > perTier[TIER_BITMAP][i]) {
                worse++;
                excess += perTier[BISCUIT_MAX_TIER][i] - perTier[TIER_BITMAP][i];
            }
        snprintf(loseRow[q], sizeof(loseRow[q]), "  %4u%10u%12u%14.1f", N, cases, worse, worse ? (double)excess / worse : 0.0);
        TEST_ASSERT_GREATER_THAN_UINT32(0, cases);
    }

    printf("\n  batches where tier %u is LARGER than tier 2\n", BISCUIT_MAX_TIER);
    printf("  %4s%10s%12s%14s\n", "N", "cases", "T4 > T2", "mean excess B");
    for (size_t q = 0; q < sizeof(kN) / sizeof(kN[0]); q++)
        printf("%s\n", loseRow[q]);

    printf("\n  median payload bytes for the whole batch\n");
    printf("  %4s%10s%10s%10s%10s%10s%9s%9s\n", "N", "protobuf", "tier 1", "tier 2", "tier 3", "tier 4", "T1 decl", "T4 decl");
    for (size_t q = 0; q < sizeof(kN) / sizeof(kN[0]); q++)
        printf("%s\n", byteRow[q]);
}

void setup()
{
    initializeTestEnvironment();
    UNITY_BEGIN();
    RUN_TEST(test_capturedEnvironment_roundTripsExactly);
    RUN_TEST(test_capturedAirQuality_roundTripsExactly);
    RUN_TEST(test_capturedCorpus_compressionByTier);
    RUN_TEST(test_capturedCorpus_savingByBatchSize);
    exit(UNITY_END());
}

void loop() {}
