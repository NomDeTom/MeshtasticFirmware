#!/usr/bin/env python3
"""How often is it worth advertising, and is advertising worth it at all?

Two questions this answers, both in *total mesh airtime*, which is what duty cycle and
channel utilisation actually care about:

  1. Push or pull? A sketch exchange can end with the behind node *requesting* what it
     lacks, or with the ahead node simply *sending* it. XORing two sketches yields the
     symmetric difference as a set of short IDs; a node splits that set by looking each ID
     up locally - the ones it holds are what the peer lacks, so it pushes those, and the
     ones it does not hold are what it lacks itself. No request is needed in either
     direction, and one advert resolves both ways at once.

  2. How many adverts per new message? Advertising is paid by every node on every
     interval whether or not anything changed: N nodes at f adverts an hour costs
     N * f * advert bytes. Repetition is paid once by the sender and heard by everyone:
     lambda messages at k copies costs lambda * k * object. The first scales with the
     size of the mesh, the second does not - which is the whole argument, and it does not
     favour advertising.

The comparison that matters is not "which is cheaper" but "cheaper for what recovery
window". Repetition only helps a node that was listening while the copies went out. A
node that was off for six hours needs either a repeat schedule spanning six hours or a
reconciliation exchange when it returns.

Usage: advertising.py [--nodes 20] [--outdir .]
"""

import argparse
import json
import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import radio  # noqa: E402
from model import WIRE  # noqa: E402

MEAN_TEXT_BYTES = 52.9  # measured over 1,455 real text packets
OBJECT = MEAN_TEXT_BYTES + WIRE["sfpp_overhead"]
SMALL_ADVERT = WIRE["sr_envelope"] + WIRE["sr_checksum"] + WIRE["short_id"] * 4
FULL_ADVERT = WIRE["sr_envelope"] + WIRE["sr_checksum"] + WIRE["short_id"] * 32
ANNOUNCE = 40  # today's CANON_ANNOUNCE
PRESET = "LongFast"


def id_list_bytes(window_objects):
    """A plain short-ID list of everything in the window."""
    return WIRE["sr_envelope"] + WIRE["short_id"] * window_objects


# --- Push against pull ---------------------------------------------------------------


def pull_exchange_bytes(d, advert=SMALL_ADVERT):
    """Advert, then the behind node asks for what it decoded, then the objects."""
    if d == 0:
        return advert, 0
    return advert + WIRE["sr_envelope"] + WIRE["short_id"] * d + d * OBJECT, 1


def push_exchange_bytes(d, advert=SMALL_ADVERT, duplicate_senders=1):
    """Advert, then whoever holds the missing objects simply sends them.

    duplicate_senders models suppression: 1 is perfect suppression, higher is every
    hearer answering. Push has no requester to address, so suppression is doing more work
    here than in the pull case - which is the risk this trades the round trip for.
    """
    if d == 0:
        return advert, 0
    return advert + duplicate_senders * d * OBJECT, 0


# --- Advertising against repetition --------------------------------------------------


def repetition_cost(lam, copies):
    """Sender-side cost per hour, including the original transmission. One broadcast
    serves every listener, so the node count does not appear - which is why blind
    repetition is hard to beat on a small mesh."""
    return lam * copies * OBJECT


def recovery_fraction(miss_rate, nodes):
    """Share of messages that need a repair push at all.

    A push is a broadcast, so one transmission serves every node in earshot that missed
    the object - what matters is whether *anyone* missed it, not how many did. That
    saturates: at 20 nodes and a 15% miss rate almost every message needs one push, while
    at 2 nodes only a quarter do. Advertising has no such ceiling, which is why earshot
    size decides this comparison.
    """
    return 1.0 - (1.0 - miss_rate) ** nodes


def reconciliation_cost(lam, adverts_per_hour, nodes, miss_rate, advert=SMALL_ADVERT):
    """Every node advertises, and every message anyone missed is pushed once.

    The original transmission is counted here too. Both strategies must put the message
    on the air once; leaving it out of one side and not the other silently compares k
    copies against k-1, which is a whole copy of every message.
    """
    original = lam * OBJECT
    advertising = nodes * adverts_per_hour * advert
    recovery = lam * recovery_fraction(miss_rate, nodes) * OBJECT
    return original + advertising + recovery


