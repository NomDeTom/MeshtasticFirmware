"""Contention results from abrun.py records (contend rows and knob sweeps).

  python analyse.py FILE.jsonl [FILE.jsonl ...]

Frames are "C-<key>-<label>-<sender>-<i>[-xxx padding]"; "J-..." frames are interferers and are
not counted. Groups by (preset key, label). Per group, over message indices i sent by every
counted sender:
  whole     every counted sender's frame i reached at least one non-sender listener
  x-heard   sender A decoded sender B's frame i (only possible if A was listening, not sending),
            averaged over ordered sender pairs
  gap       spread of host arrival times of round i's frames at the best listener, in airtimes
            of that group's frame size
Fisher exact (two-sided) on 'whole' and 'x-heard': lbt vs dev, and each variant vs "base".
"""
import json
import math
import re
import statistics as st
import sys
from collections import defaultdict
from itertools import permutations
from math import comb

PAT = re.compile(r"^([CJ])-([A-Z]{2})-(\w+?)-(\w+?)-(\d+)(?:-x*)?$")


def airtime_ms(key, onair_b):
    sf, bw, cr = {"SF": (7, 250, 5), "NF": (7, 62.5, 6), "NS": (8, 62.5, 6), "SS": (8, 250, 5),
                  "MF": (9, 250, 5), "MS": (10, 250, 5), "LM": (11, 125, 8)}[key]
    ts = 2 ** sf / (bw * 1e3)
    de = 1 if ts > 0.016 else 0
    n = 8 + max(math.ceil((8 * onair_b - 4 * sf + 44) / (4 * (sf - 2 * de))) * cr, 0)
    return (16 + 4.25 + n) * ts * 1000


def fisher(a, b, c, d):
    n1, n2, k = a + b, c + d, a + c
    n = n1 + n2
    p = lambda x: comb(n1, x) * comb(n2, k - x) / comb(n, k)  # noqa: E731
    p0 = p(a)
    return min(1.0, sum(p(x) for x in range(max(0, k - n2), min(k, n1) + 1) if p(x) <= p0 * (1 + 1e-9)))


sent = defaultdict(set)   # (key,label,sender) -> {i}
seen = defaultdict(set)   # (key,label,node) -> {(sender,i)}
arr = defaultdict(dict)   # (key,label,node) -> {(sender,i): ts}
size = {}                 # (key,label) -> on-air bytes
nodes_all = set()
for path in [a for a in sys.argv[1:] if not a.startswith("--")]:
    for line in open(path, encoding="utf-8"):
        d = json.loads(line)
        m = PAT.match(d.get("text") or "")
        if not m or m.group(1) != "C":
            continue
        key, label, s, i = m.group(2), m.group(3), m.group(4), int(m.group(5))
        size[(key, label)] = 22 + len(d["text"])
        if d["kind"] == "tx":
            sent[(key, label, s)].add(i)
        elif d["kind"] == "rx" and d["node"] != s:
            nodes_all.add(d["node"])
            seen[(key, label, d["node"])].add((s, i))
            arr[(key, label, d["node"])].setdefault((s, i), d["ts"])

groups = sorted({(k, l) for (k, l, _) in sent})
res = {}
print(f"{'key':3} {'label':12} {'snd':>3} {'B':>4} {'n':>3}  {'whole':>13}  {'x-heard':>13}  gap(airtimes) p10/med/p90  listeners")
for key, label in groups:
    senders = sorted(s for (k, l, s) in sent if (k, l) == (key, label))
    listeners = sorted(nodes_all - set(senders))
    idx = sorted(set.intersection(*(sent[(key, label, s)] for s in senders)))
    n = len(idx)
    anyl = set().union(*(seen[(key, label, w)] for w in listeners)) if listeners else set()
    whole = sum(1 for i in idx if all((s, i) in anyl for s in senders))
    pairs = list(permutations(senders, 2))
    xh = sum(1 for i in idx for a, b in pairs if (b, i) in seen[(key, label, a)])
    xn = n * len(pairs)
    A = airtime_ms(key, size[(key, label)])
    gaps = []
    for i in idx:
        for w in listeners:
            ts = [arr[(key, label, w)].get((s, i)) for s in senders]
            if all(ts):
                gaps.append((max(ts) - min(ts)) * 1000 / A)
                break
    gaps.sort()
    g = f"{gaps[len(gaps) // 10]:.1f}/{st.median(gaps):.1f}/{gaps[9 * len(gaps) // 10]:.1f}" if gaps else "-"
    per = " ".join(f"{w}:{sum(1 for i in idx if all((s, i) in seen[(key, label, w)] for s in senders))}" for w in listeners)
    pc = lambda x, t: f"{x:4}/{t:<4}({x / t:4.0%})" if t else "-"  # noqa: E731
    print(f"{key:3} {label:12} {len(senders):3} {size[(key, label)]:4} {n:3}  {pc(whole, n)}  {pc(xh, xn)}  {g:>20}  {per}")
    res[(key, label)] = {"n": n, "whole": whole, "xh": xh, "xn": xn}

print("\nComparisons (two-sided Fisher exact):")
for key, label in groups:
    ref = "dev" if label == "lbt" else ("base" if label != "base" and (key, "base") in res else None)
    if not ref or (key, ref) not in res:
        continue
    a, b = res[(key, ref)], res[(key, label)]
    pw = fisher(a["whole"], a["n"] - a["whole"], b["whole"], b["n"] - b["whole"])
    px = fisher(a["xh"], a["xn"] - a["xh"], b["xh"], b["xn"] - b["xh"])
    print(f"  {key} {label:12} vs {ref:5}: whole {a['whole']}/{a['n']} -> {b['whole']}/{b['n']} p={pw:.3f}"
          f" | x-heard {a['xh']}/{a['xn']} -> {b['xh']}/{b['xn']} p={px:.3f}")

if "--links" in sys.argv:
    print("\nPer-link delivery (frames decoded / frames sent), by group:")
    links = sorted({(s, w) for (k, l, s) in sent for w in nodes_all if w != s})
    print(f"{'key':3} {'label':12} " + " ".join(f"{s[:4]}>{w[:4]:<4}" for s, w in links))
    for key, label in groups:
        row = []
        for s, w in links:
            tx = sent.get((key, label, s), set())
            got = sum(1 for (ss, i) in seen[(key, label, w)] if ss == s and i in tx)
            row.append(f"{got / len(tx):9.0%}" if tx else f"{'-':>9}")
        print(f"{key:3} {label:12} " + " ".join(row))
