#include "HapticFeedback.h"

#ifdef HAPTIC_FEEDBACK_PIN

#include "UptimeClock.h"
#include <Arduino.h>

#ifdef HAPTIC_FEEDBACK_ACTIVE_LOW
#define HAPTIC_FEEDBACK_ON_STATE LOW
#define HAPTIC_FEEDBACK_OFF_STATE HIGH
#else
#define HAPTIC_FEEDBACK_ON_STATE HIGH
#define HAPTIC_FEEDBACK_OFF_STATE LOW
#endif

HapticFeedback *hapticFeedback = nullptr;

void initHapticFeedback()
{
    if (!hapticFeedback)
        hapticFeedback = new HapticFeedback();
}

HapticFeedback::HapticFeedback() : concurrency::OSThread("Haptic")
{
    pinMode(HAPTIC_FEEDBACK_PIN, OUTPUT);
    digitalWrite(HAPTIC_FEEDBACK_PIN, HAPTIC_FEEDBACK_OFF_STATE);
}

void HapticFeedback::motorWrite(bool on)
{
    digitalWrite(HAPTIC_FEEDBACK_PIN, on ? HAPTIC_FEEDBACK_ON_STATE : HAPTIC_FEEDBACK_OFF_STATE);
}

void HapticFeedback::pulse(uint16_t durationMs)
{
    motorWrite(true);
    pulseOffAt = Deadline::in(durationMs);
    scheduleNext();
}

void HapticFeedback::armDelayedPulse(uint16_t delayMs, uint16_t durationMs)
{
    delayedPulseAt = Deadline::in(delayMs);
    delayedPulseDuration = durationMs;
    scheduleNext();
}

void HapticFeedback::cancelDelayedPulse()
{
    delayedPulseAt = Deadline();
}

// Milliseconds until whichever of the two pending pulses is due first, or -1 when neither is armed.
int32_t HapticFeedback::msUntilNextPulse() const
{
    const Deadline next = Deadline::sooner(pulseOffAt, delayedPulseAt);
    if (!next.expires()) // nothing that will ever arrive: disarmed, or forever()
        return -1;
    const int32_t delay = next.msFromNow();
    return delay > 0 ? delay : 0;
}

void HapticFeedback::scheduleNext()
{
    const int32_t delay = msUntilNextPulse();
    if (delay < 0)
        return;
    setIntervalFromNow((unsigned long)delay);
}

int32_t HapticFeedback::runOnce()
{
    if (pulseOffAt.passed()) {
        motorWrite(false);
        pulseOffAt = Deadline();
    }

    if (delayedPulseAt.passed()) {
        uint16_t dur = delayedPulseDuration;
        delayedPulseAt = Deadline();
        pulse(dur); // re-arms pulseOffAt and calls scheduleNext()
    }

    const int32_t delay = msUntilNextPulse();
    return delay < 0 ? 60 * 1000 : delay;
}

#endif // HAPTIC_FEEDBACK_PIN
