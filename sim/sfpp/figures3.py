"""Figures for the SF++ sweep: mesh shapes, the pinned protocol result, and the coverage gap."""

import argparse
import json
import math
import os
import random
import statistics

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

from . import mesh as M  # noqa: E402
from .campaign import Placement  # noqa: E402

INK = "#1b1b1b"
MUTED = "#8a8a8a"
ACCENT = "#B4472A"
COOL = "#2E5E7E"
GRID = "#e3e3e0"
BG = "#FCFCFA"
# Set by main() from the command line. They were absolute paths into one machine's notes checkout,
# which meant this module drew nothing anywhere else and drew it on import when it did.
OUT = "figures3"
RUNS = "runs3"

# The pinned run the protocol comparison and the coverage gap are both read from.
PROTOCOL_RUN = "Q-protocol-hours-48.json"


def style(ax, t, x, y):
    ax.set_title(t, fontsize=11.5, color=INK, loc="left", pad=10)
    ax.set_xlabel(x, fontsize=9.5, color=MUTED)
    ax.set_ylabel(y, fontsize=9.5, color=MUTED)
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=9)


def save(fig, name):
    for ext in ("png", "svg"):
        fig.savefig(
            f"{OUT}/{name}.{ext}",
            facecolor=BG,
            dpi=150 if ext == "png" else None,
            bbox_inches="tight",
        )
    plt.close(fig)
    print("wrote", name)


# ---- 1. the four mesh shapes, with servers and per-node hop limits ----
def draw_mesh_shapes():
    """Four topologies side by side, coloured by each node's own hop limit."""
    shapes = ("uniform", "clustered", "corridor", "hub")
    fig, axes = plt.subplots(1, 4, figsize=(19, 5.0), facecolor=BG)
    for ax, shape in zip(axes, shapes):
        rng = random.Random(990001)
        conf = M.make_config()
        m = M.build(
            conf,
            60,
            8000.0,
            rng,
            hop_limit=3,
            router_fraction=0.1,
            topology=shape,
            hop_spread=True,
        )
        servers = Placement.BY_NAME["hops-apart"](m, 3, rng, 3)
        ax.set_facecolor(BG)
        for i in range(60):
            for j in m.neighbours[i]:
                if j > i:
                    ax.plot(
                        [m.nodes[i].x / 1000, m.nodes[j].x / 1000],
                        [m.nodes[i].y / 1000, m.nodes[j].y / 1000],
                        color="#c9d6e0",
                        linewidth=0.35,
                        zorder=1,
                    )
        hl = [m.hop_limit_for(i) for i in range(60)]
        sc = ax.scatter(
            [n.x / 1000 for n in m.nodes],
            [n.y / 1000 for n in m.nodes],
            c=hl,
            cmap="YlGnBu",
            vmin=3,
            vmax=7,
            s=[26 + 9 * (h - 3) for h in hl],
            zorder=2,
            edgecolors="none",
        )
        ax.scatter(
            [m.nodes[i].x / 1000 for i in servers],
            [m.nodes[i].y / 1000 for i in servers],
            facecolors="none",
            edgecolors=ACCENT,
            s=240,
            linewidths=2.2,
            zorder=3,
        )
        d = max(max(m.hops_from([i]).values()) for i in range(60))
        ax.set_title(
            f"{shape}\ndiameter {d} · mean degree {m.link_stats()['mean_degree']:.1f}",
            fontsize=10.5,
            color=INK,
        )
        # Equal aspect, or the corridor renders square and stops looking like a corridor - the whole
        # point of that shape is that it is long and thin.
        ax.set_aspect("equal", adjustable="datalim")
        ax.set_xticks([])
        ax.set_yticks([])
        for s in ax.spines.values():
            s.set_color(GRID)
    fig.colorbar(sc, ax=axes, fraction=0.012, pad=0.01, label="per-node hop limit")
    fig.suptitle(
        "The four simulated mesh shapes - 60 nodes, same seed, hop limits 3-7 by centrality; red rings are the three archives",
        fontsize=12,
        color=INK,
        x=0.5,
        y=1.06,
    )
    save(fig, "mesh-shapes")

    # ---- 2. the pinned protocol result ----


def _pinned_run():
    """The pinned protocol block both later figures read, or None when it is not present."""
    path = os.path.join(RUNS, PROTOCOL_RUN)
    if not os.path.exists(path):
        print(f"skipped: {path} not present - run Q-protocol first, or pass --runs")
        return None
    return json.load(open(path))


def mean_of(d, value, section, key):
    """Mean of one metric across the seeds of one cell. Shared by both figures below."""
    return statistics.mean(r[section][key] for r in d if r["value"] == value)


