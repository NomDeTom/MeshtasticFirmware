"""Predefined sweeps over the campaign's arms, one block at a time.

Seeds are drawn once per block and shared by every cell in it, so a topology and a traffic schedule
are held fixed while one arm moves. An unpaired comparison across a sweep this noisy would mostly
measure which seed drew a better-connected mesh.

Usage, from sim/:
    python3 -m sfpp.sweep --block D-cadence --out <dir>
    python3 -m sfpp.sweep --list
"""

import argparse
import copy
import json
import os
import random
import statistics
import time
from functools import lru_cache

from . import autochart as AC
from . import report as RP
from .campaign import build_parser, run_once

# Everything the protocol blocks hold fixed. Servers sit two hops apart, comfortably inside the
# default hop limit, so an advert reaches its peers and the arm under test is what varies.
BASE = [
    "--hours",
    "36",
    "--nodes",
    "60",
    "--place",
    "hops-apart",
    "--hops-apart",
    "2",
    "--servers",
    "3",
    "--capacity",
    "32",
    # Bucket-close, on the D-cadence evidence: it holds more than a five-minute interval for a
    # fourteenth of the airtime, so every later block measures the design as it should be run.
    "--trigger",
    "bucket",
    # The numbering the firmware actually does. A shared counter cannot exist, so no result about
    # bucket agreement is meaningful without this.
    "--bucket-mode",
    "local",
    "--resolve",
    "hybrid",
]

# What a retry-ladder arm needs before it does anything. An addressed message only exists once SR
# traffic is routed through the transport, and a route is only learned once traceroutes seed one, so
# an arm over --dm-mode or --coding-rate-ladder without these produces identical rows.
DM_LADDER = ["--dm-transport", "transport", "--traceroute-per-hour", "1"]

