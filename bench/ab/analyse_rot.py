"""Rotated-role sweeps (night_*.json): labels BLOCK_strategy_rotation, e.g. R2_cadrx4_dl.

  python analyse_rot.py FILE.jsonl [FILE.jsonl ...]

Per round i of a variant: whole = every sender's frame reached a non-sender listener; x-heard = a sender
decoded another sender's frame (it deferred and listened rather than talking over it).
  1. Per block and strategy, pooled over rotations: whole%, x-heard%, and how many rotations it won/lost vs base
     (or vs off where there is no base), with a Cochran-Mantel-Haenszel test stratified by rotation.
  2. Whole% by skew (airtimes between successive senders) per strategy: 0 = simultaneous (backoff's job),
     0.25-0.75 = later sender checks while the earlier one is on air (detection's job).
  3. Per node as sender: its frames delivered, per strategy (a node that is bad in every strategy is hardware).
  4. Anchor rows (A_base_*: dut+peer, branch default) over the night, for drift.
"""

import json
import math
import re
import sys
from collections import defaultdict
from itertools import permutations

PAT = re.compile(r"^([CJ])-([A-Z]{2})-(\w+?)-(\w+?)-(\d+)(?:-x*)?$")
sent, seen, skew_of, frac, t_of, senders_of = (
    defaultdict(set),
    defaultdict(set),
    {},
    {},
    {},
    defaultdict(set),
)
for f in sys.argv[1:]:
    for line in open(f, encoding="utf-8"):
        d = json.loads(line)
        if d["kind"] == "sround":
            offs = sorted(d["offsets"].values())
            skew_of[(d["label"], d["i"])] = (
                round(offs[1] - offs[0], 3) if len(offs) > 1 else 0
            )
            continue
        if d["kind"] == "variant":
            sk = d.get("skews")
            if sk:
                for i in range(int(d.get("pairs", 16))):
                    frac[(d["label"], i)] = sk[i % len(sk)]
            t_of[d["label"]] = d["ts"]
            continue
        m = PAT.match(d.get("text") or "")
        if not m or m.group(1) != "C" or m.group(3).count("_") != 2:
            continue
        lab, s, i = m.group(3), m.group(4), int(m.group(5))
        if d["kind"] == "tx":
            sent[lab].add((s, i))
            senders_of[lab].add(s)
        elif d["kind"] == "rx" and d["node"] != s:
            seen[lab].add((d["node"], s, i))


rounds = defaultdict(
    list
)  # (block, strat, rot) -> [(whole, xh, xn, skewfrac, {sender: delivered})]
for lab in sent:
    block, strat, rot = lab.split("_")
    ss = sorted(senders_of[lab])
    idx = sorted({i for _, i in sent[lab]})
    for i in idx:
        if not all((s, i) in sent[lab] for s in ss):
            continue
        heard = {
            s: any(
                (w, s, i) in seen[lab] for w in {n for n, _, _ in seen[lab]} - set(ss)
            )
            for s in ss
        }
        xh = sum((a, b, i) in seen[lab] for a, b in permutations(ss, 2))
        rounds[(block, strat, rot)].append(
            (
                all(heard.values()),
                xh,
                len(ss) * (len(ss) - 1),
                frac.get((lab, i)),
                heard,
            )
        )


def cmh(strata):
    """strata: [(a_ok, a_n, b_ok, b_n)] -> (MH odds ratio b:a, two-sided p) via CMH chi-square with continuity."""
    num = den = var = 0.0
    orn = ord_ = 0.0
    for a, an, b, bn in strata:
        n = an + bn
        if n < 2:
            continue
        m1 = a + b
        e = bn * m1 / n
        num += b - e
        var += an * bn * m1 * (n - m1) / (n * n * (n - 1)) if n > 1 else 0
        orn += b * (an - a) / n
        ord_ += a * (bn - b) / n
    if var == 0:
        return float("nan"), 1.0
    x = max(abs(num) - 0.5, 0) ** 2 / var
    return (orn / ord_ if ord_ else float("inf")), math.erfc(math.sqrt(max(x, 0) / 2))


