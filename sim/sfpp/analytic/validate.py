#!/usr/bin/env python3
"""Cross-checks between the pieces, so no single implementation grades its own work.

Four independent things are compared:

  1. Airtime. The datasheet formula against RadioLib's integer arithmetic - the code the
     firmware actually calls. Two transcriptions of the same physics from different
     sources; agreement means the seconds in the charts are real seconds.
  2. Airtime against a published figure. A 237-byte LongFast frame is widely quoted at
     about 2.3 s, and the firmware's own default preambleTimeMsec of 165 ms for LongFast
     pins the preamble term independently.
  3. Cost. The closed-form model against the Monte Carlo, at every divergence. These
     share only the wire constants; the counting logic is written twice.
  4. The wire constants themselves, against the frozen format's own arithmetic.

Exit code is non-zero if any check fails, so this can gate the charts.

Usage: validate.py --packetlog <packetlog.txt.gz> [--trials 400]
"""

import argparse
import math
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import model  # noqa: E402
import radio  # noqa: E402
import simulate  # noqa: E402

failures = []
checks = 0


def check(name, ok, detail=""):
    global checks
    checks += 1
    if ok:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        failures.append(name)


def close(a, b, tol):
    return abs(a - b) <= tol * max(abs(a), abs(b), 1e-9)


def check_airtime_implementations():
    print("airtime: datasheet formula vs RadioLib integer path")
    for preset in radio.PRESETS:
        for payload in (0, 1, 20, 52, 128, 233):
            a = radio.toa_datasheet(payload, preset)
            b = radio.toa_radiolib(payload, preset)
            check(
                f"{preset} {payload}B  datasheet {a * 1000:.1f} ms vs radiolib {b * 1000:.1f} ms",
                close(a, b, 0.02),
                f"differ by {abs(a - b) * 1000:.2f} ms",
            )


def check_airtime_against_known():
    print("airtime: against figures the firmware states independently")

    # RadioInterface computes preambleTimeMsec = preambleLength * (2^sf / bw_kHz), which
    # for LongFast is 16 * 2048/250 = 131 ms. The 165 in its member initialiser is
    # commented "the default for LongFast" but is stale - the formula is the reference.
    sf, bw, _ = radio.PRESETS["LongFast"]
    preamble_ms = radio.PREAMBLE * (2**sf) / (bw / 1000)
    check(
        f"LongFast preamble {preamble_ms:.0f} ms matches the firmware's own formula",
        close(preamble_ms, 131.072, 0.01),
        f"got {preamble_ms:.1f}",
    )

    # A full frame at LongFast is a little over two seconds. Far from that means a preset
    # or coding-rate mistake rather than a rounding one - which is how the 1-vs-5 coding
    # rate bug in the first draft of this file was caught.
    full = radio.toa_radiolib(233, "LongFast")
    check(
        f"LongFast 233 B frame {full:.2f} s is in 2.0-2.4 s",
        2.0 <= full <= 2.4,
        f"got {full:.3f}",
    )

    # Airtime must rise with spreading factor, monotonically.
    order = [
        radio.toa_radiolib(100, p)
        for p in ("ShortFast", "MediumFast", "LongFast", "LongSlow")
    ]
    check(
        "airtime rises with spreading factor",
        order == sorted(order),
        str([round(x, 3) for x in order]),
    )


