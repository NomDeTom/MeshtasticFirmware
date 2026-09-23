#include "BenchKnobs.h"

#ifdef BENCH_KNOBS

#include "configuration.h"
#include "RadioLibInterface.h"
#include "concurrency/OSThread.h"
#include <algorithm>
#include <stdlib.h>
#include <string.h>

BenchKnobs benchKnobs;

// Samples instantaneous RSSI every nfMs while the radio idles in RX and logs a one-second summary. It never
// calls isActivelyReceiving(): that clears latched preamble flags and starts the preamble hold, so polling it
// here would change the LBT behaviour under test. A frame in flight shows up as high readings instead.
class BenchNoiseThread : public concurrency::OSThread
{
  public:
    BenchNoiseThread() : OSThread("BenchNf") {}

  protected:
    int32_t runOnce() override
    {
        if (benchKnobs.nfMs == 0 || !RadioLibInterface::instance)
            return 1000;
        int16_t rssi = 0;
        if (RadioLibInterface::instance->benchSampleRssi(rssi)) {
            if (count < MAX_SAMPLES)
                samples[count++] = rssi;
        } else {
            skipped++;
        }
        const uint32_t now = millis();
        if (now - windowStart >= 1000) {
            if (count) {
                std::sort(samples, samples + count);
                long sum = 0;
                for (uint8_t i = 0; i < count; i++)
                    sum += samples[i];
                benchKnobs.floorDbm = samples[count / 2];
                LOG_INFO("BENCH nf: n=%u skip=%u p50=%d p90=%d min=%d max=%d avg=%ld", count, skipped, samples[count / 2],
                         samples[(count * 9) / 10], samples[0], samples[count - 1], sum / count);
            } else {
                LOG_INFO("BENCH nf: n=0 skip=%u", skipped);
            }
            count = skipped = 0;
            windowStart = now;
        }
        return benchKnobs.nfMs;
    }

  private:
    static const uint8_t MAX_SAMPLES = 40;
    int16_t samples[MAX_SAMPLES] = {};
    uint8_t count = 0;
    uint16_t skipped = 0;
    uint32_t windowStart = 0;
};

static BenchNoiseThread *benchNoiseThread;

// Duty-cycled RX, like a power-saving node: deaf for dcSleepMs, then listening for dcWakeMs, repeating. A queued
// packet is held through the sleep and decided at the instant of waking (see RadioLibInterface::benchDeaf).
class BenchDutyThread : public concurrency::OSThread
{
  public:
    BenchDutyThread() : OSThread("BenchDc") {}

  protected:
    int32_t runOnce() override
    {
        if (!benchKnobs.dcSleepMs || !RadioLibInterface::instance)
            return 500;
        RadioLibInterface::instance->benchDeaf(benchKnobs.dcSleepMs, true);
        return benchKnobs.dcSleepMs + benchKnobs.dcWakeMs;
    }
};

static BenchDutyThread *benchDutyThread;

// Passive LBT probe: every probeMs, one reading of each method without transmitting. Logs per second how often
// CAD, the RX flags and RSSI-above-floor each called the channel busy, plus the RSSI median.
class BenchProbeThread : public concurrency::OSThread
{
  public:
    BenchProbeThread() : OSThread("BenchProbe") {}

  protected:
    int32_t runOnce() override
    {
        if (!benchKnobs.probeMs || !RadioLibInterface::instance)
            return 500;
        bool cad = false, rx = false;
        int16_t rssi = 0;
        if (RadioLibInterface::instance->benchProbe(cad, rx, rssi)) {
            n++;
            cadBusy += cad;
            rxBusy += rx;
            const int32_t floor = benchKnobs.floorDbm > -128 ? benchKnobs.floorDbm
                                                             : RadioLibInterface::instance->getNoiseFloor();
            rssiBusy += rssi > floor + benchKnobs.rssiMargin;
            if (n <= MAX_SAMPLES)
                samples[n - 1] = rssi;
        } else {
            skipped++;
        }
        const uint32_t now = millis();
        if (now - windowStart >= 1000) {
            const uint8_t k = n < MAX_SAMPLES ? n : MAX_SAMPLES;
            std::sort(samples, samples + k);
            LOG_INFO("BENCH probe: n=%u skip=%u cad_busy=%u rx_busy=%u rssi_busy=%u rssi_p50=%d floor=%d", n, skipped,
                     cadBusy, rxBusy, rssiBusy, k ? samples[k / 2] : 0, benchKnobs.floorDbm);
            n = skipped = cadBusy = rxBusy = rssiBusy = 0;
            windowStart = now;
        }
        return benchKnobs.probeMs;
    }

