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
///   in(ms) / at(ms)  a timed deadline; passed() becomes true once it arrives.
///   in(0)            timed and already due; what a "run this on the next pass" site wants.
///   forever()        armed with no expiry - "until something cancels it". armed() and active() are
///                    both true; passed() never becomes true.
///
/// THE QUESTIONS
///   armed()     is anything scheduled?    false only when disarmed
///   active()    is it still in force?     armed and not yet arrived - forever() included
///   passed()    has it arrived?           true only for a timed deadline that has been reached
///   isForever() is it the indefinite one? armed, in force, and never arriving
///
/// Within armed(), active() and passed() partition it: exactly one of the two is true, so armed() ==
/// active() || passed(). isForever() splits active() again, into the deadline that is counting down
/// and the one that never will.
///
/// active() is the one most sites want: it is what !passed() is usually meant to say, without the
/// trap that a deadline which was never set has not passed either. Reach for isForever() only where
/// a site must treat "waits on the user" differently from "will go away by itself" - it is spelled
/// with the prefix because C++ will not let a predicate share the factory's name.
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
///   if (reboot.active()) ...                  // "is it still counting down"
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

    /// Armed with no expiry - "until something cancels it". active() stays true; passed() never does.
    static constexpr Deadline forever() { return Deadline(kForever); }

    /// Is anything scheduled? True for forever(), false only for disarmed().
    constexpr bool armed() const { return at_ != kDisarmed; }

    /// Is this the indefinite one - armed, but with no arrival time? The third state, which armed()
    /// and passed() cannot separate on their own.
    constexpr bool isForever() const { return at_ == kForever; }

    /// Complement of armed(), for the sites that read better that way - "nothing is scheduled"
    /// rather than "not (something is scheduled)". Mirrors isWithinTimespanMs()/hasElapsed() above.
    constexpr bool disarmed() const { return at_ == kDisarmed; }

    /// Has the deadline arrived? Always false when disarmed or forever.
    bool passed() const { return isReal() && Throttle::deadlinePassed(at_); }

    /// passed() against a caller-supplied "now", for a loop testing many deadlines against one read.
    bool passedAt(uint32_t nowMs) const { return isReal() && Throttle::deadlinePassedAt(nowMs, at_); }

    /// Is this deadline still in force - armed and not yet arrived? Prefer it to !passed(): a disarmed
    /// deadline has not passed either, so !passed() answers true for one that was never set. That is
    /// the sentinel guard this type exists to make unforgettable.
    ///
    /// forever() is active, which is what "until something cancels it" means. Only isForever() tells
    /// it apart from a deadline that is genuinely counting down.
    bool active() const { return armed() && !passed(); }
    bool activeAt(uint32_t nowMs) const { return armed() && !passedAt(nowMs); }

    /// Milliseconds until this fires - negative once it has passed. Same ~24.8-day range as passed().
    ///
    /// Disarmed and forever() never arrive, so both answer 0, which is also what a deadline due this
    /// instant returns. Only a caller feeding the result into a wait has to tell those apart, and the
    /// two in the tree already reject them before asking. 0 rather than a large sentinel deliberately
    /// - a caller adding a grace period to a big one would overflow, and signed overflow is UB.
    int32_t msFromNow() const { return msFrom(Time::getMillis()); }

    /// The earlier-arriving of two deadlines, for a scheduler picking its next wake-up. Only a timed
    /// deadline can arrive, so a disarmed or forever() one never wins; when neither is timed, b comes
    /// back unchanged - the caller was going to check before waiting on it anyway.
    static Deadline sooner(Deadline a, Deadline b)
    {
        if (!a.isReal())
            return b;
        if (!b.isReal())
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

    /// msFromNow() against a caller-supplied "now", so sooner() compares two deadlines against one
    /// read. Private: no site has wanted it, and msFromNow() is the question callers actually ask.
    int32_t msFrom(uint32_t nowMs) const { return isReal() ? (int32_t)(at_ - nowMs) : 0; }

    /// A real deadline is one the arithmetic may be applied to - neither reserved value.
    constexpr bool isReal() const { return at_ != kDisarmed && at_ != kForever; }

    /// Step a computed value off both reserved values. 1 ms early at most, once per wrap each.
    static constexpr uint32_t sanitise(uint32_t ms) { return (ms == kDisarmed || ms == kForever) ? 1 : ms; }

    uint32_t at_ = kDisarmed;
};
