"""Figures from the sweep's run JSONs.

Each block wrote one JSON of per-cell reports; these turn them into the pictures the write-up needs.
Nothing here recomputes anything - if a number is not in the JSON it does not appear on a chart.

Usage, from sim/:
    python3 -m sfpp.figures --runs <dir> --out <dir>
"""

import argparse
import glob
import json
import os
import statistics

# Drawing is all this module does, so it reports the missing library rather than raising at import.
try:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:  # pragma: no cover - exercised by running without matplotlib
    plt = None

INK = "#1b1b1b"
MUTED = "#8a8a8a"
ACCENT = "#B4472A"
COOL = "#2E5E7E"
GRID = "#e3e3e0"
BG = "#FCFCFA"


def style(ax, title, xlabel, ylabel):
    ax.set_title(title, fontsize=12, color=INK, loc="left", pad=12)
    ax.set_xlabel(xlabel, fontsize=10, color=MUTED)
    ax.set_ylabel(ylabel, fontsize=10, color=MUTED)
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=9)


def load(runs_dir, block):
    """One block's saved cells, or None with a line saying which file was wanted.

    A block run under --grid is written with the grid in its name, so a run directory can hold
    `D-cadence-hours-24.json` and not `D-cadence.json`. Silently drawing nothing in that case was
    indistinguishable from having no data at all.
    """
    path = os.path.join(runs_dir, f"{block}.json")
    if not os.path.exists(path):
        near = sorted(
            os.path.basename(p)
            for p in glob.glob(os.path.join(runs_dir, f"{block}-*.json"))
        )
        hint = f" (found {', '.join(near)} - rename or re-run without --grid)" if near else ""
        print(f"skipped: no {block}.json in {runs_dir}{hint}")
        return None
    with open(path) as f:
        return json.load(f)


def cells(reports, key="value"):
    """Group per-cell reports by their arm value, in first-seen order."""
    out = {}
    for report in reports:
        if "sfpp" not in report:
            continue
        out.setdefault(report[key], []).append(report)
    return out


def mean(reports, path):
    section, field = path
    return statistics.mean(r[section][field] for r in reports)


def bar_pair(ax, labels, left, right, left_label, right_label):
    x = range(len(labels))
    width = 0.38
    ax.bar([i - width / 2 for i in x], left, width, color=COOL, label=left_label)
    ax2 = ax.twinx()
    ax2.bar([i + width / 2 for i in x], right, width, color=ACCENT, label=right_label)
    ax2.spines["top"].set_visible(False)
    ax2.tick_params(colors=MUTED, labelsize=9)
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, fontsize=9)
    return ax2


def fig_cadence(runs_dir, out_dir):
    reports = load(runs_dir, "D-cadence")
    if not reports:
        return
    grouped = cells(reports)
    labels = list(grouped)
    held = [mean(v, ("sfpp", "held_fraction_mean")) for v in grouped.values()]
    air = [100 * mean(v, ("sfpp", "sr_airtime_share")) for v in grouped.values()]

    fig, ax = plt.subplots(figsize=(7.4, 4.2), facecolor=BG)
    ax.set_facecolor(BG)
    ax2 = bar_pair(ax, labels, held, air, "held", "SR airtime")
    style(
        ax,
        "Cadence: what each trigger holds, and what it spends",
        "",
        "fraction of chain held",
    )
    ax2.set_ylabel("SR share of mesh airtime (%)", fontsize=10, color=MUTED)
    ax.set_ylim(0, 1)
    fig.tight_layout()
    save(fig, out_dir, "cadence")


