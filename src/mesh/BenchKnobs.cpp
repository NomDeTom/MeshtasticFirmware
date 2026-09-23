#include "BenchKnobs.h"

#ifdef BENCH_KNOBS

#include "configuration.h"
#include "RadioLibInterface.h"
#include <stdlib.h>
#include <string.h>

BenchKnobs benchKnobs;

static const char *const LBT_NAMES[] = {"default", "off", "rx", "cad", "cadrx", "cadtx"};
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
    LOG_INFO("BENCH knobs: lbt=%s sym=%u cwmin=%d cwmax=%d slot=%d fixed=%u nobackoff=%d pre=%s sync=%d iq=%d",
             LBT_NAMES[k.lbt], k.cadSymbols, k.cwMin, k.cwMax, k.slotMs, k.fixedMs, k.noBackoff ? 1 : 0, PRE_NAMES[k.pre],
             k.syncWord, k.iqInvert);
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
    char *save = nullptr;
    strtok_r(buf, " ", &save); // "!bench"
    for (char *tok = strtok_r(nullptr, " ", &save); tok; tok = strtok_r(nullptr, " ", &save)) {
        if (strcmp(tok, "reset") == 0) {
            radioChanged |= next.syncWord != -1 || next.iqInvert != -1;
            next = BenchKnobs();
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
        } else {
            LOG_WARN("BENCH: unknown key '%s'", key);
        }
    }
    if (next.cwMin >= 0 && next.cwMax >= 0 && next.cwMin > next.cwMax)
        next.cwMax = next.cwMin;
    benchKnobs = next;
    benchKnobsLog();

    if (radioChanged && RadioLibInterface::instance) {
        // reconfigure() reapplies modulation, sync word and IQ, then restarts RX.
        RadioLibInterface::instance->reconfigure();
    }
    return true;
}

#endif
