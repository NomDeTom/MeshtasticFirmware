#!/usr/bin/env python3
"""Monte Carlo: draw the gaps, run each catch-up strategy, count what it cost.

Four ways for a node that has been away to find out what it missed and fetch it:

  chain   - today's SFPP. LINK_REQUEST names the last commit hash the node holds and
            LINK_PROVIDE returns the one link after it. The chain is ordered by
            insertion, so the walk resumes at the *earliest* gap and re-transfers every
            link after it, held or not. One round trip per link.
  enum32  - enumerate the bucket naming full 32-byte object hashes, as an
            archive-manifest design does. Two round trips at any divergence.
  enum4   - the same, naming 4-byte short IDs. This design's own escalation path, and
            the honest floor for enumeration.
  sketch  - one advert carries a sketch; XOR against the local one and decode the
            difference directly. Costs 4 bytes per unit of capacity, and capacity has to
            cover the difference or the decode fails and it falls back to enum4.

Object sizes are drawn from a real capture rather than assumed. The gap *pattern* is
uniform, which is the pessimistic case for the chain walk's rival and the optimistic one
for the chain walk itself - real absence is bursty, and a burst puts every gap together
at the tail where the walk is cheapest. Read the chain curve as a lower bound.

Usage: simulate.py --packetlog <packetlog.txt.gz> [--trials 200] [--outdir .]
"""

import argparse
import gzip
import json
import math
import os
import random
import statistics
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import radio  # noqa: E402
from model import WIRE  # noqa: E402
from parse_packetlog import parse_line  # noqa: E402

BUCKET_OBJECTS = 32
MAX_CAPACITY = 32
PRESET = "LongFast"

# Per-transmission miss rate, used only to turn "objects missed per hour" into the
# message rate that a repeat-it-again strategy would have to pay for.
MISS_RATE = 0.15
MEAN_OBJECT = (
    52.9 + WIRE["sfpp_overhead"]
)  # measured mean text packet, plus SFPP framing


class Exchange:
    """What one catch-up cost, in the units that matter."""

    def __init__(self):
        self.bytes = 0
        self.packets = 0
        self.round_trips = 0
        self.airtime = 0.0

    def send(self, payload_bytes, count=1, round_trip=False):
        for _ in range(count):
            self.bytes += payload_bytes
            self.packets += radio.frames(payload_bytes)
            self.airtime += radio.airtime(payload_bytes, PRESET)
            if round_trip:
                self.round_trips += 1

    def absorb(self, other):
        self.bytes += other.bytes
        self.packets += other.packets
        self.round_trips += other.round_trips
        self.airtime += other.airtime


def deliver(ex, sizes):
    """The objects themselves, which every strategy pays identically."""
    for size in sizes:
        ex.send(size + WIRE["sfpp_overhead"])


def run_chain(missing, sizes):
    ex = Exchange()
    if not missing:
        return ex
    first_gap = min(missing)
    ex.send(WIRE["sfpp_request"], count=len(sizes) - first_gap, round_trip=True)
    deliver(ex, sizes[first_gap:])
    return ex


def run_enum(missing, sizes, id_bytes):
    ex = Exchange()
    ex.send(WIRE["sr_envelope"], round_trip=True)
    ex.send(WIRE["sr_envelope"] + id_bytes * len(sizes))
    if missing:
        ex.send(WIRE["sr_envelope"] + WIRE["short_id"] * len(missing), round_trip=True)
        deliver(ex, [sizes[i] for i in missing])
    return ex


def run_sketch(missing, sizes, capacity, rng):
    ex = Exchange()
    ex.send(WIRE["sr_envelope"] + WIRE["sr_checksum"] + WIRE["short_id"] * capacity)
    d = len(missing)
    if d == 0:
        return ex
    if d <= capacity:
        ex.send(WIRE["sr_envelope"] + WIRE["short_id"] * d, round_trip=True)
        deliver(ex, [sizes[i] for i in missing])
        return ex

    # Over capacity, decoding usually fails outright - but roughly 1/c! of the syndrome
    # space belongs to a small set, so it can instead return a wrong set that re-encodes
    # correctly. Only the checksum catches that, after the wrong objects were fetched.
    if rng.random() < 1.0 / math.factorial(min(capacity, 20)):
        wrong = rng.sample(range(len(sizes)), min(capacity, len(sizes)))
        ex.send(WIRE["sr_envelope"] + WIRE["short_id"] * len(wrong), round_trip=True)
        deliver(ex, [sizes[i] for i in wrong])

    ex.absorb(run_enum(missing, sizes, WIRE["short_id"]))
    return ex


