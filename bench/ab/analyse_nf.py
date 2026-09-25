"""Noise floor from the knobs4 sampler: 'BENCH nf: n= skip= p50= p90= min= max= avg=' lines, one per node per second.

  python analyse_nf.py FILE.jsonl [FILE.jsonl ...] [--window 300]

Per node and time window: median of the per-second p50 (the floor), median p90, the worst max seen, and the
share of samples skipped because the radio was not idling in RX (receiving or sending), a rough occupancy.
Also per sweep variant, from the 'variant' records, so a noisy variant can be told from a noisy hour.
"""
import json
import re
import statistics as st
import sys
import time
from collections import defaultdict

NF = re.compile(r"BENCH nf: n=(\d+) skip=(\d+)(?: p50=(-?\d+) p90=(-?\d+) min=(-?\d+) max=(-?\d+))?")
args = [a for a in sys.argv[1:] if not a.startswith("--")]
window = int(sys.argv[sys.argv.index("--window") + 1]) if "--window" in sys.argv else 300
if "--window" in sys.argv:
    args = [a for a in args if a != str(window)]

rows, variants = [], []
for f in args:
    for line in open(f, encoding="utf-8"):
        d = json.loads(line)
        if d["kind"] == "variant":
            variants.append((d["ts"], d["label"]))
        elif d["kind"] == "log":
            m = NF.search(d["line"])
            if m:
                n, skip = int(m.group(1)), int(m.group(2))
                vals = tuple(int(x) for x in m.groups()[2:]) if m.group(3) else None
                rows.append((d["ts"], d["node"], n, skip, vals))
variants.sort()


def label_at(ts):
    lab = None
    for t, l in variants:
        if t <= ts:
            lab = l
    return lab


def summarise(keyfn, title):
    agg = defaultdict(lambda: {"p50": [], "p90": [], "max": -200, "n": 0, "skip": 0})
    for ts, node, n, skip, vals in rows:
        k = keyfn(ts, node)
        if k is None:
            continue
        a = agg[k]
        a["n"] += n
        a["skip"] += skip
        if vals:
            a["p50"].append(vals[0]); a["p90"].append(vals[1]); a["max"] = max(a["max"], vals[3])
    print(f"\n{title}")
    print(f"{'':22} {'floor p50':>9} {'p90':>6} {'max':>6} {'busy%':>6} {'secs':>5}")
    for k in sorted(agg):
        a = agg[k]
        tot = a["n"] + a["skip"]
        fl = f"{st.median(a['p50']):9.0f}" if a["p50"] else f"{'-':>9}"
        p9 = f"{st.median(a['p90']):6.0f}" if a["p90"] else f"{'-':>6}"
        print(f"{' '.join(map(str, k)):22} {fl} {p9} {a['max']:6} {100 * a['skip'] / tot if tot else 0:6.1f} {len(a['p50']):5}")


if not rows:
    sys.exit("no 'BENCH nf:' lines found (is nf= set, and is this a knobs4 image?)")
summarise(lambda ts, node: (node,), "Per node, whole capture")
summarise(lambda ts, node: (time.strftime("%H:%M", time.localtime(ts // window * window)), node),
          f"Per {window // 60}-minute window")
if variants:
    summarise(lambda ts, node: (label_at(ts), node) if label_at(ts) else None, "Per sweep variant")

# -- passive probe lines (knobs8): 'BENCH probe: n= skip= cad_busy= rx_busy= rssi_busy= rssi_p50= floor='
PROBE = re.compile(r"BENCH probe: n=(\d+) skip=(\d+) cad_busy=(\d+) rx_busy=(\d+) rssi_busy=(\d+) rssi_p50=(-?\d+)")
probe = defaultdict(lambda: [0, 0, 0, 0, []])
for f in args:
    for line in open(f, encoding="utf-8"):
        d = json.loads(line)
        if d["kind"] == "log":
            m = PROBE.search(d["line"])
            if m and label_at(d["ts"]):
                a = probe[(label_at(d["ts"]), d["node"])]
                n = int(m.group(1))
                a[0] += n; a[1] += int(m.group(3)); a[2] += int(m.group(4)); a[3] += int(m.group(5))
                if n:
                    a[4].append(int(m.group(6)))
if probe:
    print("\nPassive probe, per phase and node: share of readings each method called busy")
    print(f"{'':22} {'n':>5} {'CAD':>6} {'RXflag':>7} {'RSSI>fl':>8} {'RSSI p50':>9}")
    for k in sorted(probe):
        n, c, r, e, s = probe[k]
        if n:
            print(f"{' '.join(k):22} {n:5} {100 * c / n:5.0f}% {100 * r / n:6.0f}% {100 * e / n:7.0f}% {st.median(s):9.0f}")