def fig_resolve(runs_dir, out_dir):
    reports = load(runs_dir, "D-resolve")
    if not reports:
        return
    grouped = cells(reports)
    labels = list(grouped)
    held = [mean(v, ("sfpp", "held_fraction_mean")) for v in grouped.values()]
    total = [mean(v, ("sfpp", "sr_bytes")) / 1000.0 for v in grouped.values()]

    fig, ax = plt.subplots(figsize=(7.4, 4.2), facecolor=BG)
    ax.set_facecolor(BG)
    ax2 = bar_pair(ax, labels, held, total, "held", "SR bytes")
    style(
        ax,
        "Resolution: sketch-as-request against explicit enumeration",
        "",
        "fraction of chain held",
    )
    ax2.set_ylabel("total SR bytes (KB)", fontsize=10, color=MUTED)
    ax.set_ylim(0, 1)
    fig.tight_layout()
    save(fig, out_dir, "resolve")


def fig_capacity(runs_dir, out_dir):
    reports = load(runs_dir, "E-capacity")
    if not reports:
        return
    grouped = cells(reports)
    caps = sorted(grouped)
    held = [mean(grouped[c], ("sfpp", "held_fraction_mean")) for c in caps]
    fails = [mean(grouped[c], ("sfpp", "decode_failures")) for c in caps]
    bytes_ = [mean(grouped[c], ("sfpp", "advert_bytes")) / 1000.0 for c in caps]

    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), facecolor=BG)
    for ax in axes:
        ax.set_facecolor(BG)
    axes[0].plot(caps, held, "o-", color=COOL, linewidth=2)
    style(
        axes[0],
        "Sketch capacity against holdings",
        "capacity c",
        "fraction of chain held",
    )
    axes[0].set_ylim(0, 1)
    axes[1].plot(caps, fails, "o-", color=ACCENT, linewidth=2, label="decode failures")
    axes[1].plot(caps, bytes_, "s--", color=COOL, linewidth=1.6, label="advert KB")
    style(
        axes[1], "What capacity costs and what it prevents", "capacity c", "count / KB"
    )
    axes[1].legend(frameon=False, fontsize=9)
    fig.tight_layout()
    save(fig, out_dir, "capacity")


def fig_loss(runs_dir, out_dir):
    """The paired capacity-by-loss grid: what loss does, and what it conspicuously does not do."""
    groups = {}
    for capacity in (8, 16, 32):
        reports = load(runs_dir, f"F-loss-capacity-{capacity}")
        if reports:
            groups[capacity] = cells(reports)
    if not groups:
        return

    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.4), facecolor=BG)
    for ax in axes:
        ax.set_facecolor(BG)
    colours = {8: "#7FB0CB", 16: COOL, 32: ACCENT}

    for capacity, grouped in sorted(groups.items()):
        losses = sorted(grouped)
        held = [mean(grouped[x], ("sfpp", "held_fraction_mean")) for x in losses]
        fails = [mean(grouped[x], ("sfpp", "decode_failures")) for x in losses]
        axes[0].plot(
            losses,
            held,
            "o-",
            color=colours[capacity],
            linewidth=2,
            label=f"capacity {capacity}",
        )
        axes[1].plot(
            losses,
            fails,
            "o-",
            color=colours[capacity],
            linewidth=2,
            label=f"capacity {capacity}",
        )

    style(
        axes[0],
        "Loss costs holdings, as it should",
        "added loss floor",
        "fraction of chain held",
    )
    axes[0].set_ylim(0, 1)
    axes[0].legend(frameon=False, fontsize=9)
    style(
        axes[1],
        "Loss does not cost decodes, at any capacity",
        "added loss floor",
        "decode failures per run",
    )
    axes[1].set_ylim(bottom=-2)
    axes[1].legend(frameon=False, fontsize=9)
    fig.tight_layout()
    save(fig, out_dir, "capacity-vs-loss")


