r"""One scheduled sweep, read as a whole: what moved, what did nothing, and what must never happen.

`sweep.py` writes one JSON per block and prints a table per block. A scheduled run that covers every
block produces 87 of those, which is more than anyone reads, so nothing in them is looked at until
something has already gone wrong. This module reduces a whole run to two artefacts: `summary.json`,
the machine-readable digest the explorer rolls up across runs, and `trend.md`, the page a person
actually opens.

Three checks run over every cell, and they are the reason this exists rather than a prettier table:

  * `silent_losses` must be zero. A checksum closing over two unequal sets would falsify the design,
    so a single non-zero cell fails the run rather than being averaged into a column.
  * `queue_drops` against `transmissions`. A backoff cap discarding rebroadcasts silently rescales
    every airtime figure in the run; it did exactly that for three rounds before anyone noticed.
  * An arm whose cells are identical on every headline metric did nothing. That has happened twice
    to flags that were accepted, stored and never read, and both times the run produced a
    well-formed table supporting the opposite of the truth. Some flags legitimately need a second
    flag before they do anything (README §10.4), so this warns and names the arm rather than failing.

Usage, from sim/:
    python3 -m sfpp.collate --runs <dir>                     # trend.md and summary.json beside it
    python3 -m sfpp.collate --runs <dir> --out <dir> \\
        --run-id 2026-08-19 --seed-base 4711 --scenario ridge
"""

import argparse
import datetime
import glob
import json
import os
import statistics

# Everything the digest carries per cell. The key is what the explorer and the tables use; the path
# is where it lives in a campaign report. A metric absent from a report (an older transport, a
# section that only exists under some flags) becomes None rather than raising, so a run assembled
# from mixed vintages still collates.
METRICS = {
    "held": ("sfpp", "held_fraction_mean"),
    "held_min": ("sfpp", "held_fraction_min"),
    "union": ("sfpp", "union_fraction"),
    "text": ("baseline", "text_reception_mean"),
    "text_worst": ("baseline", "text_reception_min"),
    "ceiling": ("baseline", "reach_ceiling_mean"),
    "sr_airtime": ("sfpp", "sr_airtime_share"),
    "utilisation": ("traffic", "channel_utilisation"),
    "moved": ("sfpp", "objects_moved"),
    "adverts": ("sfpp", "adverts"),
    "advert_bytes": ("sfpp", "advert_bytes"),
    "sr_bytes": ("sfpp", "sr_bytes"),
    "bytes_on_air": ("traffic", "bytes_on_air"),
    "escalations": ("sfpp", "escalations"),
    "decode_failures": ("sfpp", "decode_failures"),
    "misdecodes": ("sfpp", "misdecodes"),
    "silent_losses": ("sfpp", "silent_losses"),
    "transmissions": ("traffic", "transmissions"),
    "queue_drops": ("traffic", "queue_drops"),
    "degree": ("mesh", "mean_degree"),
}

# The two the trend is read through. `held` is what the design is for; `text` is the currency it is
# paid in, and a block that buys held with text is the interesting kind of result.
HEADLINE = "held"
COST = "text"

# Cells differing by less than this on every recorded number are treated as the same cell, which is
# how an inert arm is detected. Relative, because the numbers compared span reception fractions and
# byte counters in the millions; a flag that moves a counter by one part in a billion has not moved
# it. The first version of this check compared only the metrics this module displays and called
# `E-signed` inert - the arm moves `advert_bytes` by 43%, which was not among them. Hence: every
# number in the report, not a chosen few.
INERT_EPSILON = 1e-9

# A run discarding more than this share of its rebroadcast attempts is measuring its own backoff cap.
QUEUE_DROP_WARN = 0.10


def load_block(path):
    """Every cell report in one block file, as written by sweep.run_block."""
    with open(path) as f:
        data = json.load(f)
    return data if isinstance(data, list) else [data]


def metric(report, key):
    section, field = METRICS[key]
    return (report.get(section) or {}).get(field)


def _mean(values):
    present = [v for v in values if v is not None]
    return statistics.mean(present) if present else None


def _sd(values):
    present = [v for v in values if v is not None]
    return statistics.stdev(present) if len(present) > 1 else None


def group_by_value(reports):
    """{arm value: [report per seed]}, in the order the block declares its values."""
    grouped = {}
    for r in reports:
        grouped.setdefault(str(r.get("value", "-")), []).append(r)
    return grouped


