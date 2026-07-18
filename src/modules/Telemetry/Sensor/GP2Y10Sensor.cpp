#include "configuration.h"

#if HAS_TELEMETRY && !MESHTASTIC_EXCLUDE_AIR_QUALITY_SENSOR && defined(GP2Y10_SENSOR_EN)

#include "../mesh/generated/meshtastic/telemetry.pb.h"
#include "GP2Y10Sensor.h"
#include "TelemetrySensor.h"
#if defined(ARCH_ESP32) && defined(BATTERY_PIN)
#include "Power.h" // espSharedAdcReadMilliVolts(): shares the battery ADC unit (see below)
#endif

// Datasheet timing for the GP2Y1014AU read cycle (~10ms total per sample)
#ifndef GP2Y10_SAMPLES
#define GP2Y10_SAMPLES 15 // mirror T1000X_SENSE_SAMPLES
#endif
#define GP2Y10_SAMPLING_US 280   // LED-on settle before ADC sample
#define GP2Y10_POST_SAMPLE_US 40 // hold after sample before LED off
#define GP2Y10_LED_OFF_US 9680   // idle time to complete the ~10ms cycle

// Linear conversion: dust(mg/m3) = slope * Vout - offset (datasheet values, tunable per variant)
#ifndef GP2Y10_CAL_SLOPE
#define GP2Y10_CAL_SLOPE 0.17f // mg/m3 per volt
#endif
#ifndef GP2Y10_CAL_OFFSET
#define GP2Y10_CAL_OFFSET 0.10f // mg/m3 zero-dust offset
#endif

GP2Y10Sensor::GP2Y10Sensor() : TelemetrySensor(meshtastic_TelemetrySensorType_SENSOR_UNSET, "GP2Y10") {}

bool GP2Y10Sensor::initDevice(TwoWire *bus, ScanI2C::FoundDevice *dev)
{
    LOG_INFO("Init sensor: %s", sensorName);
    pinMode(GP2Y10_LED_PIN, OUTPUT);
    digitalWrite(GP2Y10_LED_PIN, HIGH); // LED off at rest (LED drive is active-low)
    // The OUT pin is configured lazily on first read (via the shared ADC helper on ESP32),
    // so no analog pinMode/attenuation setup is needed here.
    return true;
}

float GP2Y10Sensor::readDustDensityMgM3()
{
    float mv_accum = 0.0f;

    for (uint32_t i = 0; i < GP2Y10_SAMPLES; i++) {
        digitalWrite(GP2Y10_LED_PIN, LOW); // LED on
        delayMicroseconds(GP2Y10_SAMPLING_US);
#if defined(ARCH_ESP32) && defined(BATTERY_PIN)
        // Must share the battery ADC unit; a second Arduino ADC owner would fail and read 0.
        int32_t mv = espSharedAdcReadMilliVolts(GP2Y10_OUT_PIN);
        if (mv < 0)
            mv = 0;
#elif defined(ARCH_ESP32)
        uint32_t mv = analogReadMilliVolts(GP2Y10_OUT_PIN);
#else
        uint32_t raw = analogRead(GP2Y10_OUT_PIN);
        uint32_t mv = ((1000UL * AREF_VOLTAGE) / (uint32_t)pow(2, BATTERY_SENSE_RESOLUTION_BITS)) * raw;
#endif
        delayMicroseconds(GP2Y10_POST_SAMPLE_US);
        digitalWrite(GP2Y10_LED_PIN, HIGH); // LED off
        delayMicroseconds(GP2Y10_LED_OFF_US);
        mv_accum += (float)mv;
    }

    float calcVoltage = (mv_accum / GP2Y10_SAMPLES) / 1000.0f; // volts
    float dust = GP2Y10_CAL_SLOPE * calcVoltage - GP2Y10_CAL_OFFSET;
    if (dust < 0.0f)
        dust = 0.0f; // clamp negatives (below the zero-dust baseline)
    return dust;
}

bool GP2Y10Sensor::getMetrics(meshtastic_Telemetry *measurement)
{
    uint32_t dust_ug = (uint32_t)(readDustDensityMgM3() * 1000.0f + 0.5f); // mg/m3 -> ug/m3, rounded

    measurement->variant.air_quality_metrics.has_pm25_standard = true;
    measurement->variant.air_quality_metrics.pm25_standard = dust_ug;

    LOG_DEBUG("%s: pm25_standard=%u ug/m3", sensorName, dust_ug);
    return true;
}

#endif