def recovery(reports):
    """Of everything an ordinary node missed, the share the archive holds. The payoff metric.

    Guarded the way analyse.py's block_table is: a cell whose baseline reception approaches 1 has
    almost nothing left to recover, and dividing by that headroom unguarded produced a -193%
    reading there before the guard went in. At exactly 1.0 it is a divide by zero, which until now
    only survived because fig_topology wraps the whole chart in a broad except and skips it.
    """
    rec = mean(reports, ("baseline", "text_reception_mean"))
    one = mean(reports, ("sfpp", "held_fraction_mean"))
    allof = mean(reports, ("sfpp", "union_fraction"))
    headroom = max(1e-9, 1.0 - rec)
    return (one - rec) / headroom, (allof - rec) / headroom


def fig_topology(runs_dir, out_dir):
    place = load(runs_dir, "G-place")
    hops = load(runs_dir, "G-hops")
    if not place and not hops:
        return
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.6), facecolor=BG)
    for ax in axes:
        ax.set_facecolor(BG)

    if place:
        grouped = cells(place)
        labels = list(grouped)
        one = [recovery(v)[0] for v in grouped.values()]
        allof = [recovery(v)[1] for v in grouped.values()]
        x = range(len(labels))
        axes[0].bar(
            [i - 0.19 for i in x], one, 0.38, color=COOL, label="from one server"
        )
        axes[0].bar(
            [i + 0.19 for i in x], allof, 0.38, color=ACCENT, label="from all three"
        )
        axes[0].axhline(0, color=MUTED, linewidth=0.8)
        axes[0].set_xticks(list(x))
        axes[0].set_xticklabels(labels, fontsize=8, rotation=20, ha="right")
        style(
            axes[0], "Where the servers go", "", "of what a node missed, archive holds"
        )
        axes[0].legend(frameon=False, fontsize=9)

    if hops:
        grouped = cells(hops)
        seps = sorted(grouped)
        one = [recovery(grouped[h])[0] for h in seps]
        allof = [recovery(grouped[h])[1] for h in seps]
        axes[1].plot(seps, one, "o-", color=COOL, linewidth=2, label="from one server")
        axes[1].plot(
            seps, allof, "s-", color=ACCENT, linewidth=2, label="from all three"
        )
        axes[1].axvline(
            3, color=MUTED, linestyle=":", linewidth=1.2
        )  # the mesh's hop limit
        axes[1].annotate(
            "hop limit",
            xy=(3, 0.05),
            fontsize=9,
            color=MUTED,
            ha="center",
            xytext=(3, 0.02),
        )
        style(
            axes[1],
            "Separation is the dominant variable",
            "hops between servers",
            "of what a node missed, archive holds",
        )
        axes[1].set_xticks(seps)
        axes[1].legend(frameon=False, fontsize=9)
    fig.tight_layout()
    save(fig, out_dir, "topology")


def fig_baseline(runs_dir, out_dir, block="Q-control"):
    """Reach against the routing ceiling, per seed.

    Reads only the `baseline` section, which every report carries, so any block will do. It was
    pinned to `C-baseline`, a block that no longer exists, and so had stopped drawing entirely.
    """
    reports = load(runs_dir, block)
    if reports is None:
        return
    if isinstance(reports, dict):
        reports = [reports]

    received = [r["baseline"]["text_reception_mean"] for r in reports]
    ceiling = [r["baseline"]["reach_ceiling_mean"] for r in reports]
    beyond = [r["baseline"]["missed_beyond_hop_limit"] for r in reports]
    within = [r["baseline"]["missed_within_reach"] for r in reports]
    labels = [str(r["seed"])[:6] for r in reports]

    fig, ax = plt.subplots(figsize=(7.6, 4.4), facecolor=BG)
    ax.set_facecolor(BG)
    x = range(len(labels))
    ax.bar(x, received, 0.62, color=COOL, label="heard")
    ax.bar(
        x,
        within,
        0.62,
        bottom=received,
        color=ACCENT,
        label="lost inside the hop limit",
    )
    ax.bar(
        x,
        beyond,
        0.62,
        bottom=[received[i] + within[i] for i in range(len(x))],
        color="#D8D2C6",
        label="beyond the hop limit",
    )
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, fontsize=9)
    style(
        ax,
        "What an ordinary node hears, with no SF++ in the mesh",
        "seed",
        "share of text broadcasts",
    )
    ax.set_ylim(0, 1)
    ax.legend(frameon=False, fontsize=9, loc="lower right")
    fig.tight_layout()
    save(fig, out_dir, "baseline")


