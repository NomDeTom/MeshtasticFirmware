"""The rolling view: every scheduled sweep so far, in one page.

A single run answers "what did this seed and this ground say". The question that matters is whether
it says the same thing as the last thirty, and no single run can answer that - which is the whole
reason a nightly sweep is worth running at all. This module reads the digests `collate.py` left
behind, one per run, and renders them as a page whose rows are blocks and whose columns are runs.

Only the digests are read, never the block JSONs, so the archive can drop the raw runs without the
explorer losing its history. The page is a single self-contained file: no CDN, no fonts, no fetch,
because it is served from a git branch and has to work as a local file too.

Usage, from sim/:
    python3 -m sfpp.explorer --archive <dir of run dirs> --out <dir>
    python3 -m sfpp.explorer --archive runs --out . --window 30
"""

import argparse
import glob
import html
import json
import os
import statistics

from .collate import COST, HEADLINE

# What the page shows per cell. The digest carries more (see collate.METRICS); these are the ones a
# person reads a trend through, and keeping the embedded payload to eight numbers a cell is what
# lets thirty runs of 87 blocks stay a file a browser opens instantly.
SHOWN = [
    ("held", "held", 3),
    ("union", "union", 3),
    ("text", "text", 3),
    ("text_worst", "worst", 3),
    ("sr_airtime", "SR air", 3),
    ("utilisation", "util", 3),
    ("moved", "moved", 0),
    ("adverts", "adverts", 0),
]


def load_archive(archive_dir, window=None):
    """Every run digest under `archive_dir`, oldest first, optionally only the most recent `window`."""
    runs = []
    for path in sorted(glob.glob(os.path.join(archive_dir, "*", "summary.json"))):
        try:
            with open(path) as f:
                summary = json.load(f)
        except (OSError, json.JSONDecodeError):
            # A run whose digest is unreadable is a run that failed midway; the rolling view is
            # exactly the wrong place to stop for it.
            continue
        summary["_dir"] = os.path.dirname(path)
        summary["_name"] = os.path.basename(summary["_dir"])
        runs.append(summary)
    runs.sort(key=lambda r: (r.get("run_id") or "", r.get("generated") or ""))
    return runs[-window:] if window else runs


def index_by_block(runs):
    """{block: {"arm", "runs": [{run_id, scenario, cells: {value: metrics}}]}} across the archive."""
    blocks = {}
    for run in runs:
        for b in run.get("blocks", []):
            entry = blocks.setdefault(b["block"], {"arm": b["arm"], "runs": []})
            entry["arm"] = b["arm"]
            entry["runs"].append(
                {
                    "run_id": run.get("run_id"),
                    "scenario": run.get("scenario_requested") or "flat",
                    "seed_base": run.get("seed_base"),
                    "cells": {c["value"]: c["metrics"] for c in b["cells"]},
                    "flags": b.get("flags", []),
                }
            )
    return blocks


def leaderboard(blocks):
    """Blocks by how far `held` travels across their arm, averaged over the runs that have them.

    Averaged rather than pooled: a block present in thirty runs and one present in two would
    otherwise be ranked by how long they have been in the archive.
    """
    rows = []
    for name, entry in blocks.items():
        spreads, costs = [], []
        for run in entry["runs"]:
            held = [
                m.get(HEADLINE)
                for m in run["cells"].values()
                if m.get(HEADLINE) is not None
            ]
            cost = [
                m.get(COST) for m in run["cells"].values() if m.get(COST) is not None
            ]
            if len(held) > 1:
                spreads.append(max(held) - min(held))
            if len(cost) > 1:
                costs.append(max(cost) - min(cost))
        if not spreads:
            continue
        rows.append(
            {
                "block": name,
                "arm": entry["arm"],
                "spread": statistics.mean(spreads),
                # Whether the archive's gain is stable run to run, or an artefact of one seed.
                "spread_sd": statistics.stdev(spreads) if len(spreads) > 1 else None,
                "cost": statistics.mean(costs) if costs else None,
                "runs": len(spreads),
            }
        )
    return sorted(rows, key=lambda r: r["spread"], reverse=True)


def series(entry, key):
    """{arm value: [metric per run, None where that run did not have the value]} for one block."""
    values = []
    for run in entry["runs"]:
        for v in run["cells"]:
            if v not in values:
                values.append(v)
    return {
        v: [run["cells"].get(v, {}).get(key) for run in entry["runs"]] for v in values
    }


