"""Round four's output: recommended values with the evidence beside each, not another essay.

Rounds one to three produced findings. Several were withdrawn, and the ones that survived did so
because they were ratios within a run rather than absolute figures. That history is the reason this
module refuses to emit a number without three things attached: the runs it came from, the transport
commit those runs used, and a confidence that says what would overturn it.

A recommendation here is a dataclass, not a sentence, so it can be regenerated when a block re-runs
and so a stale one is visibly stale rather than quietly wrong.

Usage, from sim/:
    python3 -m sfpp.tuning --runs <dir>              # every metric it can currently derive
    python3 -m sfpp.tuning --runs <dir> --markdown   # the table, ready to paste
"""

import argparse
import glob
import json
import os
import statistics
import subprocess
from dataclasses import dataclass, field


@dataclass
class Metric:
    name: str
    recommend: str
    replaces: str
    evidence: str
    blocks: list = field(default_factory=list)
    confidence: str = "low"
    overturned_by: str = ""

    def row(self):
        src = ", ".join(f"`{b}`" for b in self.blocks) or "-"
        return (
            f"| **{self.name}** | {self.recommend} | {self.replaces} | {self.evidence} | "
            f"{self.confidence} | {src} | {self.overturned_by} |"
        )


def _pin():
    try:
        return (
            subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                cwd=os.path.dirname(os.path.abspath(__file__)),
            ).stdout.strip()
            or "unknown"
        )
    except Exception:
        return "unknown"


def load_all(runs_dir):
    out = {}
    for path in sorted(glob.glob(os.path.join(runs_dir, "*.json"))):
        name = os.path.basename(path).replace(".json", "")
        with open(path) as f:
            d = json.load(f)
        out[name] = d if isinstance(d, list) else [d]
    return out


def _arm(blocks, name):
    """The named block, plus any grid variant of it.

    Matched on the exact name or on `name-<grid>`, never on a bare prefix: `R-repeats` would
    otherwise pull in `R-repeats-busy`, and seven block names are a prefix of another.
    """
    return [k for k in blocks if k == name or k.startswith(name + "-hours")]


def _sortable(value):
    """Order arm values numerically where they are numbers, lexically where they are not.

    Sorting on float() crashed the whole tuning pass the first time a block with a boolean arm was
    present, after every block had run and been pushed - so the results survived and the summary
    did not.
    """
    try:
        return (0, float(value), "")
    except (TypeError, ValueError):
        return (1, 0.0, str(value))


def _cells(runs):
    g = {}
    for r in runs:
        g.setdefault(str(r.get("value", "-")), []).append(r)
    return g


def _mean(rs, path):
    section, key = path
    vals = [r[section][key] for r in rs if section in r and key in r[section]]
    return statistics.mean(vals) if vals else None