BLOCKS = {
    "D-cadence": ("trigger", ["bucket", "interval", "aimd", "bucket+interval"], []),
    "D-resolve": ("resolve", ["sketch", "enum", "hybrid"], []),
    # Every server seals the same bucket at nearly the same moment, because sealing follows the
    # chain counter and the counter is global. If that synchronisation is why only 46% of
    # bucket-close adverts reach a peer, spreading them should show it.
    "D-jitter": ("advert-jitter-s", [1, 30, 120, 600], []),
    "E-capacity": ("capacity", [4, 8, 16, 32, 50], []),
    "E-width": ("short-id-bits", [16, 24, 32, 64], []),
    "E-signed": ("signed", [False, True], []),
    "F-loss": ("extra-loss", [0.0, 0.1, 0.2, 0.3], []),
    # Same nominal loss, delivered in 60-second stretches of deafness rather than spread evenly.
    # A sketch cares about the difference: flat loss spreads divergence across every bucket, a
    # burst puts a whole bucket's worth into one and can push it past the capacity in a single go.
    "F-burst": ("burst-loss", [0.0, 0.1, 0.2, 0.3], []),
    # A 60-second burst is nothing to a bucket that takes an hour to fill. This is the outage that
    # actually matters to an archive: a node away for half an hour, which is most of a bucket.
    "F-outage": ("burst-loss", [0.0, 0.1, 0.2, 0.3], ["--burst-ms", "1800000"]),
    "G-place": (
        "place",
        [
            "spread",
            "routers",
            "alternate-routers",
            "beside-router",
            "random-clients",
            "hops-apart",
        ],
        [],
    ),
    "G-hops": ("hops-apart", [1, 2, 3, 4], ["--place", "hops-apart"]),
    "G-servers": ("servers", [2, 3, 5, 8], []),
    # There is no canonical counter, so `local` is what the firmware does and `global` is a fiction
    # kept only as an upper bound. `time` and `window` are the two candidates needing no agreement.
    "J-bucketmode": ("bucket-mode", ["global", "local", "time", "window"], []),
    "J-window": ("window-size", [8, 16, 32], ["--bucket-mode", "window"]),
    "J-wincap": ("capacity", [8, 16, 32], ["--bucket-mode", "window"]),
    "J-timewin": ("time-bucket-s", [600, 1800, 3600], ["--bucket-mode", "time"]),
    # Mesh size, with per-node hop limits 3-7 by centrality rather than one value for everyone.
    # Size with density held constant - the area grows with the node count.
    "K-size": ("nodes", [40, 60, 90, 120, 150], ["--hop-spread", "--scale-area"]),
    # The same node counts in a fixed area, so this one is density rather than size. Running both is
    # the only way to say which of the two any effect belongs to.
    "K-density": ("nodes", [40, 60, 90, 120, 150], ["--hop-spread"]),
    # One hop limit for everyone, swept. --no-hop-spread is not optional here: with the spread on,
    # every node takes a per-node limit from centrality and --hop-limit is never read, which made
    # this block produce three identical rows.
    "K-hopspread": ("hop-limit", [3, 5, 7], ["--no-hop-spread"]),
    # Uniform hop limit against per-node 3-7 by centrality, everything else fixed.
    "K-spread": ("hop-spread", [False, True], []),
    # Adverts only other archives can act on; replays every node in earshot can use.
    "L-advert": ("advert-transport", ["broadcast", "dm"], []),
    "L-provide": ("provide-transport", ["dm", "broadcast"], []),
    # Filing a replay by its heard_ago rather than at the receiving tip. The bucket it came from can
    # only converge with the peer's if the object lands where it belongs.
    "M-replayorder": ("replay-ordering", ["tip", "heard"], []),
    # The same, with replays broadcast so bystanders can file them too - the combination the replay
    # header exists for.
    "M-combined": (
        "replay-ordering",
        ["tip", "heard"],
        ["--provide-transport", "broadcast"],
    ),
    # Spreading adverts in time. Under local numbering each server seals its own bucket whenever its
    # own 32nd message lands, so the synchronisation jitter would break is largely absent.
    "M-jitter": ("advert-jitter-s", [1, 30, 120, 600], []),
    "M-capacity": ("capacity", [4, 8, 16, 32, 50], []),
    # Placement under real numbering and per-node hop limits.
    "N-place": (
        "place",
        [
            "spread",
            "routers",
            "alternate-routers",
            "beside-router",
            "random-clients",
            "hops-apart",
        ],
        ["--hop-spread"],
    ),
    "N-hops": (
        "hops-apart",
        [1, 2, 3, 4, 5],
        ["--place", "hops-apart", "--hop-spread"],
    ),
    "N-servers": ("servers", [2, 3, 5, 8], ["--hop-spread"]),
    # Time of day. Text and position follow the clock; device timers do not.
    "P-diurnal": ("diurnal", ["flat", "sinusoid", "commuter"], []),
    # The catch-up window, which only means anything once traffic has a time of day.
    "P-catchup": (
        "catch-up-hours",
        ["", "02-06", "00-08"],
        ["--diurnal", "commuter", "--trigger", "bucket+interval"],
    ),
    # Slow presets scale device intervals far harder, so an archive should be cheaper to run there.
    "P-preset": ("preset", ["SHORT_FAST", "LONG_FAST", "LONG_SLOW"], []),
    # Congestion scaling on against off, to size what the firmware's own throttling is worth.
    "P-congestion": ("no-congestion-scaling", [False, True], ["--nodes", "120"]),
    # Nothing, the incumbent chain walk, and the sketch - all at one seed, so `none`
    # is a paired baseline and every other cell is a difference rather than a comparison.
    "Q-protocol": ("protocol", ["none", "chain", "sr"], []),
    # The designated-node control: the same nodes in the same places, archive off then on, so what
    # serving costs a node and what reconciliation adds can be separated from where it sits.
    "Q-control": (
        "protocol",
        ["none", "sr"],
        ["--place", "hops-apart", "--hops-apart", "3"],
    ),
    # The denominator: an SF++ airtime share is a share of whatever the mesh broadcasts anyway, so
    # the device interval decides it as much as the protocol does.
    "Q-interval": ("broadcast-interval-s", [900, 3600, 10800, 43200], []),
    # centrality is what operators do; random is the control that separates the hop limit's own
    # effect from the siting of the nodes that happen to have raised it.
    "Q-hopassign": ("hop-assign", ["centrality", "random"], []),
    "Q-topology": (
        "topology",
        ["uniform", "clustered", "corridor", "hub"],
        [],
    ),
    # All six routers as servers, against three of them, against three nodes beside them. Same
    # mesh, same traffic; only who is holding the archive changes.
    "G-allrouters": ("servers", [3, 6], ["--place", "routers"]),
    # What the 2.8 fold-in is worth against the pre-fold-in transport: same seed, same mesh, same
    # traffic, only the MAC and routing rules change.
    # --- round four: stress past the node database, and emit tuning numbers ---
    # Mesh size against the store that has to hold it. The diagonal is where they match; every cell
    # above it is the stressed case the firmware's throttle cannot see.
    "R-oversubscribed": (
        "nodes",
        [120, 250, 500],
        ["--scale-area", "--hours", "24"],
    ),
    # The same node counts against a deliberately small store, so eviction is constant.
    "R-hotstore-stress": (
        "max-num-nodes",
        [10, 120, 250],
        ["--nodes", "250", "--scale-area", "--hours", "24"],
    ),
    # Which quantity should drive the throttle. hotstore saturates; truesize is the ideal ceiling.
    "R-congestion-input": (
        "congestion-input",
        ["hotstore", "truesize"],
        ["--nodes", "250", "--scale-area", "--hours", "24"],
    ),
    # How many retries an addressed reconciliation hop needs before delivery stops improving. Named
    # for the SF++ retry budget rather than R-repeats, which is the firmware's RepeatScalingModule.
    "R-srretries": ("sr-retries", [0, 1, 2, 4], ["--hours", "24"]),
    "R-firmware": ("profile", ["legacy", "2.8"], []),
    # The feedback loop closed against the same loop held open: every node adopting a hop
    # recommendation derived from a histogram of what other adopting nodes sent. Needs the trace,
    # since a converged mean and an oscillating one are identical at the end of a run.
    "R-adopt": (
        "no-adopt-hop-recommendation",
        [False, True],
        ["--nodes", "120", "--hop-spread", "--trace-interval-s", "1800"],
    ),
    # Where the nodes physically are. The spread between a roof node and a basement one is 26 dB,
    # wider than most parameters here, so this is either the largest uncontrolled variable in every
    # other block or the one that says the others were measured on an unrealistically good mesh.
    "R-siting": (
        "siting-mix",
        ["uniform", "local-typical", "event", "backbone"],
        [],
    ),
    # How far the firmware's estimator sits from the exhaustive count it approximates, as the mesh
    # outgrows its 128 entries. Reported truth / observed / estimated side by side.
    "R-hopscale": ("nodes", [60, 120, 250, 500], ["--scale-area", "--hop-spread"]),
    # Does traceroute learning pay for its own airtime? Each reply teaches a route for every node
    # beyond the learner, and this tree's corroboration guard refuses a share of what it is told.
    "R-traceroute": ("traceroute-per-hour", [0.0, 0.25, 1.0, 4.0], []),
    # The same, on a mesh whose hot store cannot hold it - where the overflow cache is the only
    # thing that can keep a route for the long tail.
    "R-traceroute-small": (
        "traceroute-per-hour",
        [0.0, 1.0],
        ["--nodes", "120", "--max-num-nodes", "20"],
    ),
    # The retry budget from both ends: M4 spends a directed attempt to flood sooner, the coding-rate
    # ladder spends airtime to make each attempt more likely to land. Swept together because they
    # trade against the same budget.
    "R-dmmode": (
        "dm-mode",
        ["flood-only", "directed-with-late-flood", "m4-early-flood"],
        DM_LADDER,
    ),
    "R-crladder": ("coding-rate-ladder", [False, True], DM_LADDER),
    "R-dmmode-cr": (
        "dm-mode",
        ["directed-with-late-flood", "m4-early-flood"],
        DM_LADDER + ["--coding-rate-ladder"],
    ),
    # The cheapest rival to the archive: spend one extra relay of a text rather than replicate it
    # afterwards. Measured against the archive in the same arm rather than separately.
    "R-repeats": ("extra-repeats", [False, True], []),
    # The same, on a mesh busy enough for the suppression thresholds to be deciding it.
    "R-repeats-busy": ("extra-repeats", [False, True], ["--nodes", "120"]),
    # 64 bytes on every signable broadcast, against the reliability that buys. Report the share of
    # signable traffic that was actually signed rather than assuming all of it was.
    "R-signing": ("signature-policy", ["COMPATIBLE", "BALANCED", "STRICT"], []),
    # --signature-policy is a receive rule and never decides whether we sign, so it cannot price
    # signing itself. Router.cpp signs every non-PKI broadcast, which puts 66 bytes on the packet
    # and on each of its rebroadcasts; at LONG_FAST that is most of a doubling of channel airtime.
    # This is the only arm that turns it off inside 2.8, and so the only way to tell "2.8 costs
    # reach" apart from "signing costs reach".
    "R-signing-cost": (
        "profile-flag",
        ["signing=false", "signing=true"],
        ["--profile", "2.8"],
    ),
    # Each node throttling on its own online count, against one coefficient for the whole mesh. The
    # firmware does the former; every figure measured here before did the latter.
    "R-congestion-mode": ("congestion-mode", ["static", "adaptive"], ["--nodes", "120"]),
    # What the warm tier is worth on a mesh larger than the hot store: 0 is the pre-2.8 behaviour
    # of forgetting an evicted peer outright, and the rest is how much identity a node keeps.
    "R-warm": (
        "warm-num-nodes",
        [0, 25, 100, 2000],
        ["--nodes", "120", "--max-num-nodes", "20"],
    ),
    # The release series in order, each at its final release. Steps the whole rule set at once -
    # contention window, roles, queue order, hop preservation, next-hop, store size and the
    # congestion throttle - so it says what a mesh gained or lost per upgrade rather than what one
    # rule is worth.
    "R-versions": ("profile", ["2.4", "2.5", "2.6", "2.7", "2.8"], []),
    # A mesh that has not finished upgrading. The share below runs 2.6 while the rest run 2.8, which
    # is the case the release notes never describe.
    "R-mixed": (
        "legacy-fraction",
        [0.0, 0.25, 0.5, 0.75],
        ["--old-profile", "2.5"],
    ),
    # The same, one series later: 2.6 already has next-hop routing, so this separates "some nodes
    # cannot route" from "some nodes cannot learn a route".
    "R-mixed-26": (
        "legacy-fraction",
        [0.0, 0.25, 0.5, 0.75],
        ["--old-profile", "2.6"],
    ),
    # The roles 2.8 added. ROUTER_LATE only speaks when the mesh still needs it, so promoting the
    # spine to it should cut relay airtime without costing reach - which is the claim to test.
    "R-routerlate": ("router-late-fraction", [0.0, 0.05, 0.1, 0.2], []),
    # A hop between two favourited routers is free in 2.8. On a mesh whose diameter already
    # exceeds the hop limit, that is the difference between reaching the far end and not.
    "R-favourites": ("favourite-routers", [False, True], ["--router-fraction", "0.15"]),
    # The hot store is per-board and small, and everything routing knows is bounded by it. Run with
    # its consumers engaged - hop preservation and a rebroadcast mode that consults the NodeDB -
    # because with rebroadcast ALL and no favourites nothing reads the store and every mix ties.
    "R-platform": (
        "platform-mix",
        ["uniform", "baymesh-2026-08", "constrained"],
        ["--favourite-routers", "--router-fraction", "0.2"],
    ),
    # The same question as one number rather than a board mix, so the trend is readable: 10 is an
    # STM32WL, 120 the nRF52840 default, 250 a 16 MB S3. A 60-node mesh does not fit in the first.
    "R-hotstore": (
        "max-num-nodes",
        [10, 100, 120, 250],
        ["--favourite-routers", "--router-fraction", "0.2"],
    ),
    # Measured role shares - 4% ROUTER, 3% ROUTER_LATE, 16% CLIENT_BASE, 18% CLIENT_MUTE - against
    # the 10%-ROUTER default. Run with and without favourites, which decides the sign of the effect.
    "R-roles": (
        "role-mix",
        ["legacy-default", "baymesh-2026-08"],
        [],
    ),
    "R-roles-fav": (
        "role-mix",
        ["legacy-default", "baymesh-2026-08"],
        ["--favourite-routers"],
    ),
    # What a restrictive rebroadcast mode costs when the store is too small to remember who is who.
    "R-rebroadcast": (
        "rebroadcast-mode",
        ["ALL", "KNOWN_ONLY", "CORE_PORTNUMS_ONLY"],
        ["--platform-mix", "baymesh-2026-08"],
    ),
}