def advert_budget(nodes, copies, miss_rate=0.15, advert=SMALL_ADVERT):
    """Adverts per new message affordable before repeating each message `copies` times
    is cheaper. Independent of lambda: both sides scale with it."""
    headroom = copies - 1 - recovery_fraction(miss_rate, nodes)
    return max(headroom, 0.0) * OBJECT / (nodes * advert)


def id_list_cost(lam, adverts_per_hour, nodes, miss_rate, window_hours=6):
    window_objects = max(1, int(lam * window_hours))
    advertising = nodes * adverts_per_hour * id_list_bytes(window_objects)
    return lam * OBJECT + advertising + lam * miss_rate * OBJECT


def repeats_for_window(window_hours, spacing_hours):
    """Copies needed for a message to still be recoverable window_hours later."""
    return max(1, int(window_hours / spacing_hours) + 1)


def draw_utilisation(ax, fraction=0.5, frame=OBJECT, preset=PRESET, label=True):
    """Where the channel would be `fraction` occupied by this traffic alone."""
    y = radio.bytes_per_hour_at_utilisation(fraction, frame, preset)
    ax.axhline(y, color="#444", lw=1.3, ls=(0, (6, 3)))
    if label:
        ax.annotate(
            f"{fraction:.0%} channel utilisation at {preset}\n({y:,.0f} B/h in {frame:.0f} B frames)",
            xy=(ax.get_xlim()[1], y),
            xytext=(-6, 5),
            textcoords="offset points",
            ha="right",
            fontsize=7.5,
            color="#444",
        )
    return y


# --- Charts --------------------------------------------------------------------------


