"""Late-joiner results from abrun.py late_sweep records (L-*.jsonl).

  python analyse_late.py FILE.jsonl [FILE.jsonl ...]

Per round and joiner:
  join     where in the talker's frame the joiner decided: (decision time - talker frame start) / airtime.
           Decision = the joiner's "BENCH undeaf" line after its deaf/queue (software modes), else its send.
           Talker frame start = earliest arrival of the talker's frame at an uninvolved listener, minus airtime.
  verdict  the joiner's own first channel verdict after deciding: busy (CAD busy / busyRx / RSSI busy) or free
  outcome  talker frame reached an uninvolved listener? joiner frame did? collision = either lost
Bins by join fraction; "correct" = busy verdict while the talker was on air (0 <= join < 1).
"""
import json
import re
import sys
from collections import defaultdict

PAT = re.compile(r"^C-([A-Z]{2})-(\w+?)-(\w+?)-(\d+)(?:-x*)?$")
BUSY = re.compile(r"CAD busy|Can not send yet, busyRx|RSSI look .*: busy")
FREE = re.compile(r"CAD free|CAD skipped|RSSI look .*: free")
BINS = [(-9, 0, "before"), (0, .25, "0-25%"), (.25, .5, "25-50%"), (.5, .75, "50-75%"), (.75, 1.0, "75-100%"), (1.0, 9, "after")]

rounds, tx, rx, logs = {}, {}, defaultdict(dict), defaultdict(list)
for f in sys.argv[1:]:
    for line in open(f, encoding="utf-8"):
        d = json.loads(line)
        k = d["kind"]
        if k == "round":
            rounds[(d["label"], d["i"])] = d
        elif k == "log":
            logs[d["node"]].append((d["ts"], d["line"]))
        else:
            m = PAT.match(d.get("text") or "")
            if not m:
                continue
            lab, node, i = m.group(2), m.group(3), int(m.group(4))
            if k == "tx":
                tx[(lab, i, node)] = d["ts"]
            elif k == "rx" and d["node"] != node:
                rx[(lab, i, node)].setdefault(d["node"], d["ts"])
for n in logs:
    logs[n].sort()


def first_after(node, t0, t1, pat):
    for ts, line in logs[node]:
        if ts < t0:
            continue
        if ts > t1:
            return None
        if pat.search(line):
            return ts, line
    return None


table = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
for (lab, i), r in sorted(rounds.items()):
    talker, joiners, air = r["talker"], r["joiners"], r["air_s"]
    involved = {talker, *joiners}
    t_arr = [ts for n, ts in rx[(lab, i, talker)].items() if n not in involved]
    t_ok = bool(t_arr)
    for j in joiners:
        jt = tx.get((lab, i, j))
        if jt is None:
            continue
        undeaf = first_after(j, jt - 1.0, jt + 5.0, re.compile(r"BENCH undeaf"))
        decision = undeaf[0] if undeaf and undeaf[0] >= jt - 0.5 else jt
        busy = first_after(j, decision - 0.01, decision + 1.0, BUSY)
        free = first_after(j, decision - 0.01, decision + 1.0, FREE)
        verdict = "busy" if busy and (not free or busy[0] <= free[0]) else ("free" if free else "?")
        j_ok = any(n not in involved for n in rx[(lab, i, j)])
        if t_arr:
            join = (decision - (min(t_arr) - air)) / air
            b = next(name for lo, hi, name in BINS if lo <= join < hi)
        else:
            b = "talker lost"
        c = table[lab][b]
        c["n"] += 1
        c["busy"] += verdict == "busy"
        c["free"] += verdict == "free"
        c["both_ok"] += t_ok and j_ok
        c["collide"] += not (t_ok and j_ok)

names = [b[2] for b in BINS] + ["talker lost"]
print("Per variant and join point: n | joiner said busy | rounds with both frames delivered")
print(f"{'variant':16} " + " ".join(f"{n:>17}" for n in names))
for lab in table:
    cells = []
    for n in names:
        c = table[lab].get(n)
        cells.append(f"{c['n']:3} b{c['busy']:3} ok{c['both_ok']:3}" if c else f"{'-':>17}")
    print(f"{lab:16} " + " ".join(f"{x:>17}" for x in cells))

print("\nWhile the talker was on air (join 0-100%): joiner said busy / both frames delivered")
for lab in table:
    n = sum(table[lab][b]["n"] for b in ("0-25%", "25-50%", "50-75%", "75-100%") if b in table[lab])
    busy = sum(table[lab][b]["busy"] for b in ("0-25%", "25-50%", "50-75%", "75-100%") if b in table[lab])
    ok = sum(table[lab][b]["both_ok"] for b in ("0-25%", "25-50%", "50-75%", "75-100%") if b in table[lab])
    if n:
        print(f"  {lab:16} busy {busy:3}/{n:<3} ({busy / n:4.0%})   delivered {ok:3}/{n:<3} ({ok / n:4.0%})")