def cell_argv(arm, value, extra):
    """The command line for one cell of a block.

    A false arm cannot simply omit its flag. Omitting it lands on the parser's default, and a flag
    that defaults to true - `--hop-spread` is one - then reads as true in both cells, so the block
    produces two identical rows. Where the parser offers the negation, emit it; where it does not,
    say so rather than quietly measuring the same thing twice.
    """
    argv = list(BASE) + list(extra)
    if isinstance(value, bool):
        if value:
            argv.append(f"--{arm}")
        elif _flag_default(arm) is True:
            negation = f"--no-{arm}"
            if negation not in _known_flags():
                raise ValueError(
                    f"--{arm} defaults to true and has no {negation}; "
                    f"a false arm cannot be expressed and would repeat the true one"
                )
            argv.append(negation)
    else:
        argv += [f"--{arm}", str(value)]
    return argv


@lru_cache(maxsize=1)
def _known_flags():
    return {opt for a in build_parser()._actions for opt in a.option_strings}


@lru_cache(maxsize=None)
def _flag_default(arm):
    for action in build_parser()._actions:
        if f"--{arm}" in action.option_strings:
            return action.default
    return None


# Named groups, so a batch can be launched by what it asks rather than by remembering which block
# names belong together. Ordered cheap-to-expensive within each, so results accumulate rather than
# waiting on the largest mesh in the group.
BATCHES = {
    # Which release a mesh runs, and what a half-upgraded one costs.
    "versions": ["R-firmware", "R-versions", "R-mixed", "R-mixed-26"],
    # The hop limit as an operator actually sets it: one value for everyone, against per-node
    # limits, against nodes choosing their own from the histogram.
    "hops": ["K-hopspread", "K-spread", "Q-hopassign", "R-adopt"],
    # The archive itself: off, the incumbent walk, the sketch.
    "protocol": ["Q-protocol", "Q-control"],
    # The two unreleased mechanisms, each against its own control.
    "unreleased": ["R-crladder", "R-repeats", "R-repeats-busy", "R-dmmode", "R-dmmode-cr"],
    # One 2.8 mechanism per block, each with the conditions that make it do anything.
    "mechanisms": [
        "R-favourites",
        "R-routerlate",
        "R-roles",
        "R-signing",
        "R-signing-cost",
        "R-rebroadcast",
        "R-congestion-mode",
        "R-warm",
        "R-traceroute",
        "R-traceroute-small",
    ],
    # What the mesh is made of and where it sits, which bounds every other block.
    "shape": ["R-platform", "R-siting", "Q-topology", "K-density", "K-size"],
    # Past the hot store, where the NodeDB stops being able to hold the mesh.
    "scale": ["R-hotstore", "R-hopscale", "R-hotstore-stress", "R-oversubscribed"],
    # Offered load and the clock, which set the denominator every share is quoted against.
    "load": ["Q-interval", "P-diurnal", "P-preset", "P-congestion", "P-catchup"],
}