SMALL_CAPACITY = 4  # 16 B of sketch, the routine advert


def run_sketch_adaptive(missing, sizes, rng):
    """Advertise a small sketch; grow it only when the advert says growing is needed.

    A full-capacity sketch on every advert is what makes the steady state expensive, and
    the steady state is almost always d=0. So the routine advert carries capacity 4 and
    the member count. If the sketches do not resolve, the count difference is a lower
    bound on the divergence - that is what sr_count is for - so one directed request gets
    a sketch sized to it. Capacity truncation is exact, so the small sketch is a prefix
    of the large one and nothing already sent is wasted.
    """
    ex = Exchange()
    ex.send(
        WIRE["sr_envelope"] + WIRE["sr_checksum"] + WIRE["short_id"] * SMALL_CAPACITY
    )
    d = len(missing)
    if d == 0:
        return ex
    if d <= SMALL_CAPACITY:
        ex.send(WIRE["sr_envelope"] + WIRE["short_id"] * d, round_trip=True)
        deliver(ex, [sizes[i] for i in missing])
        return ex

    # Ask for a bigger sketch, sized from the count difference the advert carried.
    capacity = min(max(d, SMALL_CAPACITY), MAX_CAPACITY)
    ex.send(WIRE["sr_envelope"], round_trip=True)
    ex.send(WIRE["sr_envelope"] + WIRE["sr_checksum"] + WIRE["short_id"] * capacity)
    ex.send(WIRE["sr_envelope"] + WIRE["short_id"] * d, round_trip=True)
    deliver(ex, [sizes[i] for i in missing])
    return ex


STRATEGIES = ("chain", "enum32", "enum4", "sketch", "adaptive")


def trial(n, d, size_pool, capacity, rng):
    sizes = [rng.choice(size_pool) for _ in range(n)]
    missing = sorted(rng.sample(range(n), d))
    return {
        "chain": run_chain(missing, sizes),
        "enum32": run_enum(missing, sizes, WIRE["object_id"]),
        "enum4": run_enum(missing, sizes, WIRE["short_id"]),
        "sketch": run_sketch(missing, sizes, capacity, rng),
        "adaptive": run_sketch_adaptive(missing, sizes, rng),
    }


def sweep(n, size_pool, trials, capacity_rule, rng):
    out = {
        k: {m: [] for m in ("bytes", "packets", "round_trips", "airtime")}
        for k in STRATEGIES
    }
    for d in range(0, n + 1):
        runs = [trial(n, d, size_pool, capacity_rule(d), rng) for _ in range(trials)]
        for key in STRATEGIES:
            for metric in ("bytes", "packets", "round_trips", "airtime"):
                out[key][metric].append(
                    statistics.mean(getattr(r[key], metric) for r in runs)
                )
    return out


def crossover(a, b):
    """Smallest d > 0 where curve a stops being cheaper than b. d=0 is not a contest."""
    for d in range(1, len(a)):
        if a[d] > b[d]:
            return d
    return None


def text_sizes_from(logfile):
    opener = gzip.open if logfile.endswith(".gz") else open
    sizes = []
    with opener(logfile, "rt") as f:
        for line in f:
            rec = parse_line(line.strip())
            if rec and rec["portnum"] in (1, 7):
                sizes.append(rec["packet_len"])
    return sizes


# --- Charts --------------------------------------------------------------------------

STYLE = {
    "chain": ("#B4472F", "chain walk (SFPP today)"),
    "enum32": ("#3B6EA8", "enumerate, 32 B ids"),
    "enum4": ("#7FA6D4", "enumerate, 4 B short ids"),
    "sketch": ("#2E7D5B", "sketch, capacity = d"),
    "adaptive": (
        "#7A4FA3",
        f"adaptive sketch (advert c={SMALL_CAPACITY}, grown on demand)",
    ),
}


