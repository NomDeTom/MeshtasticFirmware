"""A/B verdict from a bench run's packets.jsonl (contention table).

usage: python ab_analyse.py ~/bench-runs/<run-id> [--sent events-derived]
Payload text is "<row>-<sender>-<i>", e.g. "C-SF-dev-dut-17".
"""

import json
import re
import sys
from collections import defaultdict
from math import comb
from pathlib import Path

run = Path(sys.argv[1]).expanduser()
PAT = re.compile(r"^(C-(\w+)-(dev|lbt))-(dut|peer)-(\d+)$")

# seen[row][observer] = set of (sender, i)
seen = defaultdict(lambda: defaultdict(set))
for line in open(run / "packets.jsonl", encoding="utf-8"):
    d = json.loads(line)
    if d.get("portnum") != "TEXT_MESSAGE_APP" or not d.get("payload_hex"):
        continue
    try:
        text = bytes.fromhex(d["payload_hex"]).decode("utf-8", "replace")
    except ValueError:
        continue
    m = PAT.match(text)
    if m and d.get("observer") != m.group(4):
        seen[m.group(1)][d["observer"]].add((m.group(4), int(m.group(5))))

# how many pairs the stimulus actually emitted, per row, from the bench's own events
sent = {}
for line in open(run / "events.jsonl", encoding="utf-8"):
    e = json.loads(line)
    if (
        e.get("event") == "stimulus_rf_peer_done"
        or e.get("kind") == "stimulus_rf_peer_done"
    ):
        sent[e.get("scenario")] = e.get("sent", {})


def fisher_two_sided(a, b, c, d):
    """2x2 [[a,b],[c,d]] two-sided Fisher exact p."""
    n1, n2, k = a + b, c + d, a + c
    n = n1 + n2

    def p(x):
        return comb(n1, x) * comb(n2, k - x) / comb(n, k)

    p0 = p(a)
    lo, hi = max(0, k - n2), min(k, n1)
    return min(1.0, sum(p(x) for x in range(lo, hi + 1) if p(x) <= p0 * (1 + 1e-9)))


rows = {}
for row, obs in sorted(seen.items()):
    n = min(sent.get(row, {}).values() or [0]) or 1 + max(
        i for o in obs.values() for _, i in o
    )
    stats = {"n": n}
    for w in ("witness", "lr"):
        got = obs.get(w, set())
        both = sum(1 for i in range(n) if ("dut", i) in got and ("peer", i) in got)
        one = sum(1 for i in range(n) if (("dut", i) in got) != (("peer", i) in got))
        stats[w] = {
            "both": both,
            "one": one,
            "none": n - both - one,
            "dut": sum(1 for s, _ in got if s == "dut"),
            "peer": sum(1 for s, _ in got if s == "peer"),
        }
    anyw = obs.get("witness", set()) | obs.get("lr", set())
    stats["any_both"] = sum(
        1 for i in range(n) if ("dut", i) in anyw and ("peer", i) in anyw
    )
    # a sender that decoded the other's frame was listening, not transmitting, when it flew
    stats["dut_heard_peer"] = sum(1 for s, _ in obs.get("dut", set()) if s == "peer")
    stats["peer_heard_dut"] = sum(1 for s, _ in obs.get("peer", set()) if s == "dut")
    rows[row] = stats

for row, s in rows.items():
    n = s["n"]
    print(
        f"{row:10} n={n:3}  any-witness both={s['any_both']:3} ({s['any_both']/n:5.1%})  "
        f"cross-heard dut<-peer {s['dut_heard_peer']:3} peer<-dut {s['peer_heard_dut']:3}"
    )
    for w in ("witness", "lr"):
        x = s[w]
        print(
            f"{'':12}{w:8} both {x['both']:3}  one {x['one']:3}  none {x['none']:3}   dut {x['dut']:3} peer {x['peer']:3}"
        )

print("\nA/B (develop vs listening-now), Fisher exact two-sided:")
for short in sorted({r.split("-")[1] for r in rows}):
    a, b = rows.get(f"C-{short}-dev"), rows.get(f"C-{short}-lbt")
    if not (a and b):
        continue
    for label, key, denom in (
        ("pairs both delivered", "any_both", 1),
        ("dut decoded peer", "dut_heard_peer", 1),
        ("peer decoded dut", "peer_heard_dut", 1),
    ):
        x1, n1, x2, n2 = a[key], a["n"], b[key], b["n"]
        p = fisher_two_sided(x1, n1 - x1, x2, n2 - x2)
        print(
            f"  {short} {label:22} dev {x1}/{n1} ({x1/n1:5.1%})  lbt {x2}/{n2} ({x2/n2:5.1%})  p={p:.4f}"
        )