blocks = sorted({b for b, _, _ in rounds} - {"A"})
for block in blocks:
    strats = sorted({s for b, s, _ in rounds if b == block})
    rots = sorted({r for b, _, r in rounds if b == block})
    ref = "base" if "base" in strats else "off"
    print(f"\n=== block {block}: {len(rots)} rotations, reference '{ref}'")
    print(
        f"{'strategy':10} {'whole':>15} {'x-heard':>15}  {'won/tied/lost vs ' + ref:>22}  {'MH OR':>6} {'p':>6}   per rotation whole%"
    )
    for s in strats:
        rr = [x for r in rots for x in rounds.get((block, s, r), [])]
        if not rr:
            continue
        w, n = sum(x[0] for x in rr), len(rr)
        xh, xn = sum(x[1] for x in rr), sum(x[2] for x in rr)
        wins = ties = losses = 0
        strata = []
        for r in rots:
            a, b = rounds.get((block, ref, r), []), rounds.get((block, s, r), [])
            if not a or not b:
                continue
            pa, pb = sum(x[0] for x in a) / len(a), sum(x[0] for x in b) / len(b)
            wins += pb > pa
            ties += pb == pa
            losses += pb < pa
            strata.append((sum(x[0] for x in a), len(a), sum(x[0] for x in b), len(b)))
        orr, p = cmh(strata) if s != ref else (float("nan"), float("nan"))
        per = " ".join(
            f"{r}:{100 * sum(x[0] for x in rounds[(block, s, r)]) / len(rounds[(block, s, r)]):3.0f}"
            for r in rots
            if rounds.get((block, s, r))
        )
        print(
            f"{s:10} {w:4}/{n:<4}({w / n:4.0%}) {xh:4}/{xn:<4}({xh / xn if xn else 0:4.0%})  "
            f"{'' if s == ref else f'{wins}/{ties}/{losses}':>22}  {orr:6.2f} {p:6.3f}   {per}"
        )

    fr = sorted(
        {
            x[3]
            for s in strats
            for r in rots
            for x in rounds.get((block, s, r), [])
            if x[3] is not None
        }
    )
    if fr:
        print(
            f"\n  whole% by skew (airtimes between successive senders), block {block}"
        )
        print(f"  {'strategy':10} " + " ".join(f"{f:>10}" for f in fr))
        for s in strats:
            cells = []
            for f in fr:
                rr = [
                    x for r in rots for x in rounds.get((block, s, r), []) if x[3] == f
                ]
                cells.append(
                    f"{sum(x[0] for x in rr):3}/{len(rr):<3}{100 * sum(x[0] for x in rr) / len(rr):3.0f}%"
                    if rr
                    else "-"
                )
            print(f"  {s:10} " + " ".join(f"{c:>10}" for c in cells))

    nodes = sorted(
        {
            n
            for s in strats
            for r in rots
            for x in rounds.get((block, s, r), [])
            for n in x[4]
        }
    )
    print(
        f"\n  per node as sender: share of its frames that reached a listener, block {block}"
    )
    print(f"  {'strategy':10} " + " ".join(f"{n:>9}" for n in nodes))
    for s in strats:
        cells = []
        for nd in nodes:
            v = [
                x[4][nd]
                for r in rots
                for x in rounds.get((block, s, r), [])
                if nd in x[4]
            ]
            cells.append(f"{100 * sum(v) / len(v):8.0f}%" if v else f"{'-':>9}")
        print(f"  {s:10} " + " ".join(cells))

anc = sorted((t_of.get(f"A_base_{r}", 0), r) for b, _, r in rounds if b == "A")
if anc:
    print("\nAnchors (dut+peer, branch default, 249 B), in time order: whole / x-heard")
    import time

    for t, r in anc:
        rr = rounds[("A", "base", r)]
        w, xh, xn = sum(x[0] for x in rr), sum(x[1] for x in rr), sum(x[2] for x in rr)
        print(
            f"  {time.strftime('%H:%M', time.localtime(t))} {r:5} {w:3}/{len(rr):<3} {xh:3}/{xn:<3}"
        )