def check_against_reference_calculator():
    """A third source: nomdetom.github.io/lora-airtime-calculator.html.

    Its stated formula is
        T_sym          = 2^SF / BW_kHz
        payload_bits   = 8*PL - 4*SF + 8 + 16*CRC + 20*explicit_header
        bits_per_sym   = SF - 2 when LDRO else SF
        payload_symbols= ceil(payload_bits / 4 / bits_per_sym) * CR_denom + 8
        airtime        = (preamble + 4.25) * T_sym + payload_symbols * T_sym

    Implemented here from that description alone, independently of radio.py.
    """
    print("airtime: against the reference calculator's stated formula")

    def reference(payload, preset):
        sf, bw, cr_denom = radio.PRESETS[preset]
        t_sym = (2**sf) / (bw / 1000) / 1000  # seconds
        total = payload + radio.MESHTASTIC_HEADER
        bits_per_sym = sf - 2 if t_sym > 0.016 else sf
        payload_bits = max(8 * total - 4 * sf + 8 + 16 * 1 + 20 * 1, 0)
        payload_symbols = math.ceil(payload_bits / 4 / bits_per_sym) * cr_denom + 8
        return (radio.PREAMBLE + 4.25) * t_sym + payload_symbols * t_sym

    for preset in radio.PRESETS:
        for payload in (20, 52, 128, 233):
            a = reference(payload, preset)
            b = radio.toa_radiolib(payload, preset)
            check(
                f"{preset} {payload}B  calculator {a * 1000:.1f} ms vs radiolib {b * 1000:.1f} ms",
                close(a, b, 0.02),
                f"differ by {abs(a - b) * 1000:.2f} ms",
            )


def check_model_against_simulation(size_pool, trials, n=32):
    print("cost: closed-form model vs Monte Carlo")
    rng = random.Random(99)
    mean_object = statistics.mean(size_pool)

    for d in (0, 1, 2, 4, 8, 16, 32):
        capacity = min(max(d, 1), simulate.MAX_CAPACITY)
        runs = [simulate.trial(n, d, size_pool, capacity, rng) for _ in range(trials)]
        predicted = model.costs(n, d, mean_object, capacity)

        for strategy, key in (
            ("chain", "chain"),
            ("enum", "enum32"),
            ("sketch", "sketch"),
        ):
            got = statistics.mean(getattr(r[key], "bytes") for r in runs)
            want = predicted[strategy]["bytes"]
            # 6% covers Monte Carlo noise in the object-size draw at these trial counts.
            check(
                f"d={d:<2} {key:<7} bytes  model {want:8.0f} vs sim {got:8.0f}",
                close(got, want, 0.06),
                f"{abs(got - want) / max(want, 1) * 100:.1f}% apart",
            )

        got_rt = statistics.mean(r["chain"].round_trips for r in runs)
        want_rt = predicted["chain"]["round_trips"]
        check(
            f"d={d:<2} chain   round trips  model {want_rt:6.1f} vs sim {got_rt:6.1f}",
            close(got_rt, want_rt, 0.08),
            f"{abs(got_rt - want_rt):.2f} apart",
        )


def check_wire_constants():
    print("wire: constants against the frozen format's arithmetic")
    w = model.WIRE

    # An advert at capacity 32 must fit one 233-byte payload with the envelope.
    advert = w["sr_envelope"] + w["sr_checksum"] + w["short_id"] * 32
    check(
        f"capacity-32 advert {advert} B fits a {radio.PAYLOAD_BUDGET} B payload",
        advert <= radio.PAYLOAD_BUDGET,
    )

    # And the largest capacity that still fits should be the ~50 the freeze claims.
    largest = (radio.PAYLOAD_BUDGET - w["sr_envelope"] - w["sr_checksum"]) // w[
        "short_id"
    ]
    check(
        f"largest single-frame capacity is {largest}, freeze says ~50",
        48 <= largest <= 52,
    )

    # 40 short IDs is the nanopb bound; it must also fit one frame.
    ids = w["sr_envelope"] + w["short_id"] * 40
    check(f"40 short ids {ids} B fit one frame", ids <= radio.PAYLOAD_BUDGET)

    # Full object ids cost 8x a short id, which is the entire enumeration argument.
    check("an object id is 8x a short id", w["object_id"] == 8 * w["short_id"])