def run_block(name, seeds, out_dir, grid=None):
    arm, values, extra = BLOCKS[name]
    parser = build_parser()
    results = []
    for value in values:
        for seed in seeds:
            argv = cell_argv(arm, value, extra)
            if grid:
                argv += grid
            opts = parser.parse_args(argv)
            started = time.time()
            report = run_once(opts, seed)
            report["block"] = name
            report["arm"] = arm
            report["value"] = value
            report["grid"] = grid or []
            results.append(report)
            print(
                f"  {name} {arm}={value} seed={seed} {time.time() - started:.0f}s",
                flush=True,
            )
            line(report)
    suffix = ""
    if grid:
        # A grid run is a different experiment, not a rerun of the same one, so it gets its own
        # file. Without this the second capacity in a capacity-by-loss sweep overwrites the first.
        suffix = "-" + "-".join(g.lstrip("-") for g in grid).replace(" ", "")
    path = os.path.join(out_dir, f"{name}{suffix}.json")
    os.makedirs(out_dir, exist_ok=True)
    with open(path, "w") as f:
        json.dump(results, f, indent=2)
    table(name, arm, values, results)
    reception_table(name, arm, values, results)
    print(f"wrote {path}")
    chart = AC.auto(results, path, kind="block")
    if chart:
        print(f"wrote {chart}")
    # The per-portnum statistics, text first, beside the table - so a block's output is readable
    # without a second command and without anyone having to remember to run one. Written to disk as
    # well as printed: an unattended run's stdout is not always kept, and the block's own directory
    # is where anyone looks afterwards.
    text = RP.report_block(path)
    print(text)
    text_path = os.path.join(out_dir, "reports", f"{name}{suffix}.txt")
    os.makedirs(os.path.dirname(text_path), exist_ok=True)
    with open(text_path, "w") as f:
        f.write(text + "\n")
    print(f"wrote {text_path}")
    return results


