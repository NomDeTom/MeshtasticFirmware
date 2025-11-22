#include "configuration.h"

#if HAS_TELEMETRY && !MESHTASTIC_EXCLUDE_ENVIRONMENTAL_SENSOR && __has_include(<bq2589x.h>)

#include "../mesh/generated/meshtastic/telemetry.pb.h"
#include "BQ25895Sensor.h"
#include "TelemetrySensor.h"
#include <bq2589x.h>

BQ25895Sensor::BQ25895Sensor() : TelemetrySensor(meshtastic_TelemetrySensorType_BQ25895, "BQ25895") {}

int32_t BQ25895Sensor::runOnce()
{
    LOG_INFO("Init sensor: %s", sensorName);
    if (!hasSensor()) {
        return DEFAULT_SENSOR_MINIMUM_WAIT_TIME_BETWEEN_READS;
    }
    if (!bq25895.success()) {
        bq25895 = bq2589x(BQ25895_ADDR);
        status = bq25895.begin();
    } else {
        status = bq25895.success();
    }
    return initI2CSensor();
}

void BQ25895Sensor::setup() {}

bool BQ25895Sensor::getMetrics(meshtastic_Telemetry *measurement)
{
    measurement->variant.environment_metrics.has_voltage = true;
    measurement->variant.environment_metrics.has_current = true;

    measurement->variant.environment_metrics.voltage = bq25895.getBusVoltage_V();
    measurement->variant.environment_metrics.current = bq25895.getCurrent_mA();
    return true;
}

uint16_t BQ25895Sensor::getBusVoltageMv()
{
    return lround(bq25895.getBusVoltage_V() * 1000);
}

int16_t BQ25895Sensor::getCurrentMa()
{
    return lround(bq25895.getCurrent_mA());
}

#endif