// Biscuit against captured traffic: 40 environment series and every air-quality series seen
// in 21 hours on the public MQTT broker, run through the codec at every tier.
//
// test_biscuit pins one hand-copied device-metrics node. This suite is the breadth case - real
// field sets, real reporting intervals, real gaps and real garbage - and it exists because the
// analysis behind the tier model was measured on device metrics and assumed to carry over.
#include "Arduino.h"
#include "TestUtil.h"
#include "corpus_data.h"
#include "mesh/biscuit/Biscuit.h"
#include "mesh/generated/meshtastic/telemetry.pb.h"
#include <math.h>
#include <unity.h>

using namespace biscuit;

/// Quantisation floor: every float is carried as value * floatScale rounded to an integer,
/// so half a unit of that scale is the most a lossless round trip can move a reading.
static const float kFloatTol = 0.0002f;

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
    case 9:
        m->has_lux = true, m->lux = v;
        break;
    case 13:
        m->has_wind_direction = true, m->wind_direction = (uint32_t)v;
        break;
    case 14:
        m->has_wind_speed = true, m->wind_speed = v;
        break;
    case 16:
        m->has_wind_gust = true, m->wind_gust = v;
        break;
    case 18:
        m->has_radiation = true, m->radiation = v;
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
    case 9:
        return m->has_lux ? m->lux : NAN;
    case 13:
        return m->has_wind_direction ? (float)m->wind_direction : NAN;
    case 14:
        return m->has_wind_speed ? m->wind_speed : NAN;
    case 16:
        return m->has_wind_gust ? m->wind_gust : NAN;
    case 18:
        return m->has_radiation ? m->radiation : NAN;
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

static uint8_t buildEnv(const EnvCorpusEntry &e, meshtastic_EnvironmentMetrics *m, uint32_t *ts)
{
    const uint8_t n = e.s.n < BISCUIT_MAX_BATCH ? e.s.n : BISCUIT_MAX_BATCH;
    for (uint8_t i = 0; i < n; i++) {
        m[i] = meshtastic_EnvironmentMetrics_init_zero;
        ts[i] = e.s.ts[i];
        for (uint8_t j = 0; j < e.s.fieldCount; j++)
            if (e.s.present[i] & (1u << j))
                setEnv(&m[i], e.s.tags[j], e.v[i * e.s.fieldCount + j]);
    }
    return n;
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
            Result r = encode(&meshtastic_EnvironmentMetrics_msg, sp, n, ts, buf, sizeof(buf), opt);
            TEST_ASSERT_GREATER_THAN_MESSAGE(0, (int)r.size, msg);
            TEST_ASSERT_TRUE_MESSAGE(r.lossless, msg);

            for (uint8_t i = 0; i < n; i++)
                dst[i] = meshtastic_EnvironmentMetrics_init_zero;
            TEST_ASSERT_EQUAL_MESSAGE(n, decode(&meshtastic_EnvironmentMetrics_msg, buf, r.size, dp, n, tsOut, opt), msg);

            for (uint8_t i = 0; i < n; i++) {
                TEST_ASSERT_EQUAL_UINT32_MESSAGE(ts[i], tsOut[i], msg);
                for (uint8_t j = 0; j < e.s.fieldCount; j++) {
                    const float want = getEnv(&src[i], e.s.tags[j]), got = getEnv(&dst[i], e.s.tags[j]);
                    TEST_ASSERT_EQUAL_MESSAGE(isnan(want), isnan(got), msg);
                    if (!isnan(want))
                        TEST_ASSERT_FLOAT_WITHIN_MESSAGE(kFloatTol, want, got, msg);
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
            Result r = encode(&meshtastic_AirQualityMetrics_msg, sp, n, ts, buf, sizeof(buf), opt);
            TEST_ASSERT_GREATER_THAN_MESSAGE(0, (int)r.size, msg);

            for (uint8_t i = 0; i < n; i++)
                dst[i] = meshtastic_AirQualityMetrics_init_zero;
            TEST_ASSERT_EQUAL_MESSAGE(n, decode(&meshtastic_AirQualityMetrics_msg, buf, r.size, dp, n, tsOut, opt), msg);

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
            Result r = encode(&meshtastic_EnvironmentMetrics_msg, sp, n, ts, buf, sizeof(buf), opt);
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

void setup()
{
    initializeTestEnvironment();
    UNITY_BEGIN();
    RUN_TEST(test_capturedEnvironment_roundTripsExactly);
    RUN_TEST(test_capturedAirQuality_roundTripsExactly);
    RUN_TEST(test_capturedCorpus_compressionByTier);
    exit(UNITY_END());
}

void loop() {}
