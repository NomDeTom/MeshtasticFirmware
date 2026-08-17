"""Markdown tables from the sweep's run JSONs, ready to paste into the write-up.

sweep.py prints a table as it goes; this reads the saved JSONs instead, so a block can be
re-tabulated without re-running it, and it carries one column the live table does not: the mesh's
own text reception in each cell. That column is the one that shows reconciliation paying for itself
in the wrong currency - a chattier cadence congests the channel the archive is fed from.

Usage, from sim/:
    python3 -m sfpp.analyse --runs <dir> [--block D-cadence]
"""

import argparse
import glob
import json
import os
import statistics


def load(path):
    with open(path) as f:
        data = json.load(f)
    return data if isinstance(data, list) else [data]


def group(reports):
    out = {}
    for report in reports:
        if "sfpp" not in report:
            continue
        out.setdefault(report.get("value", report.get("label", "")), []).append(report)
    return out


def mean(cells, section, field):
    return statistics.mean(c[section][field] for c in cells)


def block_table(path):
    reports = load(path)
    name = os.path.basename(path).replace(".json", "")
    grouped = group(reports)
    if not grouped:
        return baseline_table(path, reports)

    arm = reports[0].get("arm", "value")
    lines = [
        f"### {name} - arm `{arm}`, {len(next(iter(grouped.values())))} seeds per cell",
        "",
        f"| {arm} | held | union | mesh reception | recovered | adverts | moved | SR bytes | "
        "SR airtime | decode fail | misdecode | escalations | silent |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for value, cells in grouped.items():
        silent = mean(cells, "sfpp", "silent_losses") + mean(
            cells, "sfpp", "audit_checksum_agrees_sets_differ"
        )
        reception = mean(cells, "baseline", "text_reception_mean")
        union = mean(cells, "sfpp", "union_fraction")
        # The payoff, stated the way it matters to a client: of everything an ordinary node failed
        # to hear, how much does the archive between them actually hold?
        # Meaningless when there is no archive: the formula reads a union of zero as a catastrophic
        # loss rather than as "not applicable", and printed -193% for the baseline row.
        recovered = (
            (union - reception) / max(1e-9, 1.0 - reception) if union > 0 else None
        )
        lines.append(
            f"| {value} "
            f"| {mean(cells, 'sfpp', 'held_fraction_mean'):.3f} "
            f"| {union:.3f} "
            f"| {reception:.3f} "
            f"| {'n/a' if recovered is None else format(recovered, '.1%')} "
            f"| {mean(cells, 'sfpp', 'adverts'):.0f} "
            f"| {mean(cells, 'sfpp', 'objects_moved'):.0f} "
            f"| {mean(cells, 'sfpp', 'sr_bytes') / 1000:.1f} KB "
            f"| {mean(cells, 'sfpp', 'sr_airtime_share'):.1%} "
            f"| {mean(cells, 'sfpp', 'decode_failures'):.0f} "
            f"| {mean(cells, 'sfpp', 'misdecodes'):.0f} "
            f"| {mean(cells, 'sfpp', 'escalations'):.0f} "
            f"| {silent:.0f} |"
        )
    seeds = sorted({r["seed"] for r in reports})
    lines += ["", f"Seeds: {seeds}", ""]
    return "\n".join(lines)


def baseline_table(path, reports):
    """The no-SF++ runs have no `sfpp` section; they get their own shape."""
    lines = [
        f"### {os.path.basename(path).replace('.json', '')} - baseline, no SF++",
        "",
        "| seed | degree | util | texts | reception | ceiling | beyond hops | lost within reach |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in reports:
        b, t = r["baseline"], r["traffic"]
        lines.append(
            f"| {r['seed']} | {r['mesh']['mean_degree']:.1f} "
            f"| {t['channel_utilisation']:.0%} | {t['text_objects']} "
            f"| {b['text_reception_mean']:.3f} | {b['reach_ceiling_mean']:.3f} "
            f"| {b['missed_beyond_hop_limit']:.3f} | {b['missed_within_reach']:.3f} |"
        )
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", required=True)
    ap.add_argument("--block", action="append")
    opts = ap.parse_args(argv)

    paths = (
        [os.path.join(opts.runs, f"{b}.json") for b in opts.block]
        if opts.block
        else sorted(glob.glob(os.path.join(opts.runs, "*.json")))
    )
    for path in paths:
        if not os.path.exists(path):
            print(f"### {os.path.basename(path)} - not run yet\n")
            continue
        print(block_table(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