def chart_cost(res, n, outdir):
    fig, axes = plt.subplots(1, 3, figsize=(15.5, 4.8))
    xs = range(n + 1)

    for ax, metric, title, ylabel in (
        (axes[0], "bytes", "Bytes on air", "bytes"),
        (axes[1], "round_trips", "Sequential round trips", "round trips"),
        (axes[2], "airtime", f"Airtime at {PRESET}", "seconds"),
    ):
        for key in STRATEGIES:
            color, label = STYLE[key]
            ax.plot(xs, res[key][metric], color=color, lw=1.8, label=label)
        ax.set_title(title, fontsize=11)
        ax.set_xlabel(f"objects missing out of {n}")
        ax.set_ylabel(ylabel)
        ax.grid(alpha=0.25, lw=0.6)

    x = crossover(res["sketch"]["bytes"], res["enum4"]["bytes"])
    if x:
        axes[0].axvline(x, color="#666", ls=":", lw=1)
        axes[0].annotate(
            f"short-id enumeration\ncheaper from d={x}",
            xy=(x, max(res["chain"]["bytes"]) * 0.55),
            xytext=(6, 0),
            textcoords="offset points",
            fontsize=8,
            color="#333",
        )
    axes[0].legend(fontsize=8, frameon=False)
    fig.suptitle(
        f"Catch-up cost against divergence, bucket of {n}, object sizes from a real capture",
        fontsize=12,
    )
    fig.tight_layout()
    path = os.path.join(outdir, "cost-vs-divergence.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def chart_identification(res, n, outdir):
    """Objects cost the same whoever names them. Only the naming differs."""
    fig, ax = plt.subplots(figsize=(8, 4.8))
    xs = list(range(n + 1))
    obj = [res["enum4"]["bytes"][d] - res["enum4"]["bytes"][0] for d in xs]

    for key in STRATEGIES:
        color, label = STYLE[key]
        ident = [res[key]["bytes"][d] - obj[d] for d in xs]
        ax.plot(xs, ident, color=color, lw=1.8, label=label)

    ax.set_title("Identification cost with object transfer removed", fontsize=11)
    ax.set_xlabel(f"objects missing out of {n}")
    ax.set_ylabel("bytes")
    ax.grid(alpha=0.25, lw=0.6)
    ax.legend(fontsize=8, frameon=False)
    fig.tight_layout()
    path = os.path.join(outdir, "identification-cost.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def chart_capacity(size_pool, n, trials, outdir, rng):
    """Capacity is chosen before the divergence is known. What does guessing cost?"""
    fig, ax = plt.subplots(figsize=(8, 4.8))
    for cap in (4, 8, 16, 32):
        costs = []
        for d in range(0, n + 1):
            runs = [
                run_sketch(
                    sorted(rng.sample(range(n), d)),
                    [rng.choice(size_pool) for _ in range(n)],
                    cap,
                    rng,
                )
                for _ in range(max(trials // 4, 1))
            ]
            costs.append(statistics.mean(r.bytes for r in runs))
        ax.plot(
            range(n + 1),
            costs,
            lw=1.7,
            label=f"capacity {cap} ({4 * cap} B advert payload)",
        )

    ax.set_title("Sizing a sketch before the difference is known", fontsize=11)
    ax.set_xlabel(f"objects missing out of {n}")
    ax.set_ylabel("bytes on air")
    ax.grid(alpha=0.25, lw=0.6)
    ax.legend(fontsize=8, frameon=False)
    fig.tight_layout()
    path = os.path.join(outdir, "capacity-choice.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def chart_breakeven(res, n, outdir):
    """Advertising is the cost this design *adds*; resolution is what it saves.

    SFPP already broadcasts CANON_ANNOUNCE on a cadence whether or not anything changed.
    An advert carrying a sketch is roughly four times that size, so in a steady state
    where nothing is ever missed this design is strictly worse. It pays for itself the
    moment a node actually has to catch up - the question is how often that has to
    happen, and the answer is the break-even below.
    """
    announce = 40  # CANON_ANNOUNCE: type, root hash, commit hash
    advert = WIRE["sr_envelope"] + WIRE["sr_checksum"] + WIRE["short_id"] * MAX_CAPACITY
    per_hour = 12  # the module's 5-minute default cadence

    fig, axes = plt.subplots(1, 2, figsize=(13, 4.8))
    xs = list(range(0, n + 1))

    for ax, cadence, title in (
        (axes[0], per_hour, "at SFPP's 5-minute cadence (12/hour)"),
        (axes[1], 1, "at one advert per hour"),
    ):
        small_advert = (
            WIRE["sr_envelope"]
            + WIRE["sr_checksum"]
            + WIRE["short_id"] * SMALL_CAPACITY
        )
        today = [cadence * announce + res["chain"]["bytes"][d] for d in xs]
        sketch = [cadence * advert + res["sketch"]["bytes"][d] for d in xs]
        enumerated = [cadence * announce + res["enum4"]["bytes"][d] for d in xs]
        adaptive = [cadence * small_advert + res["adaptive"]["bytes"][d] for d in xs]

        ax.plot(
            xs,
            today,
            color=STYLE["chain"][0],
            lw=1.8,
            label="announce + chain walk (today)",
        )
        # Dashed so the adaptive sketch underneath stays visible: the two cost nearly the
        # same at this bucket size, which is the result rather than a plotting accident.
        ax.plot(
            xs,
            enumerated,
            color=STYLE["enum4"][0],
            lw=2.6,
            ls=(0, (5, 4)),
            label="announce + short-id enumeration",
        )
        ax.plot(
            xs,
            sketch,
            color=STYLE["sketch"][0],
            lw=1.8,
            label=f"full sketch advert ({advert} B) every time",
        )
        ax.plot(
            xs,
            adaptive,
            color=STYLE["adaptive"][0],
            lw=1.8,
            label=f"adaptive: {small_advert} B advert, grown on demand",
        )

        # "Just send it again", on the same axes. If d objects are missed per hour at a
        # per-transmission miss rate q, the underlying message rate is d/q, so the extra
        # copies cost (k-1) * (d/q) * object. Only the extra copies count: the first
        # transmission happens under every strategy.
        #
        # This flatters repetition and the chart says so: k copies leave q^k of messages
        # unrecovered for good, while every other curve here converges. It is a cheaper
        # mechanism for a weaker guarantee.
        for copies, colour in ((2, "#C77B58"), (3, "#8C5A3B")):
            repeat = [(copies - 1) * (d / MISS_RATE) * MEAN_OBJECT for d in xs]
            ax.plot(
                xs,
                repeat,
                color=colour,
                lw=1.5,
                ls=(0, (1, 2)),
                label=f"just send it {copies}x (leaves {MISS_RATE ** copies:.1%} lost)",
            )

        repeat2 = [(2 - 1) * (d / MISS_RATE) * MEAN_OBJECT for d in xs]
        cross = next((d for d in xs[1:] if repeat2[d] > adaptive[d]), None)
        if cross is not None:
            ax.axvline(cross, color="#666", ls=":", lw=1)
            ax.annotate(
                f"below d={cross}, just repeating\nthe message is cheaper",
                xy=(cross, 260),
                xytext=(8, 0),
                textcoords="offset points",
                fontsize=8,
                color="#333",
            )

        # 50% utilisation is roughly thirty times the top of these curves, so it only
        # reads on a log axis - which is the point: none of these saturate the channel,
        # and the differences between them are ratios rather than absolute bytes.
        util = radio.bytes_per_hour_at_utilisation(0.5, 67, PRESET)
        ax.axhline(util, color="#444", lw=1.3, ls=(0, (6, 3)))
        ax.annotate(
            f"50% channel utilisation ({util:,.0f} B/h)",
            xy=(xs[-1], util),
            xytext=(-6, 6),
            textcoords="offset points",
            ha="right",
            fontsize=7.5,
            color="#444",
        )
        ax.set_yscale("log")
        ax.set_ylim(200, util * 2.2)
        ax.set_title(title, fontsize=11)
        ax.set_xlabel("objects missed per hour")
        ax.set_ylabel("bytes per hour")
        ax.grid(alpha=0.25, lw=0.6, which="both")
        ax.legend(fontsize=8, frameon=False, loc="lower right")

    fig.suptitle(
        f"Break-even: a {advert} B advert against a {announce} B announce, against simply "
        f"sending each message again (miss rate {MISS_RATE:.0%})",
        fontsize=12,
    )
    fig.tight_layout()
    path = os.path.join(outdir, "break-even.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path, {
        "advert_bytes": advert,
        "announce_bytes": announce,
        "break_even_missed_per_hour_at_12": next(
            (
                d
                for d in xs
                if 12 * advert + res["sketch"]["bytes"][d]
                <= 12 * announce + res["chain"]["bytes"][d]
            ),
            None,
        ),
        "break_even_missed_per_hour_at_1": next(
            (
                d
                for d in xs
                if advert + res["sketch"]["bytes"][d]
                <= announce + res["chain"]["bytes"][d]
            ),
            None,
        ),
    }


def chart_bucket_size(size_pool, trials, outdir, rng):
    """The structural difference: enumeration scales with what you hold, a sketch with
    what you differ by. At the frozen bucket of 32 the two are nearly tied, which makes
    the bucket size the parameter that decides whether the sketch is worth having.
    """
    sizes_n = [8, 16, 32, 64, 128, 256]
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.8))

    for ax, d in zip(axes, (2, 8)):
        curves = {"enum32": [], "enum4": [], "adaptive": []}
        for n in sizes_n:
            if d > n:
                for k in curves:
                    curves[k].append(float("nan"))
                continue
            runs = [
                trial(n, d, size_pool, min(max(d, 1), MAX_CAPACITY), rng)
                for _ in range(max(trials // 4, 1))
            ]
        # The pool's mean, not its first entry: [size_pool[0]] * d is a constant that ignores
            # the runs it is nominally averaged over, and shifts every curve by however far the
            # capture's first text packet sits from the true mean.
            objects = d * (statistics.mean(size_pool) + WIRE["sfpp_overhead"])
            for k in curves:
                curves[k].append(statistics.mean(r[k].bytes for r in runs) - objects)

        for k, vals in curves.items():
            ax.plot(
                sizes_n,
                vals,
                color=STYLE[k][0],
                lw=1.8,
                marker="o",
                ms=4,
                label=STYLE[k][1],
            )
        ax.axvline(BUCKET_OBJECTS, color="#666", ls=":", lw=1)
        ax.annotate(
            "frozen bucket = 32",
            xy=(BUCKET_OBJECTS, ax.get_ylim()[1] * 0.9),
            xytext=(6, 0),
            textcoords="offset points",
            fontsize=8,
            color="#333",
        )
        ax.set_xscale("log", base=2)
        ax.set_title(f"{d} objects missing", fontsize=11)
        ax.set_xlabel("bucket size (objects held)")
        ax.set_ylabel("identification bytes")
        ax.grid(alpha=0.25, lw=0.6)
        ax.legend(fontsize=8, frameon=False)

    fig.suptitle(
        "Identification cost against bucket size, objects excluded", fontsize=12
    )
    fig.tight_layout()
    path = os.path.join(outdir, "bucket-size.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--packetlog", required=True)
    ap.add_argument("--trials", type=int, default=200)
    ap.add_argument("--bucket", type=int, default=BUCKET_OBJECTS)
    ap.add_argument("--outdir", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--json-out")
    args = ap.parse_args()
    # Charts are written straight into it, so a directory that is only named must be made.
    os.makedirs(args.outdir, exist_ok=True)

    rng = random.Random(20260816)
    size_pool = text_sizes_from(args.packetlog)
    if not size_pool:
        sys.exit("no text messages in the capture")

    n = args.bucket
    res = sweep(n, size_pool, args.trials, lambda d: min(max(d, 1), MAX_CAPACITY), rng)

    breakeven_path, breakeven = chart_breakeven(res, n, args.outdir)
    charts = [
        chart_cost(res, n, args.outdir),
        chart_identification(res, n, args.outdir),
        chart_capacity(size_pool, n, args.trials, args.outdir, rng),
        chart_bucket_size(size_pool, args.trials, args.outdir, rng),
        breakeven_path,
    ]

    summary = {
        "text_objects_sampled": len(size_pool),
        "mean_object_bytes": round(statistics.mean(size_pool), 1),
        "bucket": n,
        "trials_per_point": args.trials,
        "bytes": {
            d: {k: round(res[k]["bytes"][d]) for k in STRATEGIES}
            for d in (1, 2, 4, 8, 16, 32)
        },
        "round_trips": {
            d: {k: round(res[k]["round_trips"][d], 1) for k in STRATEGIES}
            for d in (1, 4, 16)
        },
        "airtime_s": {
            d: {k: round(res[k]["airtime"][d], 1) for k in STRATEGIES}
            for d in (1, 4, 16)
        },
        # None means the sketch is never overtaken anywhere in the bucket - which holds
        # only because capacity is sized to the divergence here. See capacity-choice.png
        # for what a fixed capacity costs when the guess is wrong.
        "crossovers_within_bucket": {
            "sketch_vs_enum4": crossover(res["sketch"]["bytes"], res["enum4"]["bytes"]),
            "sketch_vs_enum32": crossover(
                res["sketch"]["bytes"], res["enum32"]["bytes"]
            ),
            "sketch_vs_chain": crossover(res["sketch"]["bytes"], res["chain"]["bytes"]),
            "enum4_vs_chain": crossover(res["enum4"]["bytes"], res["chain"]["bytes"]),
        },
        "steady_state_break_even": breakeven,
        "charts": [os.path.basename(c) for c in charts],
    }
    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump({"summary": summary, "curves": res}, f)
    json.dump(summary, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
