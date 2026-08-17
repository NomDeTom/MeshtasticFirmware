"""Charts rendered as a side effect of a run, not as a separate step afterwards.

Rendering by hand meant figures lagged the numbers, and twice today a number was withdrawn after a
figure had been made from it. A chart that is produced by the same call that produced the JSON cannot
drift from it, and it carries the transport commit in its own footer so it can never be read against
the wrong code.

Every per-node quantity is drawn as a distribution with the worst node marked, because a bar at the
mean is exactly the picture that hid the stranded nodes.

Called automatically by campaign.main() and sweep.run_block(); suppress with --no-charts.
"""

import json
import os
import subprocess

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

INK = "#1b1b1b"
MUTED = "#8a8a8a"
ACCENT = "#B4472A"
COOL = "#2E5E7E"
WARN = "#B8860B"
GRID = "#e3e3e0"
BG = "#FCFCFA"


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


def _style(ax, title, xlabel, ylabel):
    ax.set_title(title, fontsize=10.5, color=INK, loc="left", pad=8)
    ax.set_xlabel(xlabel, fontsize=9, color=MUTED)
    ax.set_ylabel(ylabel, fontsize=9, color=MUTED)
    ax.grid(True, color=GRID, linewidth=0.7)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=8.5)


def _spread(ax, labels, dists, title, ylabel, highlight=None):
    """Range bars from min to max, mean as a dash, worst node as a red dot."""
    xs = range(len(labels))
    for i, d in enumerate(dists):
        if not d:
            continue
        colour = ACCENT if (highlight and labels[i] == highlight) else COOL
        ax.vlines(i, d["min"], d["max"], color=colour, linewidth=7, alpha=0.28)
        ax.vlines(i, d["p10"], d["p90"], color=colour, linewidth=7, alpha=0.55)
        ax.plot([i - 0.22, i + 0.22], [d["mean"]] * 2, color=colour, linewidth=2)
        ax.plot([i], [d["min"]], "o", color=ACCENT, markersize=5, zorder=5)
    ax.set_xticks(list(xs))
    ax.set_xticklabels(labels, fontsize=8.5, rotation=20, ha="right")
    _style(ax, title, "", ylabel)


def render_run(report, out_dir, label="run"):
    """One scenario: per-class reception spread, and what only an archive delivered."""
    os.makedirs(out_dir, exist_ok=True)
    by = report.get("by_class") or {}
    s = report.get("sfpp") or {}
    has_stretch = bool(s.get("structurally_unreachable"))
    n = 2 + (1 if has_stretch else 0)
    fig, axes = plt.subplots(1, n, figsize=(5.6 * n, 4.5), facecolor=BG)
    if n == 1:
        axes = [axes]
    for ax in axes:
        ax.set_facecolor(BG)

    order = sorted(by, key=lambda k: (not by[k].get("archived"), k))
    _spread(
        axes[0],
        order,
        [by[k].get("per_node_reception") for k in order],
        "Reception per node, by class - red dot is the worst node",
        "share of that class received",
        highlight="text",
    )
    axes[0].set_ylim(0, 1)

    shares = [by[k]["airtime_share"] for k in order]
    axes[1].bar(order, shares, color=[ACCENT if k == "text" else COOL for k in order])
    axes[1].set_xticklabels(order, fontsize=8.5, rotation=20, ha="right")
    _style(axes[1], "Share of channel airtime, by class", "", "fraction of airtime")

    if has_stretch:
        _spread(
            axes[2],
            ["unreachable", "delivered\nanyway", "share\ndelivered"],
            [
                s["structurally_unreachable"],
                s["delivered_though_unreachable"],
                s["per_node_share_of_unreachable_delivered"],
            ],
            f"Only an archive could deliver these - {s.get('nodes_with_zero_delivered')} of "
            f"{s.get('nodes_measured')} nodes got none",
            "share of all text",
        )
        axes[2].set_ylim(0, 1)

    fig.suptitle(
        f"{label} - {report['opts'].get('topology','uniform')}, {report['mesh']['nodes']} nodes, "
        f"diameter {report['mesh'].get('diameter') or 'FRAGMENTED'}, "
        f"protocol {report['opts'].get('protocol','sr')}",
        fontsize=11.5,
        color=INK,
        y=1.02,
    )
    fig.text(
        0.995,
        -0.06,
        f"transport {_pin()} · seed {report.get('seed')} · {report['opts'].get('hours')} h",
        ha="right",
        fontsize=7.5,
        color=MUTED,
    )
    fig.tight_layout()
    path = os.path.join(out_dir, f"{label}.png")
    fig.savefig(path, facecolor=BG, dpi=140, bbox_inches="tight")
    plt.close(fig)
    return path


def render_block(reports, out_dir, name):
    """One arm: the swept value against text reach (worst node and mean) and archive airtime."""
    os.makedirs(out_dir, exist_ok=True)
    arms = {}
    for r in reports:
        arms.setdefault(str(r.get("value", "-")), []).append(r)
    labels = list(arms)
    mean_reach = [
        sum(g["baseline"]["text_reception_mean"] for g in arms[k]) / len(arms[k])
        for k in labels
    ]
    worst_reach = [
        min(g["baseline"]["text_reception_min"] for g in arms[k]) for k in labels
    ]
    air = [
        100
        * sum((g.get("sfpp") or {}).get("sr_airtime_share", 0) for g in arms[k])
        / len(arms[k])
        for k in labels
    ]

    fig, axes = plt.subplots(1, 2, figsize=(12.5, 4.4), facecolor=BG)
    for ax in axes:
        ax.set_facecolor(BG)
    xs = range(len(labels))
    axes[0].bar([i - 0.19 for i in xs], mean_reach, 0.38, color=COOL, label="mean node")
    axes[0].bar(
        [i + 0.19 for i in xs], worst_reach, 0.38, color=ACCENT, label="worst node"
    )
    axes[0].set_xticks(list(xs))
    axes[0].set_xticklabels(labels, fontsize=8.5, rotation=20, ha="right")
    axes[0].set_ylim(0, 1)
    _style(
        axes[0], "Text reach - mean against worst node", "", "share of text received"
    )
    axes[0].legend(frameon=False, fontsize=8.5)

    axes[1].bar(labels, air, color=WARN)
    axes[1].set_xticklabels(labels, fontsize=8.5, rotation=20, ha="right")
    _style(axes[1], "Reconciliation's share of channel airtime", "", "% of airtime")

    fig.suptitle(
        f"{name} - arm `{reports[0].get('arm','value')}`, {len(arms[labels[0]])} seeds per cell",
        fontsize=11.5,
        color=INK,
        y=1.03,
    )
    fig.text(0.995, -0.06, f"transport {_pin()}", ha="right", fontsize=7.5, color=MUTED)
    fig.tight_layout()
    path = os.path.join(out_dir, f"{name}.png")
    fig.savefig(path, facecolor=BG, dpi=140, bbox_inches="tight")
    plt.close(fig)
    return path


def auto(reports, out_json, kind="run"):
    """Render beside the JSON that produced it. Never raises - a chart must not fail a run."""
    try:
        base = os.path.dirname(os.path.abspath(out_json)) if out_json else "."
        figs = os.path.join(base, "figures")
        name = os.path.basename(out_json or "run").replace(".json", "")
        if kind == "block" and len(reports) > 1:
            return render_block(reports, figs, name)
        return render_run(reports[0], figs, name)
    except Exception as exc:
        print(f"  (chart skipped: {type(exc).__name__}: {exc})")
        return None