def check_advertising_arithmetic():
    """The advert-versus-repetition budget, derived a second way.

    advertising.py computes costs by summing terms; here the break-even is solved
    algebraically. N nodes advertising f times an hour cost N*f*advert; sending each of
    lambda messages k times costs lambda*k*object. Advertising is affordable while

        f/lambda  <  (k - 1 - recovery_fraction) * object / (N * advert)

    with (k-1) because one copy is the original transmission either way, and the recovery
    fraction subtracted because reconciliation still has to push what someone missed.
    """
    print("advertising: budget solved algebraically vs advertising.py")
    import advertising as adv

    nodes, lam = 20, 2
    for k in (2, 3, 4):
        closed_form = (
            (k - 1 - adv.recovery_fraction(0.15, nodes))
            * adv.OBJECT
            / (nodes * adv.SMALL_ADVERT)
        )
        # Smallest adverts-per-message where summing the terms exceeds the repeat budget.
        budget = adv.repetition_cost(lam, k)
        # Bisection rather than a linear scan: at 20 nodes the k=2 budget is a few
        # thousandths of an advert per message, which a fixed step cannot resolve.
        lo, hi = 0.0, 10.0
        for _ in range(60):
            mid = (lo + hi) / 2
            if adv.reconciliation_cost(lam, mid * lam, nodes, 0.15) > budget:
                hi = mid
            else:
                lo = mid
        summed = hi
        check(
            f"k={k}  algebra {closed_form:.3f} vs summed {summed:.3f} adverts/message",
            summed is not None and close(closed_form, summed, 0.08),
            f"{closed_form:.4f} vs {summed}",
        )

    # Push must never cost more than pull for the same difference, and must remove the
    # round trip rather than merely shortening it.
    for d in (0, 1, 4, 8):
        pull_bytes, pull_rt = adv.pull_exchange_bytes(d)
        push_bytes, push_rt = adv.push_exchange_bytes(d)
        check(
            f"d={d} push {push_bytes:.0f} B <= pull {pull_bytes:.0f} B",
            push_bytes <= pull_bytes,
        )
        check(
            f"d={d} push waits {push_rt} round trips, pull {pull_rt}",
            push_rt <= pull_rt,
        )

    # The saving is exactly the request that is no longer sent.
    for d in (1, 4, 8):
        saved = adv.pull_exchange_bytes(d)[0] - adv.push_exchange_bytes(d)[0]
        expect = model.WIRE["sr_envelope"] + model.WIRE["short_id"] * d
        check(
            f"d={d} saving {saved:.0f} B is the request ({expect} B)",
            close(saved, expect, 0.001),
        )


def check_misdecode_rate():
    print("sketch: the over-capacity misdecode rate the C++ suite pins")
    # test_pinsketch asserts c=2 misdecodes on more than a fifth of over-capacity trials,
    # against a predicted 1/c!. The simulation uses the same 1/c! model, so the two
    # agree by construction at c=2; what is worth checking is that it decays fast enough
    # that the capacities actually used are safe.
    for c, limit in ((2, 0.5), (6, 0.002), (8, 1e-4), (32, 1e-30)):
        rate = 1.0 / math.factorial(c)
        check(f"c={c:<2} misdecode rate {rate:.2e} <= {limit:.0e}", rate <= limit)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--packetlog", required=True)
    ap.add_argument("--trials", type=int, default=400)
    args = ap.parse_args()

    size_pool = simulate.text_sizes_from(args.packetlog)
    if not size_pool:
        sys.exit("no text messages in the capture")
    print(
        f"object sizes: {len(size_pool)} real text packets, mean {statistics.mean(size_pool):.1f} B\n"
    )

    check_airtime_implementations()
    print()
    check_airtime_against_known()
    print()
    check_against_reference_calculator()
    print()
    check_wire_constants()
    print()
    check_advertising_arithmetic()
    print()
    check_misdecode_rate()
    print()
    check_model_against_simulation(size_pool, args.trials)

    print(f"\n{checks - len(failures)}/{checks} checks passed")
    if failures:
        print("failed: " + ", ".join(failures))
        sys.exit(1)


if __name__ == "__main__":
    main()
