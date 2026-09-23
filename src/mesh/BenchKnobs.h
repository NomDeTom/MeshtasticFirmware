#pragma once

// Runtime radio-behaviour knobs for hardware bench experiments. Compiled only with -D BENCH_KNOBS=1,
// never in a release image. Knobs live in RAM: a reboot restores the firmware's own behaviour.
// Set from a local API client with a text message beginning "!bench" (see BenchKnobs.cpp); the
// message is consumed and never transmitted.
#ifdef BENCH_KNOBS

#include <stddef.h>
#include <stdint.h>

struct BenchKnobs {
    enum Lbt : uint8_t {
        LBT_DEFAULT = 0, // the firmware as built
        LBT_OFF,         // no channel check at all: transmit when the backoff expires
        LBT_RX,          // only the in-progress-reception check (preamble/header flags), no CAD
        LBT_CAD,         // CAD, exit to standby on detection
        LBT_CADRX,       // CAD, exit straight into RX on detection
        LBT_CADTX,       // CAD, transmit immediately when free (SX126x has no hardware CAD->TX)
    };
    enum Pre : uint8_t {
        PRE_DEFAULT = 0, // the firmware as built
        PRE_HOLD,        // clear a bare preamble and hold TX for one max packet
        PRE_IGNORE,      // a bare preamble does not count as busy; only a valid header does
        PRE_BUSY,        // a latched preamble counts as busy and is never cleared here
    };

    uint8_t lbt = LBT_DEFAULT;
    uint8_t cadSymbols = 0;  // raw count 1, 2, 4, 8 or 16; 0 keeps the firmware's
    int8_t cwMin = -1;       // contention window exponent overrides; -1 keeps the firmware's
    int8_t cwMax = -1;
    int16_t slotMs = -1;     // slot time override; -1 keeps the firmware's
    uint16_t fixedMs = 0;    // added to every transmit-delay draw
    bool noBackoff = false;  // random part of the transmit delay forced to 0
    uint8_t pre = PRE_DEFAULT;
    int16_t syncWord = -1;   // -1 keeps the firmware's
    int8_t iqInvert = -1;    // -1 keeps the firmware's (standard IQ)
    int16_t txPower = -128;  // dBm; -128 keeps the configured power (the driver still clamps)
    int16_t detPeak = -1;    // SX126x CAD detPeak/detMin; -1 keeps RadioLib's per-SF default
    int16_t detMin = -1;
    int32_t agcMs = 0;       // AGC reset interval; 0 keeps the firmware's, -1 disables the reset (RX recovery stays)
    int8_t li = -1;          // long interleaving for TX: -1 keeps the driver's choice, 0 off, 1 on
    // Not cleared by "!bench reset": a monitor, not an experiment variable.
    uint16_t nfMs = 0;       // noise-floor sample interval; 0 = sampler off

    bool usesCad() const { return lbt != LBT_OFF && lbt != LBT_RX; }
    bool checksRx() const { return lbt != LBT_OFF; }
    uint8_t syncWordOr(uint8_t fallback) const { return syncWord >= 0 ? (uint8_t)syncWord : fallback; }
    bool iqInverted() const { return iqInvert == 1; }
};

extern BenchKnobs benchKnobs;

/** Parse and apply a "!bench ..." command. Returns true if the text was a bench command (consumed). */
bool benchKnobsHandleCommand(const char *text, size_t len);

/** Log the current knob state on one line, prefixed "BENCH knobs:". */
void benchKnobsLog();

#endif