def draw_protocol_comparison(d):
    """Airtime, adverts and reception for no archive, the chain walk, and the sketch."""
    d = json.load(open(f"{RUNS}/Q-protocol-hours-48.json"))
    labels = ["none\n(no archive)", "chain\n(today's SF++)", "sr\n(sketch)"]
    fig, axes = plt.subplots(1, 3, figsize=(14, 4.3), facecolor=BG)
    for a in axes:
        a.set_facecolor(BG)
    air = [
        0,
        100 * mean_of(d, "chain", "sfpp", "sr_airtime_share"),
        100 * mean_of(d, "sr", "sfpp", "sr_airtime_share"),
    ]
    axes[0].bar(labels, air, color=[MUTED, ACCENT, COOL])
    style(axes[0], "Airtime it costs the mesh", "", "% of channel airtime")
    for i, v in enumerate(air):
        if v:
            axes[0].text(i, v + 0.4, f"{v:.1f}%", ha="center", fontsize=10, color=INK)
    adv = [0, mean_of(d, "chain", "sfpp", "adverts"), mean_of(d, "sr", "sfpp", "adverts")]
    axes[1].bar(labels, adv, color=[MUTED, ACCENT, COOL])
    style(axes[1], "Adverts sent - timer vs bucket-close", "", "adverts per run")
    for i, v in enumerate(adv):
        if v:
            axes[1].text(i, v + 30, f"{v:.0f}", ha="center", fontsize=10, color=INK)
    rec = [mean_of(d, v, "baseline", "text_reception_mean") for v in ("none", "chain", "sr")]
    axes[2].bar(labels, rec, color=[MUTED, ACCENT, COOL])
    axes[2].axhline(rec[0], color=MUTED, linestyle=":", linewidth=1.2)
    axes[2].set_ylim(0.7, 0.9)
    style(axes[2], "What a node still hears directly", "", "fraction of text heard")
    for i, v in enumerate(rec):
        axes[2].text(i, v + 0.004, f"{v:.3f}", ha="center", fontsize=10, color=INK)
    fig.suptitle(
        "Protocol comparison - 3 seeds, 48 h, transport pinned at 1bafcc7fe",
        fontsize=12,
        color=INK,
        x=0.5,
        y=1.02,
    )
    fig.tight_layout()
    save(fig, "protocol-comparison")


def draw_coverage_gap(d):
    """What an archive holds against what a node is ever shown to receive."""

    # ---- 3. the coverage gap: what is measured vs what is not ----
    fig, ax = plt.subplots(figsize=(9.5, 4.6), facecolor=BG)
    ax.set_facecolor(BG)
    node = mean_of(d, "sr", "baseline", "text_reception_mean")
    union = mean_of(d, "sr", "sfpp", "union_fraction")
    ax.barh(
        ["with SF++\n(archive union)", "ordinary node\n(measured)"],
        [node, node],
        color=COOL,
        height=0.55,
        label="heard directly by the node",
    )
    ax.barh(
        ["with SF++\n(archive union)"],
        [union - node],
        left=[node],
        color="#D8B26A",
        height=0.55,
        label="held by an archive, retrieval NOT modelled",
    )
    ax.barh(
        ["with SF++\n(archive union)", "ordinary node\n(measured)"],
        [1 - union, 1 - node],
        left=[union, node],
        color="#E4E0D8",
        height=0.55,
        label="in no archive and unheard",
    )
    ax.axvline(node, color=MUTED, linestyle=":", linewidth=1.2)
    ax.set_xlim(0, 1)
    style(
        ax,
        "Text coverage: what is demonstrated, and what is assumed",
        "fraction of all text broadcasts",
        "",
    )
    ax.legend(
        frameon=False, fontsize=9, loc="lower center", bbox_to_anchor=(0.5, -0.42), ncol=3
    )
    ax.annotate(
        f"{union-node:.3f} of all text sits in an archive\nand is never shown to reach a node",
        xy=(union, 0),
        xytext=(node - 0.30, 0.55),
        fontsize=9.5,
        color="#8a6d2f",
        arrowprops=dict(arrowstyle="->", color="#8a6d2f", lw=1.1),
    )
    fig.tight_layout()
    save(fig, "coverage-gap")


def main(argv=None):
    global OUT, RUNS
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--runs", default=RUNS, help="directory holding the sweep JSON")
    ap.add_argument("--out", default=OUT, help="directory to write the figures into")
    opts = ap.parse_args(argv)
    RUNS, OUT = opts.runs, opts.out
    os.makedirs(OUT, exist_ok=True)
    draw_mesh_shapes()
    d = _pinned_run()
    if d is not None:
        draw_protocol_comparison(d)
        draw_coverage_gap(d)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
