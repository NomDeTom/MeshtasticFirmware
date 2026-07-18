#include "configuration.h"

#if HAS_TELEMETRY && !MESHTASTIC_EXCLUDE_AIR_QUALITY_SENSOR && defined(GP2Y10_SENSOR_EN)

#include "../mesh/generated/meshtastic/telemetry.pb.h"
#include "TelemetrySensor.h"

// Sharp GP2Y1014AU / GP2Y1010AU0F optical dust sensor (e.g. Keyestudio KS0196).
// Pure analog sensor: pulse the IR LED, sample the analog output, convert to dust density.
class GP2Y10Sensor : public TelemetrySensor
{
  public:
    GP2Y10Sensor();
    virtual bool getMetrics(meshtastic_Telemetry *measurement) override;
    virtual bool initDevice(TwoWire *bus, ScanI2C::FoundDevice *dev) override;

  private:
    float readDustDensityMgM3(); // returns clamped mg/m3
};

#endif