def cells_of(reports):
    """One entry per arm value, averaged over whatever seeds the run drew for it.

    Insertion order is the order sweep ran the values in, which is the order the block declares -
    so a table built from this reads in the same direction as the block's own output.
    """
    grouped = group_by_value(reports)
    cells = []
    for value, group in grouped.items():
        cell = {
            "value": value,
            "seeds": [g.get("seed") for g in group],
            "metrics": {k: _mean([metric(g, k) for g in group]) for k in METRICS},
        }
        # Only when a value was run more than once - a single-seed run has no spread, and writing
        # 0.0 there would let the explorer average a fiction into a real one later.
        spread = {k: _sd([metric(g, k) for g in group]) for k in METRICS}
        cell["sd"] = {k: v for k, v in spread.items() if v is not None}
        cells.append(cell)
    return cells


def _effect(cells, key):
    """Spread of one metric across an arm: (low, high, high - low), or None if nothing was recorded."""
    values = [c["metrics"].get(key) for c in cells]
    present = [v for v in values if v is not None]
    if len(present) < 2:
        return None
    return min(present), max(present), max(present) - min(present)


# Not measurements: `opts` restates the arm's own setting, `seed` names the draw, and `wall_seconds`
# is how long this machine took - it differs between two identical cells and would make every block
# look live.
NOT_A_MEASUREMENT = ("opts", "seed", "wall_seconds")


def numeric_leaves(obj, prefix=""):
    """Every number anywhere in a report, keyed by its path. Bools are labels, not measurements."""
    found = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            found.update(numeric_leaves(v, f"{prefix}/{k}"))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            found.update(numeric_leaves(v, f"{prefix}[{i}]"))
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        found[prefix] = obj
    return found


def _inert(grouped):
    """Report whether no number anywhere in the reports distinguishes any two arm values.

    `grouped` is {arm value: [report per seed]}. Reports are compared through their means over
    seeds, so a block run with several seeds is judged on the same footing as one run with a single
    seed.
    """
    if len(grouped) < 2:
        return False
    per_value = []
    for reports in grouped.values():
        leaves = [
            numeric_leaves({k: v for k, v in r.items() if k not in NOT_A_MEASUREMENT})
            for r in reports
        ]
        keys = set().union(*leaves) if leaves else set()
        per_value.append({k: _mean([leaf.get(k) for leaf in leaves]) for k in keys})
    for key in set().union(*per_value):
        values = [v.get(key) for v in per_value]
        present = [v for v in values if v is not None]
        if len(present) < 2:
            # A number one arm value records and another does not is itself a difference.
            if len(present) != len(values):
                return False
            continue
        scale = max(abs(min(present)), abs(max(present)), 1.0)
        if (max(present) - min(present)) / scale > INERT_EPSILON:
            return False
    return True


def summarise_block(reports):
    first = reports[0]
    cells = cells_of(reports)
    block = {
        "block": first.get("block", "?"),
        "arm": first.get("arm", "?"),
        "grid": first.get("grid") or [],
        "transport": first.get("transport"),
        "cells": cells,
        "wall_seconds": sum(r.get("wall_seconds") or 0 for r in reports),
        "nodes": (first.get("mesh") or {}).get("nodes"),
        "scenario": (first.get("opts") or {}).get("scenario"),
        "effect": {},
        "flags": [],
    }
    for key in (HEADLINE, COST):
        eff = _effect(cells, key)
        if eff:
            block["effect"][key] = {"low": eff[0], "high": eff[1], "spread": eff[2]}

    if _inert(group_by_value(reports)):
        block["flags"].append(
            f"inert: every value of `{block['arm']}` produced identical metrics - "
            "either the flag is not read, or it needs a second flag before it does anything (README §10.4)"
        )
    for cell in cells:
        silent = cell["metrics"].get("silent_losses")
        if silent:
            block["flags"].append(
                f"SILENT LOSSES {silent:g} at {block['arm']}={cell['value']} - "
                "a checksum closed over two unequal sets"
            )
        tx, drops = cell["metrics"].get("transmissions"), cell["metrics"].get(
            "queue_drops"
        )
        if tx and drops and drops / tx > QUEUE_DROP_WARN:
            block["flags"].append(
                f"queue drops {drops / tx:.1%} of transmissions at {block['arm']}={cell['value']} - "
                "airtime figures in this cell are measured through a backoff cap"
            )
        mis = cell["metrics"].get("misdecodes")
        if mis:
            block["flags"].append(
                f"misdecodes {mis:g} at {block['arm']}={cell['value']}"
            )
    return block


