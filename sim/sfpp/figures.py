"""Figures from the sweep's run JSONs.

Each block wrote one JSON of per-cell reports; these turn them into the pictures the write-up needs.
Nothing here recomputes anything - if a number is not in the JSON it does not appear on a chart.

Usage, from sim/:
    python3 -m sfpp.figures --runs <dir> --out <dir>
"""

import argparse
import json
import os
import statistics

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

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
    path = os.path.join(runs_dir, f"{block}.json")
    if not os.path.exists(path):
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
    reports = load(runs_dir, "F-loss")
    if not reports:
        return
    by_capacity = {}
    for report in reports:
        if "sfpp" not in report:
            continue
        by_capacity.setdefault(report["opts"]["capacity"], []).append(report)

    fig, ax = plt.subplots(figsize=(7.4, 4.4), facecolor=BG)
    ax.set_facecolor(BG)
    colours = [COOL, ACCENT, "#4E86A8", "#7FB0CB"]
    for i, (capacity, group) in enumerate(sorted(by_capacity.items())):
        grouped = cells(group)
        losses = sorted(grouped)
        held = [mean(grouped[loss], ("sfpp", "held_fraction_mean")) for loss in losses]
        ax.plot(
            losses,
            held,
            "o-",
            color=colours[i % len(colours)],
            linewidth=2,
            label=f"capacity {capacity}",
        )
    style(
        ax,
        "Capacity against added packet loss",
        "added loss floor",
        "fraction of chain held",
    )
    ax.set_ylim(0, 1)
    ax.legend(frameon=False, fontsize=9)
    fig.tight_layout()
    save(fig, out_dir, "capacity-vs-loss")


def fig_topology(runs_dir, out_dir):
    place = load(runs_dir, "G-place")
    hops = load(runs_dir, "G-hops")
    if not place and not hops:
        return
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.4), facecolor=BG)
    for ax in axes:
        ax.set_facecolor(BG)

    if place:
        grouped = cells(place)
        labels = list(grouped)
        held = [mean(v, ("sfpp", "held_fraction_mean")) for v in grouped.values()]
        union = [mean(v, ("sfpp", "union_fraction")) for v in grouped.values()]
        x = range(len(labels))
        axes[0].bar([i - 0.19 for i in x], held, 0.38, color=COOL, label="per server")
        axes[0].bar([i + 0.19 for i in x], union, 0.38, color=ACCENT, label="union")
        axes[0].set_xticks(list(x))
        axes[0].set_xticklabels(labels, fontsize=8, rotation=20, ha="right")
        style(axes[0], "Where the servers go", "", "fraction of chain held")
        axes[0].set_ylim(0, 1)
        axes[0].legend(frameon=False, fontsize=9)

    if hops:
        grouped = cells(hops)
        separations = sorted(grouped)
        held = [mean(grouped[h], ("sfpp", "held_fraction_mean")) for h in separations]
        air = [
            100 * mean(grouped[h], ("sfpp", "sr_airtime_share")) for h in separations
        ]
        axes[1].plot(separations, held, "o-", color=COOL, linewidth=2, label="held")
        twin = axes[1].twinx()
        twin.plot(
            separations, air, "s--", color=ACCENT, linewidth=1.6, label="SR airtime %"
        )
        twin.tick_params(colors=MUTED, labelsize=9)
        twin.spines["top"].set_visible(False)
        style(
            axes[1],
            "Server separation, in hops",
            "hops apart",
            "fraction of chain held",
        )
        axes[1].set_ylim(0, 1)
    fig.tight_layout()
    save(fig, out_dir, "topology")


def fig_baseline(runs_dir, out_dir):
    path = os.path.join(runs_dir, "C-baseline.json")
    if not os.path.exists(path):
        return
    with open(path) as f:
        reports = json.load(f)
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
    opts = ap.parse_args(argv)
    for fn in (
        fig_baseline,
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