  private:
    static const uint8_t MAX_SAMPLES = 40;
    int16_t samples[MAX_SAMPLES] = {};
    uint8_t n = 0, skipped = 0, cadBusy = 0, rxBusy = 0, rssiBusy = 0;
    uint32_t windowStart = 0;
};

static BenchProbeThread *benchProbeThread;

static const char *const LBT_NAMES[] = {"default", "off", "rx", "cad", "cadrx", "cadtx", "rssi", "rxrssi"};
static const char *const PRE_NAMES[] = {"default", "hold", "ignore", "busy"};

static int indexOf(const char *const *names, size_t n, const char *v)
{
    for (size_t i = 0; i < n; i++)
        if (strcmp(names[i], v) == 0)
            return (int)i;
    return -1;
}

void benchKnobsLog()
{
    const BenchKnobs &k = benchKnobs;
    LOG_INFO("BENCH knobs: lbt=%s sym=%u cwmin=%d cwmax=%d slot=%d fixed=%u nobackoff=%d pre=%s sync=%d iq=%d pwr=%d "
             "detpeak=%d detmin=%d agc=%ld li=%d nf=%u rssim=%d floor=%d dc=%u/%u rxdc=%u/%u",
             LBT_NAMES[k.lbt], k.cadSymbols, k.cwMin, k.cwMax, k.slotMs, k.fixedMs, k.noBackoff ? 1 : 0, PRE_NAMES[k.pre],
             k.syncWord, k.iqInvert, k.txPower, k.detPeak, k.detMin, (long)k.agcMs, k.li, k.nfMs, k.rssiMargin, k.floorDbm, k.dcSleepMs, k.dcWakeMs, k.rxdcRxSym, k.rxdcSleepSym);
}