def collate(runs_dir, run_id=None, seed_base=None, scenario=None, expected=None):
    blocks = []
    for path in sorted(glob.glob(os.path.join(runs_dir, "*.json"))):
        # summary.json is this module's own output; a re-collate must not read it back in as a block.
        if os.path.basename(path) == "summary.json":
            continue
        reports = load_block(path)
        if reports and "block" in reports[0]:
            blocks.append(summarise_block(reports))

    present = {b["block"] for b in blocks}
    missing = sorted(set(expected) - present) if expected else []
    transports = sorted({b["transport"] for b in blocks if b.get("transport")})
    scenarios = sorted({b["scenario"] for b in blocks if b.get("scenario")})
    seeds = sorted(
        {s for b in blocks for c in b["cells"] for s in c["seeds"] if s is not None}
    )

    return {
        "run_id": run_id or datetime.date.today().isoformat(),
        "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"
        ),
        "seed_base": seed_base,
        "seeds": seeds,
        # What was asked for, and what the reports say happened. They differ when a block refuses a
        # scenario, and a digest that only recorded the request would hide that.
        "scenario_requested": scenario,
        "scenario_observed": scenarios,
        "transport": transports[0] if len(transports) == 1 else transports,
        "blocks": sorted(blocks, key=lambda b: b["block"]),
        "missing_blocks": missing,
        "wall_seconds": sum(b["wall_seconds"] for b in blocks),
        "gate": gate(blocks, missing),
    }


def gate(blocks, missing):
    """Judge the run. Only a silent loss is fatal; everything else is worth seeing, not stopping."""
    failures = [f for b in blocks for f in b["flags"] if f.startswith("SILENT LOSSES")]
    warnings = [
        f"{b['block']}: {f}"
        for b in blocks
        for f in b["flags"]
        if not f.startswith("SILENT LOSSES")
    ]
    return {
        "ok": not failures,
        "failures": failures,
        "warnings": warnings,
        "blocks_run": len(blocks),
        "blocks_missing": len(missing),
    }


def _fmt(v, places=3):
    if v is None:
        return "-"
    if isinstance(v, float) and abs(v) < 1000:
        return f"{v:.{places}f}"
    return f"{v:,.0f}"


def _arrow(cells, key):
    """Which end of the arm the metric prefers, read in the order the block declares its values."""
    values = [c["metrics"].get(key) for c in cells]
    present = [v for v in values if v is not None]
    if len(present) < 2:
        return " "
    if abs(present[-1] - present[0]) < INERT_EPSILON:
        return "="
    return "↑" if present[-1] > present[0] else "↓"


