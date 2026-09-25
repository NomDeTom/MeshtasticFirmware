#pragma once

// Runtime radio-behaviour knobs for hardware bench experiments. Compiled only with -D BENCH_KNOBS=1,
// never in a release image. Knobs live in RAM: a reboot restores the firmware's own behaviour.
// Set from a local API client with a text message beginning "!bench" (see BenchKnobs.cpp); the
// message is consumed and never transmitted.
#ifdef BENCH_KNOBS

#include "configuration.h"
#include <stddef.h>
#include <stdint.h>

struct BenchKnobs {
    enum Lbt : uint8_t {
        LBT_DEFAULT = 0, // the firmware as built
        LBT_OFF,         // no channel check at all: transmit when the backoff expires
        LBT_RX,          // only the in-progress-reception check (preamble/header flags), no CAD
        LBT_CAD,         // CAD, exit to standby on detection
        LBT_CADRX,       // CAD, exit straight into RX on detection
        LBT_CADTX,       // same as LBT_CAD (no hardware CAD->TX path is used); name kept for compatibility
        LBT_RSSI,        // energy only: busy if instantaneous RSSI > noise floor + rssiMargin; no flags, no CAD
        LBT_RXRSSI,      // in-progress-reception flags, or the RSSI test above
    };
    enum Pre : uint8_t {
        PRE_DEFAULT = 0, // the firmware as built
        PRE_HOLD,        // clear a bare preamble and hold TX for one max packet
        PRE_IGNORE,      // a bare preamble does not count as busy; only a valid header does
        PRE_BUSY,        // a latched preamble counts as busy and is never cleared here
    };

    enum TxArm : uint8_t {
        TXARM_DEFAULT = 0, // the firmware as built: log and release the sent packet, then re-arm RX
        TXARM_EARLY,       // re-arm RX first; log and release the sent packet afterwards
    };

    uint8_t lbt = LBT_DEFAULT;
    uint8_t cadSymbols = 0; // raw count 1, 2, 4, 8 or 16; 0 keeps the firmware's
    int8_t cwMin = -1;      // contention window exponent overrides; -1 keeps the firmware's
    int8_t cwMax = -1;
    int16_t slotMs = -1;    // slot time override; -1 keeps the firmware's
    uint16_t fixedMs = 0;   // added to every transmit-delay draw
    bool noBackoff = false; // random part of the transmit delay forced to 0
    uint8_t pre = PRE_DEFAULT;
    uint8_t txArm = TXARM_DEFAULT;
    int16_t syncWord = -1;  // -1 keeps the firmware's
    int8_t iqInvert = -1;   // -1 keeps the firmware's (standard IQ)
    int16_t txPower = -128; // dBm; -128 keeps the configured power (the driver still clamps)
    int16_t detPeak = -1;   // SX126x CAD detPeak/detMin; -1 keeps RadioLib's per-SF default
    int16_t detMin = -1;
    int32_t agcMs = 0;      // AGC reset interval; 0 keeps the firmware's, -1 disables the reset (RX recovery stays)
    int8_t li = -1;         // long interleaving for TX: -1 keeps the driver's choice, 0 off, 1 on
    int8_t rssiMargin = 8;  // dB above the noise floor that counts as busy for LBT_RSSI / LBT_RXRSSI
    uint16_t dcSleepMs = 0; // duty-cycled RX: deaf this long, then listen dcWakeMs, repeating; 0 = off
    uint16_t dcWakeMs = 0;
    uint8_t rxdcRxSym = 0;    // SX126x hardware RX duty cycle (sniff), in symbols: listen this long...
    uint8_t rxdcSleepSym = 0; // ...then sleep this long, repeating; staying in RX on a detected preamble. 0 = off
    uint16_t probeMs = 0;     // passive LBT probe interval: CAD + RX-flag peek + RSSI, logged per second; 0 = off
    uint8_t rxCont = 0;       // 1: continuous RX instead of the duty-cycled default (implied by the RSSI modes)
    uint32_t trigNode = 0;    // hold queued TX until a frame from this node arrives, then decide at atMs; 0 = off
    uint16_t atMs = 0;        // decision offset after the trigger frame's RX_DONE
    bool atDeaf = false;      // deaf from the trigger until atMs, instead of listening
    // Not cleared by "!bench reset": a monitor, not an experiment variable.
    uint16_t nfMs = 0;       // noise-floor sample interval; 0 = sampler off
    int16_t floorDbm = -128; // latest one-second median from the sampler; -128 until it has run
    bool txGap = false;      // log each TX_DONE -> RX re-armed gap ("BENCH txgap"); on nRF52 runs the cycle counter

    bool usesCad() const { return lbt != LBT_OFF && lbt != LBT_RX && lbt != LBT_RSSI && lbt != LBT_RXRSSI; }
    bool checksRx() const { return lbt != LBT_OFF && lbt != LBT_RSSI; }
    bool usesRssi() const { return lbt == LBT_RSSI || lbt == LBT_RXRSSI; }
    uint8_t syncWordOr(uint8_t fallback) const { return syncWord >= 0 ? (uint8_t)syncWord : fallback; }
    bool iqInverted() const { return iqInvert == 1; }
};

extern BenchKnobs benchKnobs;

// Stopwatch for sub-ms bench timing. nRF52 micros() has tick (ms) resolution until DWT runs, so read the cycle
// counter there (txgap=1 starts it). Subtract raw ticks, which wraps cleanly mod 2^32, then convert.
#if defined(ARCH_NRF52) && !defined(ARCH_NRF54L)
#include <nrf.h>
static inline uint32_t benchTicks()
{
    return DWT->CYCCNT;
}
static inline uint32_t benchTicksToUs(uint32_t ticks)
{
    return ticks / (SystemCoreClock / 1000000);
}
#else
#include <Arduino.h>
static inline uint32_t benchTicks()
{
    return micros();
}
static inline uint32_t benchTicksToUs(uint32_t ticks)
{
    return ticks;
}
#endif

/** Parse and apply a "!bench ..." command. Returns true if the text was a bench command (consumed). */
bool benchKnobsHandleCommand(const char *text, size_t len);

/** Log the current knob state on one line, prefixed "BENCH knobs:". */
void benchKnobsLog();

#endif