def line(report):
    s = report.get("sfpp")
    if not s:
        return
    print(
        f"    held {s['held_fraction_mean']:.3f} union {s['union_fraction']:.3f} "
        f"adverts {s['adverts']} moved {s['objects_moved']} "
        f"fail {s['decode_failures']} mis {s['misdecodes']} esc {s['escalations']} "
        f"SRair {s['sr_airtime_share']:.1%} silent {s['silent_losses']}/"
        f"{s['audit_checksum_agrees_sets_differ']}",
        flush=True,
    )


def table(name, arm, values, results):
    """One row per cell, averaged over the block's seeds."""
    print(f"\n=== {name} ===")
    header = (
        f"{arm:>18} | held  union | adverts moved | SRbytes  SRair | "
        f"fail mis esc | silent"
    )
    print(header)
    print("-" * len(header))
    rows = []
    for value in values:
        cells = [r for r in results if r["value"] == value and "sfpp" in r]
        if not cells:
            continue
        g = lambda k: statistics.mean(c["sfpp"][k] for c in cells)  # noqa: E731
        row = {
            "value": value,
            "held": g("held_fraction_mean"),
            "union": g("union_fraction"),
            "adverts": g("adverts"),
            "moved": g("objects_moved"),
            "sr_bytes": g("sr_bytes"),
            "sr_airtime_share": g("sr_airtime_share"),
            "decode_failures": g("decode_failures"),
            "misdecodes": g("misdecodes"),
            "escalations": g("escalations"),
            "silent": g("silent_losses") + g("audit_checksum_agrees_sets_differ"),
            "reception": statistics.mean(
                c["baseline"]["text_reception_mean"] for c in cells
            ),
        }
        rows.append(row)
        print(
            f"{str(value):>18} | {row['held']:.3f} {row['union']:.3f} | "
            f"{row['adverts']:7.0f} {row['moved']:5.0f} | "
            f"{row['sr_bytes']:7.0f} {row['sr_airtime_share']:6.1%} | "
            f"{row['decode_failures']:4.0f} {row['misdecodes']:3.0f} {row['escalations']:3.0f} | "
            f"{row['silent']:.0f}"
        )
    return rows


