#include "PeriodicTestModule.h"
#include "SafeFile.h"
#include "Throttle.h"
#include "configuration.h"
#include "mesh/Router.h"

#if !EXCLUDE_TEST_MODULE

static constexpr uint32_t LOG_INTERVAL_MS = 30 * 1000UL;
static constexpr const char *STATE_FILE = "/prefs/periodicTest.bin";
static constexpr LR2021DcdcProfile DCDC_PROFILES[] = {
    {0, 0, 0, false, "bypass (no workaround - raw hardware baseline)"},
    {15, 15, 2800000, false, "RISE=15 FALL=15 FREQ=2.8MHz (Semtech reset state)"},
    {11, 13, 2800000, false, "RISE=11 FALL=13 FREQ=2.8MHz (narrowband timing)"},
    {11, 13, 4300000, false, "RISE=11 FALL=13 FREQ=4.3MHz (narrowband timing + elevated freq)"},
    {15, 15, 4300000, false, "RISE=15 FALL=15 FREQ=4.3MHz (conservative + elevated freq)"},
    {0, 0, 0, true, "LDO-only (SIMO DCDC disabled via setRegMode SIMO_OFF)"},
};
static constexpr uint8_t DCDC_PROFILE_COUNT = sizeof(DCDC_PROFILES) / sizeof(DCDC_PROFILES[0]);

PeriodicTestModule *periodicTestModule;

PeriodicTestModule::PeriodicTestModule() : concurrency::OSThread("PeriodicTest")
{
    loadFromDisk();
    setIntervalFromNow(1000);
}

const LR2021DcdcProfile &PeriodicTestModule::getCurrentDcdcProfile() const
{
    return DCDC_PROFILES[dcdcProfileIndex % DCDC_PROFILE_COUNT];
}

uint8_t PeriodicTestModule::getDcdcProfileCount() const
{
    return DCDC_PROFILE_COUNT;
}

int32_t PeriodicTestModule::runOnce()
{
    if (Throttle::isWithinTimespanMs(lastTickMs, LOG_INTERVAL_MS))
        return 1000;

    lastTickMs = millis();

    const bool isFirst = !firstFired;
    firstFired = true;

    const LR2021DcdcProfile &profile = getCurrentDcdcProfile();
    LOG_INFO("[PeriodicTest] LR20x0 DCDC ramp test [%u/%u]: %s", (unsigned)(dcdcProfileIndex + 1), (unsigned)DCDC_PROFILE_COUNT,
             profile.label);

    // This is the packet router singleton, not the device role being a router.
    if (router) {
        if (auto *iface = router->getIface()) {
            iface->getCurrentRSSI();
            RadioDiagnostics diagnostics{};
            if (iface->collectDiagnostics(diagnostics)) {
                if (diagnostics.hasNoiseFloor)
                    LOG_INFO("[DCDC-TEST] noise floor: %d dBm", (int)diagnostics.noiseFloor);
                if (diagnostics.hasLastRx)
                    LOG_INFO("[DCDC-TEST] last rx: RSSI=%.1f dBm  SNR=%.2f dB", diagnostics.lastRxRssi, diagnostics.lastRxSnr);
                else
                    LOG_INFO("[DCDC-TEST] last rx: none since boot");

                if (diagnostics.hasDcdcSwitcher) {
                    LOG_INFO("[DCDC-TEST] DCDC_SWITCHER=0x%08X  RISE=%u  FALL=%u", (unsigned)diagnostics.dcdcSwitcher,
                             (unsigned)diagnostics.dcdcRise, (unsigned)diagnostics.dcdcFall);
                } else if (diagnostics.dcdcSwitcherReadFailed) {
                    LOG_WARN("[DCDC-TEST] DCDC_SWITCHER read failed");
                }

                if (diagnostics.hasDcdcFreqLf) {
                    LOG_INFO("[DCDC-TEST] DCDC_FREQ_LF=0x%08X  (~%u Hz)", (unsigned)diagnostics.dcdcFreqLfRaw,
                             (unsigned)diagnostics.dcdcFreqLfHz);
                } else if (diagnostics.dcdcFreqLfReadFailed) {
                    LOG_WARN("[DCDC-TEST] DCDC_FREQ_LF read failed");
                }

                if (diagnostics.hasRfFreq) {
                    LOG_INFO("[DCDC-TEST] RTTOF_RF_FREQ=0x%08X  (~%u Hz)", (unsigned)diagnostics.rfFreqRaw,
                             (unsigned)diagnostics.rfFreqHz);
                } else if (diagnostics.rfFreqReadFailed) {
                    LOG_WARN("[DCDC-TEST] RTTOF_RF_FREQ read failed");
                }
            }
        }
    }

    if (isFirst) {
        advanceDcdcProfile();
        saveToDisk();
    }

    return 1000;
}

void PeriodicTestModule::advanceDcdcProfile()
{
    dcdcProfileIndex = (dcdcProfileIndex + 1) % DCDC_PROFILE_COUNT;
}

void PeriodicTestModule::saveToDisk() const
{
#ifdef FSCom
    FSCom.mkdir("/prefs");
    auto file = SafeFile(STATE_FILE, true);
    const size_t written = file.write(&dcdcProfileIndex, sizeof(dcdcProfileIndex));
    if (!file.close() || written != sizeof(dcdcProfileIndex))
        LOG_WARN("[PeriodicTest] Failed to save state");
#endif
}

void PeriodicTestModule::loadFromDisk()
{
#ifdef FSCom
    auto file = FSCom.open(STATE_FILE, FILE_O_READ);
    if (!file)
        return;
    uint8_t savedProfileIndex = 0;
    const int32_t bytesRead = file.read(&savedProfileIndex, sizeof(savedProfileIndex));
    file.close();
    if (bytesRead != (int32_t)sizeof(savedProfileIndex))
        return;
    dcdcProfileIndex = savedProfileIndex % DCDC_PROFILE_COUNT;
#endif
}
#endif // !EXCLUDE_TEST_MODULE