def sparkline(points, width=90, height=18):
    """Inline SVG, no library. Gaps in the series break the line rather than interpolating a lie."""
    present = [p for p in points if p is not None]
    if len(present) < 2:
        return ""
    lo, hi = min(present), max(present)
    span = (hi - lo) or 1.0
    step = width / max(1, len(points) - 1)
    segments, current = [], []
    for i, p in enumerate(points):
        if p is None:
            if len(current) > 1:
                segments.append(current)
            current = []
            continue
        current.append(
            f"{i * step:.1f},{height - (p - lo) / span * (height - 2) - 1:.1f}"
        )
    if len(current) > 1:
        segments.append(current)
    paths = "".join(f'<polyline points="{" ".join(s)}" />' for s in segments)
    return (
        f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'aria-hidden="true">{paths}</svg>'
    )


def _fmt(v, places=3):
    if v is None:
        return "·"
    if places == 0:
        return f"{v:,.0f}"
    return f"{v:.{places}f}"


def _esc(v):
    return html.escape(str(v))


CSS = """
:root {
  color-scheme: light dark;
  --bg: #fbfaf9; --panel: #ffffff; --ink: #1d1b19; --muted: #6c6660; --line: #e4e0dc;
  --accent: #9b4a1f; --warn: #8a6d1a; --bad: #a01f1f; --spark: #9b4a1f;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16151a; --panel: #1e1d23; --ink: #eceaf0; --muted: #9d97a6; --line: #302e38;
    --accent: #e0895a; --warn: #d4b155; --bad: #e57373; --spark: #e0895a;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2rem 1.25rem 5rem; background: var(--bg); color: var(--ink);
  font: 15px/1.55 ui-sans-serif, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
.wrap { max-width: 1180px; margin: 0 auto; }
h1 { font-size: 1.5rem; margin: 0 0 .25rem; letter-spacing: -0.01em; }
h2 { font-size: 1.05rem; margin: 2.5rem 0 .75rem; letter-spacing: -0.005em; }
h3 { font-size: .95rem; margin: 0; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
p.sub { color: var(--muted); margin: 0 0 1.5rem; }
.meta { display: flex; flex-wrap: wrap; gap: .5rem 1.5rem; color: var(--muted); font-size: .85rem; margin-bottom: 1.5rem; }
.meta b { color: var(--ink); font-weight: 600; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 1rem 1.1rem; margin-bottom: 1rem; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-size: .85rem; }
th, td { text-align: right; padding: .35rem .5rem; border-bottom: 1px solid var(--line); white-space: nowrap; }
th:first-child, td:first-child { text-align: left; }
th { color: var(--muted); font-weight: 600; font-size: .78rem; text-transform: uppercase; letter-spacing: .04em; }
tbody tr:last-child td { border-bottom: 0; }
code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: .95em; }
.spark polyline { fill: none; stroke: var(--spark); stroke-width: 1.5; stroke-linejoin: round; }
.flag { color: var(--warn); font-size: .82rem; margin: .5rem 0 0; }
.flag.bad { color: var(--bad); }
.pill { display: inline-block; padding: .1rem .5rem; border: 1px solid var(--line); border-radius: 999px; font-size: .75rem; color: var(--muted); }
.controls { display: flex; flex-wrap: wrap; gap: .6rem; margin: 1rem 0 1.5rem; }
input[type=search], select {
  background: var(--panel); color: var(--ink); border: 1px solid var(--line);
  border-radius: 8px; padding: .4rem .6rem; font: inherit; font-size: .85rem;
}
.blockhead { display: flex; align-items: baseline; gap: .6rem; flex-wrap: wrap; margin-bottom: .6rem; }
.blockhead .arm { color: var(--muted); font-size: .85rem; }
.hidden { display: none; }
footer { color: var(--muted); font-size: .8rem; margin-top: 3rem; }
"""

JS = """
const q = document.getElementById('q');
const sc = document.getElementById('scenario');
function apply() {
  const needle = (q.value || '').toLowerCase();
  const scen = sc.value;
  for (const el of document.querySelectorAll('[data-block]')) {
    const hitText = !needle || el.dataset.block.toLowerCase().includes(needle)
                            || el.dataset.arm.toLowerCase().includes(needle);
    const hitScen = scen === 'all' || (el.dataset.scenarios || '').split('|').includes(scen);
    el.classList.toggle('hidden', !(hitText && hitScen));
  }
}
q.addEventListener('input', apply);
sc.addEventListener('change', apply);
"""