def markdown(summary):
    run_scenario = summary.get("scenario_requested") or "flat"
    out = [
        f"# Sweep {summary['run_id']}",
        "",
        f"- **transport** `{summary['transport']}`",
        f"- **ground** {run_scenario}",
        f"- **seed base** {summary.get('seed_base') or 'drawn per block'}"
        + (
            f" · seeds {', '.join(str(s) for s in summary['seeds'][:8])}"
            if summary["seeds"]
            else ""
        ),
        f"- **blocks** {summary['gate']['blocks_run']} run"
        + (
            f", {summary['gate']['blocks_missing']} missing"
            if summary["missing_blocks"]
            else ""
        ),
        f"- **compute** {summary['wall_seconds'] / 3600:.1f} h of simulator time across every cell",
        f"- **generated** {summary['generated']}",
        "",
    ]

    gates = summary["gate"]
    out.append("## Gates" if not gates["ok"] else "## Gates - held")
    out.append("")
    if gates["failures"]:
        out.append(
            "**The standing gate failed. Nothing else in this run should be read until it is explained.**"
        )
        out.append("")
        out += [f"- ❌ {f}" for f in gates["failures"]]
        out.append("")
    else:
        out += ["- ✅ `silent_losses` zero in every cell of every block", ""]
    if gates["warnings"]:
        out.append(
            "<details><summary>" + f"{len(gates['warnings'])} warnings</summary>"
        )
        out.append("")
        out += [f"- ⚠️ {w}" for w in gates["warnings"]]
        out += ["", "</details>", ""]
    if summary["missing_blocks"]:
        out += [
            "Blocks that produced no JSON (their job failed, timed out, or was cancelled): "
            + ", ".join(f"`{b}`" for b in summary["missing_blocks"]),
            "",
        ]

    # The trend proper: which variables move the archive at all, largest first. A reader who stops
    # after this table has the run's answer; everything below is the working.
    ranked = sorted(
        (b for b in summary["blocks"] if b["effect"].get(HEADLINE)),
        key=lambda b: b["effect"][HEADLINE]["spread"],
        reverse=True,
    )
    out += [
        "## What moved the archive",
        "",
        "Blocks ranked by how far `held` travels across their arm. `text` is the mesh's own reception "
        "in the same cells - an arm buying `held` while `text` falls is paying for reconciliation in "
        "the currency the mesh exists to spend.",
        "",
        "| block | arm | held low → high | spread | text | dir | cells |",
        "| --- | --- | --- | --- | --- | :-: | --: |",
    ]
    for b in ranked:
        eff = b["effect"][HEADLINE]
        cost = b["effect"].get(COST)
        out.append(
            f"| `{b['block']}` | {b['arm']} | {_fmt(eff['low'])} → {_fmt(eff['high'])} | "
            f"{_fmt(eff['spread'])} | "
            f"{_fmt(cost['low']) + ' → ' + _fmt(cost['high']) if cost else '-'} | "
            f"{_arrow(b['cells'], HEADLINE)} | {len(b['cells'])} |"
        )
    flat = [b for b in summary["blocks"] if not b["effect"].get(HEADLINE)]
    if flat:
        out += [
            "",
            f"{len(flat)} block(s) recorded no `held` spread: "
            + ", ".join(f"`{b['block']}`" for b in flat)
            + ".",
        ]

    out += ["", "## Every block", ""]
    for b in summary["blocks"]:
        grid = " ".join(b["grid"])
        out += [
            f"### `{b['block']}` - {b['arm']}" + (f"  `{grid}`" if grid else ""),
            "",
            "| value | held | union | text | worst node | SR airtime | util | moved | adverts |",
            "| --- | --: | --: | --: | --: | --: | --: | --: | --: |",
        ]
        for c in b["cells"]:
            m = c["metrics"]
            out.append(
                f"| {c['value']} | {_fmt(m.get('held'))} | {_fmt(m.get('union'))} | "
                f"{_fmt(m.get('text'))} | {_fmt(m.get('text_worst'))} | "
                f"{_fmt(m.get('sr_airtime'))} | {_fmt(m.get('utilisation'))} | "
                f"{_fmt(m.get('moved'), 0)} | {_fmt(m.get('adverts'), 0)} |"
            )
        for f in b["flags"]:
            out.append(f"\n> ⚠️ {f}")
        out.append("")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="reduce one scheduled sweep to a digest and a trend report"
    )
    ap.add_argument(
        "--runs", required=True, help="directory of block JSONs written by sfpp.sweep"
    )
    ap.add_argument(
        "--out", help="where to write summary.json and trend.md (default: --runs)"
    )
    ap.add_argument(
        "--run-id", help="names the run in the digest and the explorer; default today"
    )
    ap.add_argument("--seed-base", help="recorded so the run can be replayed exactly")
    ap.add_argument(
        "--scenario",
        help="the ground this run asked for, recorded alongside what it observed",
    )
    ap.add_argument(
        "--expect-all-blocks",
        action="store_true",
        help="treat sweep.BLOCKS as the expected set, so a block whose job failed is named rather than missed",
    )
    ap.add_argument(
        "--expect",
        help="space-separated block names this run asked for. Narrower than --expect-all-blocks, "
        "which would report every block a partial run never asked for as missing",
    )
    ap.add_argument(
        "--fail-on-gate",
        action="store_true",
        help="exit non-zero when the silent-loss gate failed",
    )
    opts = ap.parse_args(argv)

    expected = set(opts.expect.split()) if opts.expect else None
    if expected is None and opts.expect_all_blocks:
        from .sweep import BLOCKS

        expected = set(BLOCKS)

    summary = collate(
        opts.runs,
        run_id=opts.run_id,
        seed_base=opts.seed_base,
        scenario=opts.scenario,
        expected=expected,
    )
    out_dir = opts.out or opts.runs
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "summary.json"), "w") as f:
        json.dump(summary, f, indent=1, sort_keys=True)
    with open(os.path.join(out_dir, "trend.md"), "w") as f:
        f.write(markdown(summary) + "\n")
    print(
        f"collated {summary['gate']['blocks_run']} blocks"
        + (
            f", {summary['gate']['blocks_missing']} missing"
            if summary["missing_blocks"]
            else ""
        )
        + f" -> {out_dir}/summary.json, {out_dir}/trend.md"
    )
    for f in summary["gate"]["failures"]:
        print(f"FAIL {f}")
    return 1 if opts.fail_on_gate and not summary["gate"]["ok"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
