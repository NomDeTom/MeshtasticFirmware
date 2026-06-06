#include "PeriodicTestModule.h"
#include "SafeFile.h"
#include "Throttle.h"
#include "configuration.h"
#include "main.h"
#include "mesh/Router.h"

#if !EXCLUDE_TEST_MODULE

// Set this to enable automatic reboots every 120s (one per profile cycle).
// Comment out or define as 0 to disable.
#define DCDC_TEST_AUTO_REBOOT 1

// This has pretty much done what I intended. I've disabled the auto-reboot for safety. It basically proves that the LDO and DCDC
// plain settings are both fine-ish.

static constexpr uint32_t FIRST_LOG_DELAY_MS = 45 * 1000UL;
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
    appliedProfileIndex = dcdcProfileIndex; // capture before any advance
    const LR2021DcdcProfile &profile = DCDC_PROFILES[appliedProfileIndex % DCDC_PROFILE_COUNT];
    LOG_INFO("[PeriodicTest] Boot: running profile [%u/%u]: %s", (unsigned)(appliedProfileIndex + 1),
             (unsigned)DCDC_PROFILE_COUNT, profile.label);
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
    const uint32_t interval = firstFired ? LOG_INTERVAL_MS : FIRST_LOG_DELAY_MS;
    if (Throttle::isWithinTimespanMs(lastTickMs, interval))
        return 1000;

    lastTickMs = millis();

    const bool isFirst = !firstFired;
    firstFired = true;

    const LR2021DcdcProfile &profile = DCDC_PROFILES[appliedProfileIndex % DCDC_PROFILE_COUNT];
    LOG_INFO("[PeriodicTest] LR20x0 DCDC ramp test [%u/%u]: %s", (unsigned)(appliedProfileIndex + 1),
             (unsigned)DCDC_PROFILE_COUNT, profile.label);

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

                if (diagnostics.hasPacketStats) {
                    const uint32_t rxDupe = router ? router->rxDupe : 0;
                    const uint32_t txRelayCanceled = router ? router->txRelayCanceled : 0;
                    LOG_INFO("[DCDC-TEST] pkt: txOrig=%u txRelay=%u rxGood=%u rxBad=%u rxDupe=%u relayCancel=%u",
                             (unsigned)(diagnostics.txGood - diagnostics.txRelay), (unsigned)diagnostics.txRelay,
                             (unsigned)diagnostics.rxGood, (unsigned)diagnostics.rxBad, (unsigned)rxDupe,
                             (unsigned)txRelayCanceled);
                }
            }
        }
    }

    if (isFirst) {
        advanceDcdcProfile();
        saveToDisk();
#ifdef DCDC_TEST_AUTO_REBOOT
        // Reboot ~180 s from now so the log lines above are visible before reset.
        rebootAtMsec = millis() + 180 * 1000UL;
        LOG_INFO("[PeriodicTest] Reboot scheduled in 180s");
#endif
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