def render_html(runs, blocks, board):
    scenarios = sorted({r.get("scenario_requested") or "flat" for r in runs})
    transports = sorted(
        {
            t
            for r in runs
            for t in (
                [r["transport"]]
                if isinstance(r.get("transport"), str)
                else r.get("transport") or []
            )
        }
    )
    run_ids = [r.get("run_id") or r["_dir"] for r in runs]
    failures = [
        (r.get("run_id"), f)
        for r in runs
        for f in r.get("gate", {}).get("failures", [])
    ]

    out = [
        "<title>SF++ sweep explorer</title>",
        f"<style>{CSS}</style>",
        '<div class="wrap">',
        "<h1>SF++ sweep explorer</h1>",
        f'<p class="sub">{len(runs)} scheduled run(s), {len(blocks)} block(s), rolling. '
        "Each column is one run: one random seed base over one landform.</p>",
        '<div class="meta">',
        f"<span><b>{len(runs)}</b> runs</span>",
        f"<span>first <b>{_esc(run_ids[0]) if run_ids else '-'}</b></span>",
        f"<span>latest <b>{_esc(run_ids[-1]) if run_ids else '-'}</b></span>",
        f"<span>ground <b>{_esc(', '.join(scenarios))}</b></span>",
        f"<span>transport <b class='mono'>{_esc(', '.join(transports) or '-')}</b></span>",
        "</div>",
    ]

    if failures:
        out.append('<div class="panel">')
        out.append(
            '<p class="flag bad"><b>The standing gate failed in one or more runs.</b></p>'
        )
        for run_id, f in failures:
            out.append(f'<p class="flag bad">{_esc(run_id)}: {_esc(f)}</p>')
        out.append("</div>")

    out += [
        "<h2>What moves the archive</h2>",
        '<p class="sub">Mean spread of <code>held</code> across each arm, averaged over the runs that '
        "carry that block. A large spread means the variable decides something; a large <code>text</code> "
        "column beside it means it decides that something by spending the mesh's own reception.</p>",
        '<div class="panel scroll"><table><thead><tr>'
        "<th>block</th><th>arm</th><th>held spread</th><th>run-to-run sd</th>"
        "<th>text spread</th><th>runs</th></tr></thead><tbody>",
    ]
    for row in board:
        out.append(
            f'<tr><td class="mono">{_esc(row["block"])}</td><td>{_esc(row["arm"])}</td>'
            f"<td>{_fmt(row['spread'])}</td><td>{_fmt(row['spread_sd'])}</td>"
            f"<td>{_fmt(row['cost'])}</td><td>{row['runs']}</td></tr>"
        )
    out.append("</tbody></table></div>")

    out += [
        "<h2>Every block, run by run</h2>",
        '<div class="controls">',
        '<input type="search" id="q" placeholder="filter by block or arm…" />',
        '<select id="scenario"><option value="all">every landform</option>'
        + "".join(f'<option value="{_esc(s)}">{_esc(s)}</option>' for s in scenarios)
        + "</select>",
        "</div>",
    ]

    for name in sorted(blocks):
        entry = blocks[name]
        block_scenarios = sorted({r["scenario"] for r in entry["runs"]})
        held = series(entry, HEADLINE)
        out.append(
            f'<div class="panel" data-block="{_esc(name)}" data-arm="{_esc(entry["arm"])}" '
            f'data-scenarios="{_esc("|".join(block_scenarios))}">'
        )
        out.append(
            f'<div class="blockhead"><h3>{_esc(name)}</h3>'
            f'<span class="arm">{_esc(entry["arm"])}</span>'
            + "".join(f'<span class="pill">{_esc(s)}</span>' for s in block_scenarios)
            + f'<span class="pill">{len(entry["runs"])} run(s)</span></div>'
        )
        out.append('<div class="scroll"><table><thead><tr><th>value</th><th>trend</th>')
        out += [f"<th>{_esc(r['run_id'])}</th>" for r in entry["runs"]]
        out.append("<th>mean</th></tr></thead><tbody>")
        for value, points in held.items():
            present = [p for p in points if p is not None]
            out.append(
                f'<tr><td class="mono">{_esc(value)}</td><td>{sparkline(points)}</td>'
                + "".join(f"<td>{_fmt(p)}</td>" for p in points)
                + f"<td><b>{_fmt(statistics.mean(present)) if present else '·'}</b></td></tr>"
            )
        out.append("</tbody></table></div>")

        # The latest run in full: the sparkline row above carries `held` only, and a reader who has
        # spotted a moving block needs the currency it moved in without opening the run's own report.
        latest = entry["runs"][-1]
        out.append(
            f'<p class="sub" style="margin:.75rem 0 .35rem;font-size:.8rem">'
            f"latest run {_esc(latest['run_id'])}, every metric</p>"
        )
        out.append('<div class="scroll"><table><thead><tr><th>value</th>')
        out += [f"<th>{_esc(label)}</th>" for _, label, _ in SHOWN]
        out.append("</tr></thead><tbody>")
        for value, metrics in latest["cells"].items():
            out.append(
                f'<tr><td class="mono">{_esc(value)}</td>'
                + "".join(
                    f"<td>{_fmt(metrics.get(key), places)}</td>"
                    for key, _, places in SHOWN
                )
                + "</tr>"
            )
        out.append("</tbody></table></div>")
        for f in {f for r in entry["runs"] for f in r["flags"]}:
            out.append(f'<p class="flag">{_esc(f)}</p>')
        out.append("</div>")

    out += [
        "<h2>Runs</h2>",
        '<div class="panel scroll"><table><thead><tr><th>run</th><th>ground</th><th>seed base</th>'
        "<th>blocks</th><th>missing</th><th>warnings</th><th>compute h</th><th>report</th>"
        "</tr></thead><tbody>",
    ]
    for r in reversed(runs):
        gate = r.get("gate", {})
        # A run that asked for ground and got none is a run whose landform column is a label rather
        # than a fact - the same failure as an inert arm, one level up.
        asked = r.get("scenario_requested") or ""
        ignored = asked and asked not in (r.get("scenario_observed") or [])
        out.append(
            f'<tr><td class="mono">{_esc(r.get("run_id"))}</td>'
            f'<td>{_esc(asked or "flat")}'
            + (
                ' <span class="pill" title="the blocks in this run recorded no scenario">not applied</span>'
                if ignored
                else ""
            )
            + "</td>"
            f'<td class="mono">{_esc(r.get("seed_base") or "-")}</td>'
            f'<td>{gate.get("blocks_run", 0)}</td><td>{gate.get("blocks_missing", 0)}</td>'
            f'<td>{len(gate.get("warnings", []))}</td>'
            f'<td>{(r.get("wall_seconds") or 0) / 3600:.1f}</td>'
            f'<td><a href="{_esc(r["_href"])}/trend.md">trend.md</a></td></tr>'
        )
    out += [
        "</tbody></table></div>",
        "<footer>Built by <code>sfpp.explorer</code> from the run digests in this branch. "
        "Nothing here is hand-edited; a scheduled job rewrites the page.</footer>",
        "</div>",
        f"<script>{JS}</script>",
    ]
    return "\n".join(out)