def chart_push_vs_pull(outdir):
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.6))
    ds = list(range(0, 9))

    pull = [pull_exchange_bytes(d)[0] for d in ds]
    push = [push_exchange_bytes(d)[0] for d in ds]
    push3 = [push_exchange_bytes(d, duplicate_senders=3)[0] for d in ds]

    axes[0].plot(
        ds,
        pull,
        color="#2E7D5B",
        lw=1.8,
        marker="o",
        ms=4,
        label="pull: advert, request, objects",
    )
    axes[0].plot(
        ds,
        push,
        color="#7A4FA3",
        lw=1.8,
        marker="o",
        ms=4,
        label="push: advert, objects",
    )
    axes[0].plot(
        ds,
        push3,
        color="#B4472F",
        lw=1.6,
        ls="--",
        label="push with 3 unsuppressed senders",
    )
    axes[0].set_title("Bytes per exchange", fontsize=11)
    axes[0].set_ylabel("bytes")

    axes[1].plot(
        ds,
        [pull_exchange_bytes(d)[1] for d in ds],
        color="#2E7D5B",
        lw=1.8,
        marker="o",
        ms=4,
        label="pull",
    )
    axes[1].plot(
        ds,
        [push_exchange_bytes(d)[1] for d in ds],
        color="#7A4FA3",
        lw=1.8,
        marker="o",
        ms=4,
        label="push",
    )
    axes[1].set_title("Round trips the behind node waits for", fontsize=11)
    axes[1].set_ylabel("round trips")
    axes[1].set_ylim(-0.2, 1.5)

    for ax in axes:
        ax.set_xlabel("objects the peer is missing")
        ax.grid(alpha=0.25, lw=0.6)
        ax.legend(fontsize=8, frameon=False)

    fig.suptitle("Requesting what you lack, against being sent it unasked", fontsize=12)
    fig.tight_layout()
    path = os.path.join(outdir, "push-vs-pull.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def chart_adverts_per_message(nodes, outdir):
    """The headline: advertising scales with the mesh, repetition does not."""
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.8))

    for ax, lam in zip(axes, (2, 20)):
        ratios = [x / 20 for x in range(1, 121)]  # adverts per new message
        recon = [reconciliation_cost(lam, r * lam, nodes, 0.15) for r in ratios]
        recon_full = [
            reconciliation_cost(lam, r * lam, nodes, 0.15, advert=FULL_ADVERT)
            for r in ratios
        ]
        ax.plot(
            ratios,
            recon,
            color="#7A4FA3",
            lw=1.9,
            label=f"advert+push, {SMALL_ADVERT} B advert",
        )
        ax.plot(
            ratios,
            recon_full,
            color="#2E7D5B",
            lw=1.6,
            ls="--",
            label=f"advert+push, {FULL_ADVERT} B advert",
        )

        for copies, colour in ((2, "#B4472F"), (3, "#D08A70"), (4, "#E3B9A8")):
            cost = repetition_cost(lam, copies)
            ax.axhline(cost, color=colour, lw=1.4, ls=":")
            ax.annotate(
                f"just send it {copies}x  ({cost:.0f} B/h)",
                xy=(ratios[-1], cost),
                xytext=(-4, 3),
                textcoords="offset points",
                ha="right",
                fontsize=7.5,
                color=colour,
            )

        # Where advertising stops being affordable against sending it three times.
        budget = repetition_cost(lam, 3)
        be = next((r for r, c in zip(ratios, recon) if c > budget), None)
        if be:
            ax.axvline(be, color="#666", ls="-.", lw=1)
            ax.annotate(
                f"{be:.2f} adverts\nper message",
                xy=(be, budget * 1.25),
                xytext=(5, 0),
                textcoords="offset points",
                fontsize=8,
                color="#333",
            )

        ax.set_title(f"{lam} new messages/hour, {nodes} nodes advertising", fontsize=11)
        ax.set_xlabel("adverts per new message")
        ax.set_ylabel("total mesh bytes per hour")
        draw_utilisation(ax)
        ax.set_yscale("log")
        ax.grid(alpha=0.25, lw=0.6, which="both")
        ax.legend(fontsize=8, frameon=False, loc="upper left")

    fig.suptitle(
        "Advertising is paid by every node on every interval; repetition is paid once by the sender",
        fontsize=12,
    )
    fig.tight_layout()
    path = os.path.join(outdir, "adverts-per-message.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def chart_recovery_window(nodes, outdir):
    """Repetition only reaches nodes that were listening. This is what buys the sketch
    its place: recovery long after the fact, at a cost that does not grow with the
    window."""
    fig, ax = plt.subplots(figsize=(8.5, 4.8))
    windows = [0.5, 1, 2, 4, 8, 12, 24, 48]
    lam = 2

    for spacing, colour in ((0.25, "#B4472F"), (1.0, "#D08A70")):
        costs = [repetition_cost(lam, repeats_for_window(w, spacing)) for w in windows]
        ax.plot(
            windows,
            costs,
            color=colour,
            lw=1.8,
            marker="o",
            ms=4,
            label=f"repeat every {spacing:g} h until the window closes",
        )

    for f, colour in ((1.0, "#7A4FA3"), (0.25, "#4C3070")):
        cost = reconciliation_cost(lam, f, nodes, 0.15)
        ax.axhline(
            cost,
            color=colour,
            lw=1.6,
            ls="--",
            label=f"advert+push at {f:g}/hour ({cost:.0f} B/h, any window)",
        )

    draw_utilisation(ax)
    ax.set_xscale("log", base=2)
    ax.set_xlabel(
        "how long after sending a message it must still be recoverable (hours)"
    )
    ax.set_ylabel("total mesh bytes per hour")
    ax.set_title(f"{lam} new messages/hour, {nodes} nodes", fontsize=11)
    ax.grid(alpha=0.25, lw=0.6, which="both")
    ax.legend(fontsize=8, frameon=False)
    fig.suptitle(
        "Reconciliation earns its keep on the recovery window, not on the byte count",
        fontsize=12,
    )
    fig.tight_layout()
    path = os.path.join(outdir, "recovery-window.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def chart_earshot(outdir):
    """Everything above assumed 20 nodes in earshot. That is a dense mesh, and it is the
    worst case for advertising: cost scales with earshot while repetition does not, and
    the repair term saturates. Sparse deployments change the answer.
    """
    fig, axes = plt.subplots(1, 2, figsize=(13.5, 4.8))
    counts = [1, 2, 3, 5, 8, 12, 20, 30, 40]

    for copies, colour in ((2, "#B4472F"), (3, "#7A4FA3"), (4, "#3B6EA8")):
        budgets = [advert_budget(n, copies) for n in counts]
        axes[0].plot(
            counts,
            budgets,
            color=colour,
            lw=1.9,
            marker="o",
            ms=4,
            label=f"vs sending each message {copies}x",
        )

    axes[0].axhline(1.0, color="#444", lw=1.2, ls=(0, (6, 3)))
    axes[0].annotate(
        "one advert per new message",
        xy=(counts[-1], 1.0),
        xytext=(-6, 5),
        textcoords="offset points",
        ha="right",
        fontsize=7.5,
        color="#444",
    )
    axes[0].axhline(6.0, color="#999", lw=1.2, ls=":")
    axes[0].annotate(
        "SFPP today at 2 msg/h (6 adverts/message)",
        xy=(counts[-1], 6.0),
        xytext=(-6, 5),
        textcoords="offset points",
        ha="right",
        fontsize=7.5,
        color="#999",
    )
    axes[0].set_yscale("log")
    axes[0].set_title("Advert budget against earshot size", fontsize=11)
    axes[0].set_ylabel("adverts per new message still affordable")

    lam = 2
    for f, colour in ((1.0, "#7A4FA3"), (12.0, "#2E7D5B")):
        costs = [reconciliation_cost(lam, f, n, 0.15) for n in counts]
        axes[1].plot(
            counts,
            costs,
            color=colour,
            lw=1.9,
            marker="o",
            ms=4,
            label=f"advert+push at {f:g}/hour",
        )
    for copies, colour in ((2, "#B4472F"), (3, "#D08A70")):
        axes[1].axhline(
            repetition_cost(lam, copies),
            color=colour,
            lw=1.4,
            ls=":",
            label=f"just send it {copies}x",
        )
    axes[1].set_yscale("log")
    axes[1].set_title(f"Total mesh bytes per hour, {lam} messages/hour", fontsize=11)
    axes[1].set_ylabel("bytes per hour")

    for ax in axes:
        ax.set_xscale("log", base=2)
        ax.set_xlabel("nodes in earshot, all advertising")
        ax.grid(alpha=0.25, lw=0.6, which="both")
        ax.legend(fontsize=8, frameon=False)

    fig.suptitle(
        "Advertising cost scales with earshot; repetition does not, and the repair term saturates",
        fontsize=12,
    )
    fig.tight_layout()
    path = os.path.join(outdir, "earshot.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodes", type=int, default=20)
    ap.add_argument("--outdir", default=os.path.dirname(os.path.abspath(__file__)))
    args = ap.parse_args()
    # Charts are written straight into it, so a directory that is only named must be made.
    os.makedirs(args.outdir, exist_ok=True)

    charts = [
        chart_push_vs_pull(args.outdir),
        chart_adverts_per_message(args.nodes, args.outdir),
        chart_recovery_window(args.nodes, args.outdir),
        chart_earshot(args.outdir),
    ]

    lam = 2
    summary = {
        "object_bytes": round(OBJECT, 1),
        "small_advert_bytes": SMALL_ADVERT,
        "full_advert_bytes": FULL_ADVERT,
        "push_saves_per_exchange": {
            d: round(pull_exchange_bytes(d)[0] - push_exchange_bytes(d)[0])
            for d in (1, 2, 4, 8)
        },
        "push_round_trips": 0,
        "pull_round_trips": 1,
        "adverts_per_message_budget_vs_3x_repeat": {
            "nodes": args.nodes,
            "lambda_per_hour": lam,
            "budget_bytes_per_hour": round(repetition_cost(lam, 3)),
            "max_adverts_per_message": round(advert_budget(args.nodes, 3), 3),
            "by_earshot_vs_3x": {
                n: round(advert_budget(n, 3), 2) for n in (1, 2, 3, 5, 8, 12, 20, 40)
            },
            "by_earshot_vs_2x": {
                n: round(advert_budget(n, 2), 2) for n in (1, 2, 3, 5, 8, 12, 20, 40)
            },
            "sfpp_today_adverts_per_message": round(12 / lam, 1),
        },
        "airtime_s_per_hour": {
            "advert_push_1_per_hour": round(
                args.nodes * radio.airtime(SMALL_ADVERT, PRESET)
                + lam * 0.15 * radio.airtime(OBJECT, PRESET),
                1,
            ),
            "advert_push_12_per_hour": round(
                args.nodes * 12 * radio.airtime(SMALL_ADVERT, PRESET)
                + lam * 0.15 * radio.airtime(OBJECT, PRESET),
                1,
            ),
            "repeat_3x": round(lam * 3 * radio.airtime(OBJECT, PRESET), 1),
        },
        "charts": [os.path.basename(c) for c in charts],
    }
    json.dump(summary, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
