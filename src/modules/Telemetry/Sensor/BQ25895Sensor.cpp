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
    status = bq25895.begin();
    initI2CSensor();
    return status;
}

void BQ25895Sensor::setup()
{
    LOG_INFO("Setup BQ25895");
}

bool BQ25895Sensor::getMetrics(meshtastic_Telemetry *measurement)
{
    measurement->variant.environment_metrics.has_voltage = true;
    measurement->variant.environment_metrics.has_current = true;

    measurement->variant.environment_metrics.voltage = bq25895.adc_read_battery_volt();
    measurement->variant.environment_metrics.current = bq25895.adc_read_charge_current();
    LOG_INFO("Get metrics BQ25895");
    return true;
}

uint16_t BQ25895Sensor::getBusVoltageMv()
{
    LOG_INFO("GetBus BQ25895");
    return lround(bq25895.adc_read_battery_volt());
}

int16_t BQ25895Sensor::getCurrentMa()
{
    LOG_INFO("GetCurrentMa BQ25895");
    return lround(bq25895.adc_read_charge_current());
}

#endif