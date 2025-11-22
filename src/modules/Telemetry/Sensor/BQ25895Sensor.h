#include "configuration.h"

#if HAS_TELEMETRY && !MESHTASTIC_EXCLUDE_ENVIRONMENTAL_SENSOR && __has_include(<bq2589x.h>)

#include "../mesh/generated/meshtastic/telemetry.pb.h"
#include "CurrentSensor.h"
#include "TelemetrySensor.h"
#include "VoltageSensor.h"
#include <bq2589x.h>

class BQ25895Sensor : public TelemetrySensor, VoltageSensor, CurrentSensor
{
  private:
    bq2589x bq25895;

  protected:
    virtual void setup() override;

  public:
    BQ25895Sensor();
    virtual int32_t runOnce() override;
    virtual bool getMetrics(meshtastic_Telemetry *measurement) override;
    virtual uint16_t getBusVoltageMv() override;
    virtual int16_t getCurrentMa() override;
};

#endif