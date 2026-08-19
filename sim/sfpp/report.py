"""Statistics harness: what a mesh scenario actually delivered, per portnum, text first.

Every number this campaign has quoted was a mean, and a mean is the wrong statistic for the question
the design exists to answer. The node that hears almost nothing is the one an archive is for, and
averaging it together with a well-sited node describes neither. So everything here is a distribution,
and two figures a mean cannot express are called out separately: how many nodes received *none* of a
class, and how many nodes an archive did nothing for.

`text` is the archived class and is printed first and marked. The other classes are here because they
set the contention text competes with, and because a per-class table is the only way to tell whether
reconciliation displaces text or telemetry.

Usage, from sim/:
    python3 -m sfpp.report --run <one.json>
    python3 -m sfpp.report --runs <dir>          # every block, summarised
"""

import argparse
import glob
import json
import os
import statistics

PORTNUMS = {
    "text": 1,
    "position": 3,
    "nodeinfo": 4,
    "telemetry": 67,
    "sr:advert": 35,
    "sr:item_provide": 35,
    "chain:announce": 35,
}
BAR = "█"


def load(path):
    with open(path) as f:
        d = json.load(f)
    return d if isinstance(d, list) else [d]


def sparkline(dist, width=22):
    """A crude visual of where the mass sits between 0 and 1, so a tail is visible in plain text."""
    if not dist:
        return ""
    lo, hi = dist["min"], dist["max"]
    cells = []
    for key in ("min", "p10", "median", "mean", "p90", "max"):
        v = dist.get(key, 0)
        cells.append(BAR if v >= 0.999 else " ▁▂▃▄▅▆▇█"[min(8, int(v * 8.999))])
    return "".join(cells)


def scenario_line(r):
    m, o = r["mesh"], r["opts"]
    diam = m.get("diameter")
    return (
        f"{o.get('topology','uniform')} · {m['nodes']} nodes · {m['area_km']:.1f} km · "
        f"degree {m['mean_degree']:.1f} · diameter {diam if diam is not None else 'FRAGMENTED'} · "
        f"{o.get('preset','LONG_FAST')} · hop {'spread 3-7' if o.get('hop_spread') else o.get('hop_limit')} · "
        f"protocol {o.get('protocol','sr')} · {o.get('servers',0)} archives"
    )