// Syntax: "!bench" (report), "!bench reset", or "!bench key=value [key=value ...]". Unknown keys and bad
// values are reported and skipped; the rest still apply. Radio-level knobs (sync, iq) reconfigure the radio.
bool benchKnobsHandleCommand(const char *text, size_t len)
{
    static const char PREFIX[] = "!bench";
    const size_t plen = sizeof(PREFIX) - 1;
    if (len < plen || strncmp(text, PREFIX, plen) != 0 || (len > plen && text[plen] != ' '))
        return false;

    char buf[200];
    size_t n = len < sizeof(buf) - 1 ? len : sizeof(buf) - 1;
    memcpy(buf, text, n);
    buf[n] = 0;

    BenchKnobs next = benchKnobs;
    bool radioChanged = false;
    uint32_t jamMs = 0;  // actions, not settings: key a carrier once, now
    uint32_t deafMs = 0; // drop RX now, hold any queued TX, rejoin and decide after this long
    char *save = nullptr;
    strtok_r(buf, " ", &save); // "!bench"
    for (char *tok = strtok_r(nullptr, " ", &save); tok; tok = strtok_r(nullptr, " ", &save)) {
        if (strcmp(tok, "reset") == 0) {
            radioChanged |= next.syncWord != -1 || next.iqInvert != -1 || next.txPower != -128 || next.li != -1 ||
                            next.rxdcRxSym != 0;
            const uint16_t keepNf = next.nfMs;
            const int16_t keepFloor = next.floorDbm;
            next = BenchKnobs();
            next.nfMs = keepNf;
            next.floorDbm = keepFloor;
            continue;
        }
        char *eq = strchr(tok, '=');
        if (!eq) {
            LOG_WARN("BENCH: ignored '%s' (want key=value)", tok);
            continue;
        }
        *eq = 0;
        const char *key = tok, *val = eq + 1;
        long num = strtol(val, nullptr, 0);
        if (strcmp(key, "lbt") == 0) {
            int i = indexOf(LBT_NAMES, sizeof(LBT_NAMES) / sizeof(*LBT_NAMES), val);
            if (i < 0)
                LOG_WARN("BENCH: lbt=%s unknown", val);
            else
                next.lbt = (uint8_t)i;
        } else if (strcmp(key, "pre") == 0) {
            int i = indexOf(PRE_NAMES, sizeof(PRE_NAMES) / sizeof(*PRE_NAMES), val);
            if (i < 0)
                LOG_WARN("BENCH: pre=%s unknown", val);
            else
                next.pre = (uint8_t)i;
        } else if (strcmp(key, "sym") == 0) {
            if (num == 0 || num == 1 || num == 2 || num == 4 || num == 8 || num == 16)
                next.cadSymbols = (uint8_t)num;
            else
                LOG_WARN("BENCH: sym=%ld not one of 0/1/2/4/8/16", num);
        } else if (strcmp(key, "cwmin") == 0) {
            next.cwMin = (int8_t)constrain(num, -1, 10);
        } else if (strcmp(key, "cwmax") == 0) {
            next.cwMax = (int8_t)constrain(num, -1, 10);
        } else if (strcmp(key, "slot") == 0) {
            next.slotMs = (int16_t)constrain(num, -1, 2000);
        } else if (strcmp(key, "fixed") == 0) {
            next.fixedMs = (uint16_t)constrain(num, 0, 10000);
        } else if (strcmp(key, "nobackoff") == 0) {
            next.noBackoff = num != 0;
        } else if (strcmp(key, "sync") == 0) {
            next.syncWord = (int16_t)constrain(num, -1, 255);
            radioChanged = true;
        } else if (strcmp(key, "iq") == 0) {
            next.iqInvert = (int8_t)constrain(num, -1, 1);
            radioChanged = true;
        } else if (strcmp(key, "pwr") == 0) {
            next.txPower = (int16_t)(strcmp(val, "default") == 0 ? -128 : constrain(num, -17, 30));
            radioChanged = true;
        } else if (strcmp(key, "detpeak") == 0) {
            next.detPeak = (int16_t)constrain(num, -1, 255);
        } else if (strcmp(key, "detmin") == 0) {
            next.detMin = (int16_t)constrain(num, -1, 255);
        } else if (strcmp(key, "agc") == 0) {
            if (strcmp(val, "off") == 0)
                next.agcMs = -1;
            else if (strcmp(val, "default") == 0 || num == 0)
                next.agcMs = 0;
            else
                next.agcMs = constrain(num, 200, 3600000L);
        } else if (strcmp(key, "li") == 0) {
            next.li = (int8_t)constrain(num, -1, 1);
            radioChanged = true;
        } else if (strcmp(key, "rssim") == 0) {
            next.rssiMargin = (int8_t)constrain(num, 0, 60);
        } else if (strcmp(key, "probe") == 0) {
            next.probeMs = num <= 0 ? 0 : (uint16_t)constrain(num, 50, 60000);
        } else if (strcmp(key, "rxdc") == 0) {
            // "rxdc=4/4": RX symbols / sleep symbols; "rxdc=0" turns it off
            char *slash = nullptr;
            const long rxSym = strtol(val, &slash, 0);
            const long sleepSym = (slash && *slash == '/') ? strtol(slash + 1, nullptr, 0) : 0;
            next.rxdcRxSym = (uint8_t)constrain(rxSym, 0, 255);
            next.rxdcSleepSym = (uint8_t)constrain(sleepSym, 0, 255);
            radioChanged = true;
        } else if (strcmp(key, "dcsleep") == 0) {
            next.dcSleepMs = (uint16_t)constrain(num, 0, 10000);
        } else if (strcmp(key, "dcwake") == 0) {
            next.dcWakeMs = (uint16_t)constrain(num, 0, 10000);
        } else if (strcmp(key, "deaf") == 0) {
            deafMs = (uint32_t)constrain(num, 5, 5000);
        } else if (strcmp(key, "jam") == 0) {
            jamMs = (uint32_t)constrain(num, 5, 2000);
        } else if (strcmp(key, "nf") == 0) {
            next.nfMs = num <= 0 ? 0 : (uint16_t)constrain(num, 50, 60000);
        } else {
            LOG_WARN("BENCH: unknown key '%s'", key);
        }
    }
    if (next.cwMin >= 0 && next.cwMax >= 0 && next.cwMin > next.cwMax)
        next.cwMax = next.cwMin;
    benchKnobs = next;
    if (deafMs && RadioLibInterface::instance) {
        // Like jam: timed by the host against another node's frame, so act before logging anything.
        RadioLibInterface::instance->benchDeaf(deafMs);
        return true;
    }
    if (jamMs && RadioLibInterface::instance) {
        // First, before any logging: the host times this command to land inside a frame.
        RadioLibInterface::instance->benchJam(jamMs);
        return true;
    }
    if (benchKnobs.nfMs && !benchNoiseThread)
        benchNoiseThread = new BenchNoiseThread();
    if (benchKnobs.dcSleepMs && !benchDutyThread)
        benchDutyThread = new BenchDutyThread();
    if (benchKnobs.probeMs && !benchProbeThread)
        benchProbeThread = new BenchProbeThread();
    benchKnobsLog();

    if (radioChanged && RadioLibInterface::instance) {
        // reconfigure() reapplies modulation, sync word and IQ, then restarts RX.
        RadioLibInterface::instance->reconfigure();
    }
    return true;
}

#endif