def reception_table(name, arm, values, results):
    """What each arm did to delivery, read at the tails rather than the mean.

    The archive table above answers whether reconciliation worked. This one answers whether the arm
    was worth having at all, which is a different question and the one most of the feature blocks
    exist for. p10 is the tenth-percentile node - the badly-placed one an arm has to help to be
    worth its airtime - and p90 the well-placed one, which most arms move very little. A change that
    lifts the mean by moving p90 has helped nobody who needed it.

    `all` is every portnum together: an arm that lifts text by displacing telemetry is a trade, not
    a gain, and only the aggregate row shows it.
    """
    print(f"\n=== {name} - reception ===")
    header = (
        f"{arm:>18} | text p10   med   p90 | all  p10   med   p90 | "
        f"util  none | txs     coll"
    )
    print(header)
    print("-" * len(header))
    rows = []
    for value in values:
        cells = [r for r in results if r["value"] == value and "by_class" in r]
        if not cells:
            continue

        def dist(cls, stat, cells=cells):
            got = [
                c["by_class"][cls]["per_node_reception"][stat]
                for c in cells
                if cls in c["by_class"]
            ]
            return statistics.mean(got) if got else float("nan")

        def traffic(key, cells=cells):
            return statistics.mean(c["traffic"][key] for c in cells)

        row = {
            "value": value,
            "text_p10": dist("text", "p10"),
            "text_median": dist("text", "median"),
            "text_p90": dist("text", "p90"),
            "all_p10": dist("all", "p10"),
            "all_median": dist("all", "median"),
            "all_p90": dist("all", "p90"),
            "channel_utilisation": traffic("channel_utilisation"),
            "nodes_receiving_none": statistics.mean(
                c["by_class"]["text"]["nodes_receiving_none"] for c in cells
            ),
            "transmissions": traffic("transmissions"),
            "lost_to_collision": traffic("lost_to_collision"),
        }
        rows.append(row)
        print(
            f"{str(value):>18} | {row['text_p10']:9.3f} {row['text_median']:5.3f} {row['text_p90']:5.3f} | "
            f"{row['all_p10']:8.3f} {row['all_median']:5.3f} {row['all_p90']:5.3f} | "
            f"{row['channel_utilisation']:5.2f} {row['nodes_receiving_none']:5.1f} | "
            f"{row['transmissions']:7.0f} {row['lost_to_collision']:8.0f}"
        )
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--block", action="append", help="repeatable; omit with --list")
    ap.add_argument(
        "--batch",
        action="append",
        help="a named group of blocks, repeatable: " + ", ".join(sorted(BATCHES)),
    )
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument(
        "--seed-base", type=int, help="omit to draw random seeds and record them"
    )
    ap.add_argument("--out", default=".")
    # A single quoted string, not nargs: argparse would read the leading "--" of the extra flags
    # as options of this parser rather than payload.
    ap.add_argument(
        "--grid",
        default=None,
        help='extra flags for every cell, quoted: --grid "--capacity 8"',
    )
    opts = ap.parse_args(argv)

    # A batch is a name for a group of blocks; --block and --batch add to the same list, so the two
    # can be mixed and a batch can be extended with one extra block on the command line.
    blocks = list(opts.block or [])
    for batch in opts.batch or []:
        if batch not in BATCHES:
            ap.error(f"unknown batch {batch!r}; known: {', '.join(sorted(BATCHES))}")
        blocks += [b for b in BATCHES[batch] if b not in blocks]

    if opts.list or not blocks:
        for name, (arm, values, grid) in BLOCKS.items():
            suffix = f"   [{' '.join(grid)}]" if grid else ""
            print(f"{name:20} {arm} = {values}{suffix}")
        print()
        for batch, names in sorted(BATCHES.items()):
            print(f"batch {batch:12} {' '.join(names)}")
        return 0

    unknown = [b for b in blocks if b not in BLOCKS]
    if unknown:
        ap.error(f"unknown block(s): {', '.join(unknown)}")

    if opts.seed_base is None:
        seeds = [random.SystemRandom().randrange(1 << 31) for _ in range(opts.seeds)]
    else:
        seeds = [opts.seed_base + i for i in range(opts.seeds)]
    print(f"seeds {seeds}")

    for name in blocks:
        run_block(name, seeds, opts.out, grid=opts.grid.split() if opts.grid else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
