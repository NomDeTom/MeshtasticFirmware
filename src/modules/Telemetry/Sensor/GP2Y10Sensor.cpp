#include "configuration.h"

#if HAS_TELEMETRY && !MESHTASTIC_EXCLUDE_AIR_QUALITY_SENSOR && defined(GP2Y10_SENSOR_EN)

#include "../mesh/generated/meshtastic/telemetry.pb.h"
#include "GP2Y10Sensor.h"
#include "TelemetrySensor.h"

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
    // Prime the ADC channel first: analogSetPinAttenuation() errors ("Pin is not configured
    // as analog channel") if the pin has never been read, since the channel is configured lazily.
    analogRead(GP2Y10_OUT_PIN);
    analogSetPinAttenuation(GP2Y10_OUT_PIN, ADC_11db); // cover the sensor's full ~0-3.3V swing
    return true;
}

float GP2Y10Sensor::readDustDensityMgM3()
{
    float mv_accum = 0.0f;

    for (uint32_t i = 0; i < GP2Y10_SAMPLES; i++) {
        digitalWrite(GP2Y10_LED_PIN, LOW); // LED on
        delayMicroseconds(GP2Y10_SAMPLING_US);
        mv_accum += analogReadMilliVolts(GP2Y10_OUT_PIN); // ESP32 calibrated millivolts
        delayMicroseconds(GP2Y10_POST_SAMPLE_US);
        digitalWrite(GP2Y10_LED_PIN, HIGH); // LED off
        delayMicroseconds(GP2Y10_LED_OFF_US);
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
