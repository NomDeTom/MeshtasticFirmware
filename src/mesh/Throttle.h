#pragma once
#include "UptimeClock.h"
#include <cstddef>
#include <cstdint>

class Throttle
{
  public:
    static bool execute(uint32_t *lastExecutionMs, uint32_t minumumIntervalMs, void (*func)(void), void (*onDefer)(void) = NULL);
    static bool isWithinTimespanMs(uint32_t lastExecutionMs, uint32_t intervalMs);

    /// Complement of isWithinTimespanMs(): true once intervalMs has passed since lastExecutionMs.
    /// Boundary is inclusive (>=), mirroring isWithinTimespanMs()'s exclusive <.
    /// Deliberately does not treat lastExecutionMs == 0 as "never run" - callers that use 0 as a
    /// sentinel must test for it separately, so the sentinel never reaches the arithmetic.
    static bool hasElapsed(uint32_t lastExecutionMs, uint32_t intervalMs)
    {
        return !isWithinTimespanMs(lastExecutionMs, intervalMs);
    }

    /// True once an absolute deadline has arrived. Use this rather than comparing against millis()
    /// directly: that inverts while the deadline sits on the far side of the 32-bit wrap, so the
    /// action either fires immediately or blocks for about the interval it should have waited.
    ///
    /// Use this when the site stores a deadline; use hasElapsed() when it stores the time of the
    /// last event, which allows the full ~49.7 day range instead of ~24.8 days ahead.
    ///
    /// Callers that overload the deadline with an "inactive" sentinel (0, or UINT32_MAX) MUST test
    /// for that separately, first: every such value is arithmetically far in the past, so it reads
    /// as passed.
    ///
    /// New code should store a Deadline (below) instead of a bare uint32_t: it makes that MUST
    /// impossible to forget, and gives the "inactive" state a name rather than a magic value.
    static bool deadlinePassed(uint32_t deadlineMs);

    /// deadlinePassed() against a caller-supplied "now", for a loop that snapshots the time once and
    /// tests many deadlines against it. Same range limit and sentinel rules as above.
    static bool deadlinePassedAt(uint32_t nowMs, uint32_t deadlineMs)
    {
        // Passed iff now - deadline has not wrapped past 2^31 ms; further-ahead deadlines land in
        // the top half. Not an int32_t cast, which is implementation-defined beyond INT32_MAX.
        return (uint32_t)(nowMs - deadlineMs) < 0x80000000u;
    }
};

/// An absolute uptime deadline, in a type that cannot be confused with a plain millis value.
///
/// Replaces the bare `uint32_t deadline` idiom and the several different meanings this codebase gave
/// to a magic value stored in one. Same size and cost as that uint32_t: a single member, every
/// method inline.
///
/// STATES
///   Deadline()       disarmed - nothing scheduled. armed() false, passed() never true. The default.
///   in(ms) / at(ms)  an armed deadline; passed() becomes true once it arrives.
///   in(0)            armed and already due; what a "run this on the next pass" site wants.
///   forever()        armed with no expiry - "show until something cancels it". armed() true,
///                    passed() never true.
///
/// Four questions, and they do not collapse into each other:
///   armed()   is anything scheduled?   true for forever(), false only when disarmed
///   expires() is it timed?             false for BOTH disarmed and forever()
///   passed()  has it arrived?          false until a timed deadline is reached
///   pending() is it still running?     armed and not yet arrived - what !passed() is usually
///                                      meant to say, but !passed() is also true when disarmed
///
/// RANGE
///   passed() is correct while the deadline is at most ~24.8 days ahead of now - half the 32-bit
///   millis wrap. Beyond that the comparison inverts. To ask "has interval X elapsed since event Y"
///   over the full ~49.7 days, store the event instead and use Throttle::hasElapsed().
///
/// SENTINELS
///   Two raw values are reserved - 0 for disarmed, UINT32_MAX for forever. in() and at() step past
///   both, so a computed deadline can never land on one by accident. That was the failure mode the
///   bare-uint32_t idiom left to chance: `now + interval` landing on the magic value silently
///   disarms the deadline (or, worse, arms it forever).
///
/// USAGE
///   Deadline reboot;                          // disarmed
///   reboot = Deadline::in(5000);              // fires 5 s from now
///   reboot = Deadline();                      // cancel
///   if (reboot.passed()) { doIt(); reboot.disarm(); }   // one-shot: disarm after acting
///   if (reboot.armed()) ...                   // "is anything scheduled" - never test raw() != 0
///
/// Do not compare raw() against anything. It exists for logging and for the few places that must
/// serialise the value; the comparison it looks like you want is passed().
class Deadline
{
  public:
    /// Disarmed - nothing scheduled. This is also what a cancel path assigns: `d = Deadline();`
    constexpr Deadline() = default;