def report_one(r, indent=""):
    lines = []
    a = lines.append
    a(f"{indent}{scenario_line(r)}")
    t = r["traffic"]
    a(
        f"{indent}  aggregate demand {t['channel_utilisation']:.2f}x"
        + " · "
        + (
        f"{t['transmissions']} transmissions · {t['queue_drops']} queue drops"
        )
        + (
            "  ← CHECK: drops are a large share of transmissions"
            if t["queue_drops"] > 0.2 * max(1, t["transmissions"])
            else ""
        )
    )
    # Two different measurements over two different windows, so they get a line each rather than
    # being averaged into one "utilisation": chutil is what a node HEARD busy over sixty seconds,
    # air-util-TX is what it SENT over sixty minutes. The spread is the point in both - a median
    # says nothing about the repeater carrying the mesh, and a duty cycle binds per device.
    for key, label, unit in (
        ("node_channel_util_percent", "node chutil  ", "%"),
        ("node_air_util_tx_percent", "node airutil ", "%"),
    ):
        d = t.get(key)
        if d:
            a(
                f"{indent}  {label} min {d['min']:.1f}{unit}  p10 {d['p10']:.1f}{unit}"
                f"  mean {d['mean']:.1f}{unit}  p90 {d['p90']:.1f}{unit}  max {d['max']:.1f}{unit}"
            )

    by = r.get("by_class") or {}
    if by:
        a("")
        a(
            f"{indent}  {'class':<11}{'port':>5}{'sent':>7}{'recv':>9}"
            f"{'  min':>7}{'p10':>7}{'med':>7}{'mean':>7}{'p90':>7}{'max':>7}  {'none':>5}  {'air':>6}  shape"
        )
        # Archived class first, then the rest, then the all-portnum aggregate last as a rule-off.
        order = sorted(
            (k for k in by if k != "all"), key=lambda k: (not by[k].get("archived"), k)
        )
        for name in order + (["all"] if "all" in by else []):
            c = by[name]
            d = c.get("per_node_reception") or {}
            mark = "»" if c.get("archived") else " "
            if name == "all":
                a(f"{indent}  {'-' * 78}")
                mark = "Σ"
            a(
                f"{indent}{mark} {name:<11}{PORTNUMS.get(name,'-'):>5}{c['originated']:>7}"
                f"{c['receptions']:>9}"
                f"{d.get('min',0):>7.3f}{d.get('p10',0):>7.3f}{d.get('median',0):>7.3f}"
                f"{d.get('mean',0):>7.3f}{d.get('p90',0):>7.3f}{d.get('max',0):>7.3f}"
                f"{c.get('nodes_receiving_none',0):>6}"
                f"{c['airtime_share']:>7.1%}  {sparkline(d)}"
            )
        a(
            f"{indent}  » = the archived class · Σ = every portnum together · "
            f"'none' = nodes that received not one packet of it."
        )
        # p10 against p90 is the pair to read: an arm that lifts p90 and leaves p10 alone has helped
        # the nodes that were already fine.
        for name in ("text", "all"):
            if name in by:
                d = by[name].get("per_node_reception") or {}
                a(
                    f"{indent}  {name.upper():<5} tail        p10 {d.get('p10',0):.3f}  "
                    f"median {d.get('median',0):.3f}  p90 {d.get('p90',0):.3f}  "
                    f"(spread {d.get('p90',0) - d.get('p10',0):.3f})"
                )

    b = r["baseline"]
    a("")
    a(
        f"{indent}  TEXT reach          min {b['text_reception_min']:.3f}  median {b['text_reception_median']:.3f}"
        f"  mean {b['text_reception_mean']:.3f}  max {b['text_reception_max']:.3f}"
    )
    a(
        f"{indent}  routing ceiling     {b['reach_ceiling_mean']:.3f}"
        f"   (beyond it {b['missed_beyond_hop_limit']:.3f}, lost inside it {b['missed_within_reach']:.3f})"
    )
    # A primary line, beside text reach, because a DM is judged at the node it was addressed to and
    # the broadcast figure says nothing about it. Absent only when no DMs were asked for.
    dm = r.get("dm")
    if dm:
        flag = ""
        if dm["reception"] < 0.5 and dm["composed"]:
            flag = "   <- under half of composed DMs arrived"
        a(
            f"{indent}  DM success          {dm['reception']:.3f}"
            f"  ({dm['delivered']}/{dm['composed']} composed"
            + (f", {dm['no_key']} no key" if dm["no_key"] else "")
            + (
                f", {dm['no_addressable_peer']} no addressable peer"
                if dm["no_addressable_peer"]
                else ""
            )
            + f") · of all attempted {dm['reception_of_attempted']:.3f}"
            + flag
        )
        h, lat = dm.get("hops"), dm.get("latency_ms")
        if h and lat:
            a(
                f"{indent}  DM delivered        hops p10 {h['p10']:.1f} median {h['median']:.1f}"
                f" p90 {h['p90']:.1f} max {h['max']:.0f}"
                f" · latency median {lat['median']/1000:.1f}s p90 {lat['p90']/1000:.1f}s"
            )
        a(
            f"{indent}  DM population       {dm['emitting_nodes']} emitting of "
            f"{dm['originating_nodes']} attended of {dm['eligible_nodes']} eligible"
            + ("  · archived" if dm["archived"] else "  · not archived (contention only)")
        )

    # Can an operator configure a node this far out, and when not, what stopped it. Primary, beside
    # DM success: a mesh whose text reach looks healthy can still be one where nothing past two hops
    # can be administered, and the reason matters more than the rate.
    admin = r.get("admin")
    if admin:
        a("")
        for hops in sorted(admin, key=int):
            d = admin[hops]
            if not d.get("sessions"):
                continue
            why = ", ".join(
                f"{k.replace('_', ' ')} {v}"
                for k, v in (d.get("failed_because") or {}).items()
                if v
            )
            on = d.get("completed_on_attempt") or {}
            retried = sum(v for k, v in on.items() if k != "1")
            a(
                f"{indent}  ADMIN {hops} hop{'s' if hops != '1' else ' '}       "
                f"{d['success_rate']:.3f}  ({d['session_completed']}/{d['sessions']} sessions"
                f", {d['attempts_per_session']:.2f} tries each)"
                + (f" · needed a retry {retried}" if retried else "")
                + (f" · failed: {why}" if why else "")
            )
        first = admin.get(sorted(admin, key=int)[0]) if admin else None
        if first and not first.get("keys_preloaded"):
            a(f"{indent}  ADMIN assumption    keys NOT preloaded - no_key failures are reachable")

    s = r.get("sfpp")
    if s:
        u = s.get("structurally_unreachable") or {}
        dv = s.get("delivered_though_unreachable") or {}
        sh = s.get("per_node_share_of_unreachable_delivered") or {}
        if u:
            a("")
            a(f"{indent}  --- what only an archive could have delivered ---")
            a(
                f"{indent}  unreachable to a node   min {u['min']:.3f}  median {u['median']:.3f}  max {u['max']:.3f}"
            )
            a(
                f"{indent}  DELIVERED regardless    min {dv['min']:.3f}  median {dv['median']:.3f}  max {dv['max']:.3f}"
            )
            a(
                f"{indent}  per-node share of it    min {sh['min']:.1%}  median {sh['median']:.1%}  max {sh['max']:.1%}"
            )
            a(
                f"{indent}  nodes the archive did nothing for: "
                f"{s.get('nodes_with_zero_delivered')} of {s.get('nodes_measured')}"
            )
        a("")
        a(
            f"{indent}  archive: held {s['held_fraction_mean']:.3f} · union {s['union_fraction']:.3f} · "
            f"{s['adverts']} adverts · {s['objects_moved']} moved · {s['sr_airtime_share']:.1%} airtime · "
            f"bystander pickups {s.get('bystander_pickups',0)}"
        )
        silent = s["silent_losses"] + s.get("audit_checksum_agrees_sets_differ", 0)
        a(
            f"{indent}  SAFETY: silent losses {silent}"
            + ("   ← STOP, this falsifies the design" if silent else "  (gate holds)")
        )
    return "\n".join(lines)