def render_markdown(runs, blocks, board):
    latest = runs[-1] if runs else {}
    out = [
        "# SF++ sweep explorer",
        "",
        f"{len(runs)} scheduled run(s) rolled up, {len(blocks)} block(s). "
        "Open `index.html` for the filterable page; this file is the same data in a diff-readable form.",
        "",
        f"- **latest** `{latest.get('run_id', '-')}` on {latest.get('scenario_requested') or 'flat'} "
        f"ground, seed base `{latest.get('seed_base', '-')}`",
        f"- **transport** `{latest.get('transport', '-')}`",
        "",
        "## What moves the archive",
        "",
        "| block | arm | held spread | run-to-run sd | text spread | runs |",
        "| --- | --- | --: | --: | --: | --: |",
    ]
    for row in board:
        out.append(
            f"| `{row['block']}` | {row['arm']} | {_fmt(row['spread'])} | {_fmt(row['spread_sd'])} | "
            f"{_fmt(row['cost'])} | {row['runs']} |"
        )
    out += [
        "",
        "## Runs",
        "",
        "| run | ground | seed base | blocks | missing | warnings |",
        "| --- | --- | --- | --: | --: | --: |",
    ]
    for r in reversed(runs):
        gate = r.get("gate", {})
        out.append(
            f"| [`{r.get('run_id')}`]({r['_href']}/trend.md) | {r.get('scenario_requested') or 'flat'} | "
            f"`{r.get('seed_base', '-')}` | {gate.get('blocks_run', 0)} | {gate.get('blocks_missing', 0)} | "
            f"{len(gate.get('warnings', []))} |"
        )
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="roll every collated sweep into one page")
    ap.add_argument(
        "--archive", required=True, help="directory holding one subdirectory per run"
    )
    ap.add_argument(
        "--out", default=".", help="where index.html and INDEX.md are written"
    )
    ap.add_argument("--window", type=int, help="use only the most recent N runs")
    opts = ap.parse_args(argv)

    runs = load_archive(opts.archive, opts.window)
    if not runs:
        print(f"no run digests under {opts.archive} - nothing to roll up")
        return 1
    blocks = index_by_block(runs)
    board = leaderboard(blocks)

    os.makedirs(opts.out, exist_ok=True)
    # Links are written relative to the page, which does not sit in the archive: the page is at the
    # results root and the runs are a directory below it.
    for r in runs:
        r["_href"] = os.path.relpath(r["_dir"], opts.out).replace(os.sep, "/")
    with open(os.path.join(opts.out, "index.html"), "w") as f:
        f.write(render_html(runs, blocks, board))
    with open(os.path.join(opts.out, "INDEX.md"), "w") as f:
        f.write(render_markdown(runs, blocks, board) + "\n")
    print(
        f"rolled {len(runs)} run(s), {len(blocks)} block(s) -> {opts.out}/index.html, {opts.out}/INDEX.md"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