    /// Arm delayMs from now. delayMs of 0 means "already due".
    static Deadline in(uint32_t delayMs) { return Deadline(sanitise(Time::getMillis() + delayMs)); }

    /// Arm at an absolute uptime stamp, for a caller that already computed one.
    static Deadline at(uint32_t whenMs) { return Deadline(sanitise(whenMs)); }

    /// Armed with no expiry - "until something cancels it". passed() is never true.
    static constexpr Deadline forever() { return Deadline(kForever); }

    /// Is anything scheduled? True for forever(), false only for disarmed().
    constexpr bool armed() const { return at_ != kDisarmed; }

    /// Complement of armed(), for the sites that read better that way - "nothing is scheduled"
    /// rather than "not (something is scheduled)". Mirrors isWithinTimespanMs()/hasElapsed() above.
    constexpr bool disarmed() const { return at_ == kDisarmed; }

    /// Does this deadline have an arrival time at all? False for the two states passed() can never
    /// become true for: disarmed and forever(). Use it where the question is "is this one timed",
    /// which is distinct from armed() ("is anything scheduled") and passed() ("has it arrived").
    constexpr bool expires() const { return isReal(); }

    /// Has the deadline arrived? Always false when disarmed or forever.
    bool passed() const { return isReal() && Throttle::deadlinePassed(at_); }

    /// passed() against a caller-supplied "now", for a loop testing many deadlines against one read.
    bool passedAt(uint32_t nowMs) const { return isReal() && Throttle::deadlinePassedAt(nowMs, at_); }

    /// Armed and not yet arrived - "is this still running". Prefer it to !passed(): a disarmed
    /// deadline has not passed either, so !passed() answers true for one that was never set. That is
    /// the sentinel guard this type exists to make unforgettable, so do not hand-write it.
    /// forever() is pending, which is what "until something cancels it" means.
    bool pending() const { return armed() && !passed(); }
    bool pendingAt(uint32_t nowMs) const { return armed() && !passedAt(nowMs); }

    /// Milliseconds until this fires - negative once it has passed. Same ~24.8-day range as passed().
    ///
    /// Disarmed and forever() never arrive, so both answer 0. Ask expires() first when the answer
    /// feeds a wait: 0 is also what a deadline due this instant returns, and the two want opposite
    /// treatment. 0 rather than a large sentinel deliberately - a caller adding to the result (a
    /// grace period, a minimum wait) would overflow a big one, and signed overflow is undefined.
    int32_t msFromNow() const { return msFrom(Time::getMillis()); }
    int32_t msFrom(uint32_t nowMs) const { return isReal() ? (int32_t)(at_ - nowMs) : 0; }

    /// The earlier-arriving of two deadlines, for a scheduler picking its next wake-up. Only a timed
    /// deadline can arrive, so a disarmed or forever() one never wins; when neither expires, b comes
    /// back unchanged - the caller was going to ask expires() before waiting on it anyway.
    static Deadline sooner(Deadline a, Deadline b)
    {
        if (!a.expires())
            return b;
        if (!b.expires())
            return a;
        const uint32_t now = Time::getMillis(); // one read, so the two comparisons agree
        return a.msFrom(now) <= b.msFrom(now) ? a : b;
    }

    void disarm() { at_ = kDisarmed; }

    /// Raw stored value - logging and serialisation only. See the note above.
    constexpr uint32_t raw() const { return at_; }

  private:
    static constexpr uint32_t kDisarmed = 0;
    static constexpr uint32_t kForever = UINT32_MAX;

    explicit constexpr Deadline(uint32_t at) : at_(at) {}

    /// A real deadline is one the arithmetic may be applied to - neither reserved value.
    constexpr bool isReal() const { return at_ != kDisarmed && at_ != kForever; }

    /// Step a computed value off both reserved values. 1 ms early at most, once per wrap each.
    static constexpr uint32_t sanitise(uint32_t ms) { return (ms == kDisarmed || ms == kForever) ? 1 : ms; }

    uint32_t at_ = kDisarmed;
};