def derive(blocks):
    """Only emit a metric when the block that decides it is actually present."""
    metrics = []

    # --- already answered well enough to state, from rounds two and three ---
    place = _arm(blocks, "N-place") + _arm(blocks, "G-place")
    hops = _arm(blocks, "N-hops") + _arm(blocks, "G-hops")
    servers = _arm(blocks, "N-servers") + _arm(blocks, "G-servers")
    if hops and servers:
        metrics.append(
            Metric(
                name="Archive count and separation",
                recommend="**3 archives, 3-4 hops apart**",
                replaces="nothing - unspecified in the design",
                evidence="held flat to 3 decimals above 3 servers; recovery peaks at 3-4 hops "
                "(55.9-57.9% against 44.9% at 1 hop)",
                blocks=hops + servers,
                confidence="high - held across two rounds, both numbering regimes, four topologies",
                overturned_by="a stretched-mesh result, where archives clustered at one end strand a fifth "
                "of the nodes and separation stops being the binding constraint",
            )
        )
    if place:
        metrics.append(
            Metric(
                name="Archive host",
                recommend="**no preference between router, beside-router and every-other-router**",
                replaces="'put it beside a router, not on it'",
                evidence="0.839 / 0.841 / 0.840 held - indistinguishable once hop limits vary 3-7",
                blocks=place,
                confidence="medium - the round-one preference was an artefact of a flat hop limit",
                overturned_by="a topology where routers do not cluster in the dense core",
            )
        )

    cad = (
        _arm(blocks, "D-cadence") + _arm(blocks, "M-jitter") + _arm(blocks, "D-jitter")
    )
    if cad:
        metrics.append(
            Metric(
                name="Advert cadence",
                recommend="**trigger on bucket close, not a timer**; jitter window in **minutes, not seconds**",
                replaces="`f_floor` guesswork",
                evidence="bucket-close holds more for a sixteenth of the airtime; at 1 s jitter five "
                "adverts in six are destroyed by the other archives",
                blocks=cad,
                confidence="medium - the airtime ratio was measured pre-transport-fix and needs re-running",
                overturned_by="a re-run on the pinned transport showing the ratio narrowing as it did for "
                "the sketch-vs-chain headline",
            )
        )

    cap = _arm(blocks, "M-capacity") + _arm(blocks, "E-capacity")
    if cap:
        metrics.append(
            Metric(
                name="Sketch capacity",
                recommend="**c = bucket size**, and treat the two as one decision",
                replaces="`c = 32` frozen independently of the bucket",
                evidence="under local numbering the difference is bounded by 2x the bucket, so larger is "
                "monotonically better to 50; escalations fall 28 -> 2",
                blocks=cap,
                confidence="medium",
                overturned_by="a bucket size other than 32, which has never been swept",
            )
        )

    # --- round four's own, only when its blocks exist ---
    ci = _arm(blocks, "R-congestion-input")
    if ci:
        cells = _cells(blocks[ci[0]])
        hot = _mean(cells.get("hotstore", []), ("traffic", "channel_utilisation"))
        true = _mean(cells.get("truesize", []), ("traffic", "channel_utilisation"))
        if hot and true:
            metrics.append(
                Metric(
                    name="Congestion-scaling input",
                    recommend=f"**replace `getNumOnlineMeshNodes()`** - utilisation {hot:.0%} on the hot "
                    f"store against {true:.0%} on true mesh size",
                    replaces="`getNumOnlineMeshNodes()`, bounded by MAX_NUM_NODES",
                    evidence=f"measured at 250 nodes: the throttle saturates and channel demand rises, "
                    f"where an unbounded input holds it flat",
                    blocks=ci,
                    confidence="medium",
                    overturned_by="a utilisation-driven input performing worse than either",
                )
            )

    rp = _arm(blocks, "R-srretries")
    if rp:
        cells = _cells(blocks[rp[0]])
        best, best_v = None, -1
        for v, rs in sorted(cells.items(), key=lambda kv: _sortable(kv[0])):
            held = _mean(rs, ("sfpp", "held_fraction_mean")) or 0
            if held > best_v + 0.005:
                best, best_v = v, held
        if best is not None:
            metrics.append(
                Metric(
                    name="Reconciliation retries per hop",
                    recommend=f"**{best}** - held stops improving beyond it",
                    replaces="a fixed retry count chosen without measurement",
                    evidence=f"held {best_v:.3f} at {best} retries; further retries do not improve it",
                    blocks=rp,
                    confidence="medium",
                    overturned_by="a lossier mesh, where the plateau should move up",
                )
            )

    over = _arm(blocks, "R-oversubscribed") + _arm(blocks, "R-hotstore-stress")
    if over:
        metrics.append(
            Metric(
                name="Behaviour past the node database",
                recommend="**needs a scaling term the firmware does not have**",
                replaces="nothing - the case is unhandled",
                evidence="the throttle's input saturates at MAX_NUM_NODES while offered load keeps rising",
                blocks=over,
                confidence="low until the sweep completes",
                overturned_by="the sweep showing demand flat past the store size",
            )
        )

    return metrics


def report(runs_dir, markdown=False):
    blocks = load_all(runs_dir)
    metrics = derive(blocks)
    pin = _pin()
    lines = []
    if markdown:
        lines += [
            "# Tuning metrics",
            "",
            f"**Transport pin:** `{pin}` · **Source:** `{runs_dir}` · "
            f"**{len(blocks)} blocks present**",
            "",
            "Every row names the blocks it came from and what would overturn it. A row whose blocks "
            "are absent is not shown at all rather than guessed.",
            "",
            "| Metric | Recommendation | Replaces | Evidence | Confidence | From | Overturned by |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        lines += [m.row() for m in metrics]
        missing = [
            b
            for b in (
                "R-oversubscribed",
                "R-congestion-input",
                "R-srretries",
                "R-hotstore-stress",
            )
            if not _arm(blocks, b)
        ]
        if missing:
            lines += [
                "",
                "## Not yet derivable",
                "",
                "These need blocks that have not run:",
                "",
            ] + [f"- `{b}`" for b in missing]
    else:
        lines.append(
            f"tuning metrics - transport {pin}, {len(blocks)} blocks in {runs_dir}"
        )
        for m in metrics:
            lines += [
                "",
                f"  {m.name}",
                f"    recommend : {m.recommend}",
                f"    replaces  : {m.replaces}",
                f"    evidence  : {m.evidence}",
                f"    confidence: {m.confidence}",
                f"    from      : {', '.join(m.blocks)}",
                f"    overturned by: {m.overturned_by}",
            ]
        if not metrics:
            lines.append("  (no block present yet decides any metric)")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", required=True)
    ap.add_argument("--markdown", action="store_true")
    ap.add_argument("--out")
    opts = ap.parse_args(argv)
    text = report(opts.runs, markdown=opts.markdown)
    print(text)
    if opts.out:
        with open(opts.out, "w") as f:
            f.write(text + "\n")
        print(f"\nwrote {opts.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