def fig_placements(out_dir, seed=20260817, nodes=60, area=8000.0, servers=3):
    """The same mesh five times, with the servers put somewhere different each time.

    Placement is the one arm whose result is meaningless as a number without the picture: knowing
    that 'beside a router' holds less than 'two hops apart' says nothing until you can see that the
    first arrangement puts three servers inside one neighbourhood.
    """
    import random

    from . import mesh as M
    from .campaign import Placement

    strategies = [
        "routers",
        "alternate-routers",
        "beside-router",
        "random-clients",
        "hops-apart",
    ]
    fig, axes = plt.subplots(
        1, len(strategies), figsize=(4.0 * len(strategies), 4.3), facecolor=BG
    )

    for ax, strategy in zip(axes, strategies):
        rng = random.Random(seed)
        conf = M.make_config()
        mesh = M.build(conf, nodes, area, rng, hop_limit=3, router_fraction=0.1)
        chosen = Placement.BY_NAME[strategy](mesh, servers, rng, 2)
        ax.set_facecolor(BG)

        for i in range(nodes):
            for j in mesh.neighbours[i]:
                if j > i:
                    ax.plot(
                        [mesh.nodes[i].x / 1000, mesh.nodes[j].x / 1000],
                        [mesh.nodes[i].y / 1000, mesh.nodes[j].y / 1000],
                        color="#c9d6e0",
                        linewidth=0.4,
                        zorder=1,
                    )
        xs = [n.x / 1000 for n in mesh.nodes]
        ys = [n.y / 1000 for n in mesh.nodes]
        roles = ["#8FA8B8" if n.role == M.ROUTER else "#D8D2C6" for n in mesh.nodes]
        ax.scatter(xs, ys, c=roles, s=26, zorder=2, edgecolors="none")
        ax.scatter(
            [mesh.nodes[i].x / 1000 for i in chosen],
            [mesh.nodes[i].y / 1000 for i in chosen],
            c=ACCENT,
            s=110,
            marker="s",
            zorder=3,
            edgecolors="none",
        )
        depth = mesh.hops_from([chosen[0]])
        seps = [depth.get(c, -1) for c in chosen[1:]]
        ax.set_title(f"{strategy}\nseparation {seps}", fontsize=10, color=INK)
        ax.set_xticks([])
        ax.set_yticks([])
        for spine in ax.spines.values():
            spine.set_color(GRID)
    fig.tight_layout()
    save(fig, out_dir, "placements")


def save(fig, out_dir, name):
    os.makedirs(out_dir, exist_ok=True)
    for ext in ("svg", "png"):
        fig.savefig(
            os.path.join(out_dir, f"{name}.{ext}"),
            facecolor=BG,
            dpi=150 if ext == "png" else None,
        )
    plt.close(fig)
    print(f"wrote {name}.svg / .png")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument(
        "--placements", action="store_true", help="draw the placement strategies"
    )
    ap.add_argument(
        "--baseline-block",
        default="Q-control",
        help="which block's `baseline` section the reach figure is drawn from; any block will do",
    )
    opts = ap.parse_args(argv)
    if plt is None:
        print("matplotlib is not installed - see requirements.txt; drawing nothing")
        return 1
    if opts.placements:
        fig_placements(opts.out)
    fig_baseline(opts.runs, opts.out, opts.baseline_block)
    for fn in (
        fig_cadence,
        fig_resolve,
        fig_capacity,
        fig_loss,
        fig_topology,
    ):
        try:
            fn(opts.runs, opts.out)
        except Exception as exc:  # a missing block must not sink the rest
            print(f"{fn.__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