def report_block(path):
    rs = load(path)
    name = os.path.basename(path).replace(".json", "")
    out = [f"\n{'=' * 100}", f"{name}   ({len(rs)} runs)", "=" * 100]
    arms = {}
    for r in rs:
        arms.setdefault(r.get("value", "-"), []).append(r)
    for value, group in arms.items():
        # group[0], not the loop variable left over from building `arms`: that one points at
        # whichever report came last in the file, and is only right by coincidence when every
        # report in the file shares an arm.
        out.append(f"\n--- {group[0].get('arm','value')} = {value}   ({len(group)} seeds) ---")
        out.append(report_one(group[0], indent="  "))
        if len(group) > 1:
            txt = [g["baseline"]["text_reception_mean"] for g in group]
            worst = [g["baseline"]["text_reception_min"] for g in group]
            out.append(
                f"    across seeds: text reach mean {statistics.mean(txt):.3f} "
                f"(sd {statistics.stdev(txt):.3f} over {len(txt)}), worst node {min(worst):.3f}"
            )
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--run")
    ap.add_argument("--runs")
    opts = ap.parse_args(argv)
    if opts.run:
        print(report_one(load(opts.run)[0]))
    if opts.runs:
        for path in sorted(glob.glob(os.path.join(opts.runs, "*.json"))):
            print(report_block(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
