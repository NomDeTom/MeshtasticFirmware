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
    "--resolve",
    "hybrid",
]

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
    # All six routers as servers, against three of them, against three nodes beside them. Same
    # mesh, same traffic; only who is holding the archive changes.
    "G-allrouters": ("servers", [3, 6], ["--place", "routers"]),
}


def cell_argv(arm, value, extra):
    argv = list(BASE) + list(extra)
    if arm == "signed":
        if value:
            argv.append("--signed")
    else:
        argv += [f"--{arm}", str(value)]
    return argv


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
    print(f"wrote {path}")
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


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--block", action="append", help="repeatable; omit with --list")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument(
        "--seed-base", type=int, help="omit to draw random seeds and record them"
    )
    ap.add_argument("--out", default=".")
    ap.add_argument(
        "--grid", nargs="*", default=None, help="extra argv appended to every cell"
    )
    opts = ap.parse_args(argv)

    if opts.list or not opts.block:
        for name, (arm, values, _) in BLOCKS.items():
            print(f"{name:14} {arm} = {values}")
        return 0

    if opts.seed_base is None:
        seeds = [random.SystemRandom().randrange(1 << 31) for _ in range(opts.seeds)]
    else:
        seeds = [opts.seed_base + i for i in range(opts.seeds)]
    print(f"seeds {seeds}")

    for name in opts.block:
        run_block(name, seeds, opts.out, grid=opts.grid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
