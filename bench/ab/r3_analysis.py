"""Round 3 analysis: K (TCXO start delay), C1d / C1diq (true develop arm), C2r (ShortFast contention + event ring)."""
import collections
import json
import statistics
import sys

S = sys.argv[1]
R3 = [json.loads(l) for l in open(f"{S}/camp-r3/records.jsonl", encoding="utf-8")]
try:
    R3B = [json.loads(l) for l in open(f"{S}/camp-r3b/records.jsonl", encoding="utf-8")]
except FileNotFoundError:
    R3B = []
PRE = ["SHORT_FAST", "MEDIUM_FAST", "LONG_FAST"]
ab = {"SHORT_FAST": "SF", "MEDIUM_FAST": "MF", "LONG_FAST": "LF"}
BIG = 4_000_000_000  # stamps that wrapped (trigger stamp ahead of micros)


def med(v, nd=0):
    return round(statistics.median(v), nd) if v else None


def q(v, p):
    v = sorted(v)
    return v[int(p * (len(v) - 1))]


print("records:", len(R3), "+", len(R3B), "| health", sum(r["kind"] == "health" and r["ok"] for r in R3 + R3B), "/",
      sum(r["kind"] == "health" for r in R3 + R3B))

# ---- K
print("\n## K: TCXO start delay (TC1+TC2 pooled)")
print("preset base tcxo | n | key-up ms | TX->RX gap us | heard | rx_after | SNR med per listener")
for p in ("SHORT_FAST", "LONG_FAST"):
    for base in ("reset", "develop"):
        for v in (5000, 2000, 1000, 500, 200):
            x = [r for r in R3 if r["kind"] == "K" and r["preset"] == p and r["base"] == base and r["tcxo"] == v]
            if not x:
                continue
            ku = [(r["txstart"] - r["decide"]) / 1000 for r in x if r["txstart"] and r["decide"] and r["decide"] < BIG and r["txstart"] - r["decide"] < 50000]
            gap = [r["gap_total"] for r in x if r["gap_total"]]
            snr = collections.defaultdict(list)
            for r in x:
                for n, s in r["heard"].items():
                    if s is not None:
                        snr[n].append(s)
            print(ab[p], base, v, "|", len(x), "|", med(ku, 2), "|", med(gap), "|",
                  f"{sum(sum(s is not None for s in r['heard'].values()) for r in x)}/{3 * len(x)}", "|",
                  f"{sum(r['rx_after'] for r in x)}/{len(x)}", "|", {n: med(s, 1) for n, s in sorted(snr.items())})

# ---- C1d / C1diq
ARMS = ["dev", "devcad", "devtrue", "cadhold", "cadpeek", "lnpeek", "off"]
for kind in ("C1d", "C1diq"):
    for dutset, label in ((("TC1", "TC2"), "DUT TC1/TC2"), (("XT",), "DUT XT")):
        print(f"\n## {kind}, {label}: stomped/valid by preset | median after-end ms | no-TX | foreign ok | obs heard")
        for a in ARMS:
            row, aft, notx, fok, oh, tot = [], [], 0, 0, 0, 0
            for p in PRE:
                x = [r for r in R3 if r["kind"] == kind and r["arm"] == a and r["preset"] == p and r["dut"] in dutset and r["armed"] and r["emitted"]]
                v = [r for r in x if r["dut_tx_ms"] is not None]
                notx += len(x) - len(v)
                row.append(f"{sum(r['overlap'] for r in v)}/{len(v)}" if x else "-")
                aft.append(med([r["after_end_ms"] for r in v if not r["overlap"]]))
                fok += sum(bool(r["foreign_ok"]) for r in v)
                oh += sum(bool(r["obs_heard"]) for r in v)
                tot += len(v)
            print(f"{a:8s}", " ".join(f"{c:>6s}" for c in row), "| after", aft, f"| noTX {notx} | fok {fok}/{tot} | obs {oh}/{tot}")
print("\nunusable C1d/C1diq rounds (not armed or not emitted):",
      sum(1 for r in R3 if r["kind"] in ("C1d", "C1diq") and not (r["armed"] and r["emitted"])), "of",
      sum(1 for r in R3 if r["kind"] in ("C1d", "C1diq")))

# how develop defers: first-verdict kinds in the DUT's ring
print("\n## C1d decision path per arm (TC1/TC2 DUTs): counts of b (busyRx) and c (CAD busy) verdicts before the TX")
for a in ARMS:
    b = c = n = 0
    for r in R3:
        if r["kind"] == "C1d" and r["arm"] == a and r["dut"] in ("TC1", "TC2") and r.get("ev"):
            toks = r["ev"].split()[2:]
            b += sum(t[0] == "b" for t in toks)
            c += sum(t[0] == "c" for t in toks)
            n += 1
    print(f"{a:8s} rounds {n} busyRx {b} CADbusy {c}")


# ---- C2r
def c2r(recs, title, senders_ok=None):
    print(f"\n## {title}")
    arms = ["default", "cad4", "devtrue", "rx_nb", "default_slot25", "cad4_slot25", "default_cw5", "default_tcxo", "cad4_tcxo"]
    for a in arms:
        x = [r for r in recs if r["kind"] == "C2r" and r["arm"] == a and r["triggered"]]
        if not x:
            continue
        sk = {s: f"{sum(r['whole'] for r in x if r['skew_airtimes'] == s)}/{sum(1 for r in x if r['skew_airtimes'] == s)}" for s in (0, 0.25, 0.5)}
        gaps = []
        for r in x:
            t = sorted(v for v in r["txstart_us"].values() if v and v < BIG)
            if len(t) == 3 and t[1] - t[0] > 50000:
                gaps.append((t[2] - t[1]) / 1000)
        print(f"{a:15s} whole {sum(r['whole'] for r in x)}/{len(x)}  x-heard {sum(r['x_heard'] for r in x)}/{6 * len(x)}  by skew {sk}  "
              f"deferrers' 2nd->3rd start: med {med(gaps, 1)} ms, <10 ms {sum(g < 10 for g in gaps)}/{len(gaps)}")
    print("untriggered:", sum(1 for r in recs if r["kind"] == "C2r" and not r["triggered"]), "of", sum(1 for r in recs if r["kind"] == "C2r"))


c2r([r for r in R3 if "XT" not in r.get("senders", [])], "C2r round 3, triple TC1+TC2+LR only (XT not a sender)")
if R3B:
    c2r(R3B, "C2r supplement, triple TC1+TC2+LR, XT listener/marker, XT rebooted, no osc changes on XT")

# ---- event ring: wake lateness after RX
print("\n## event ring: timer wakes (all C2r rounds, both runs)")
postR, other, rw = [], [], []
for r in R3 + R3B:
    if r["kind"] != "C2r":
        continue
    for s, ev in r["ev"].items():
        if not ev:
            continue
        lastR = prevD = None
        for t in ev.split()[2:]:
            k = t[0]
            if k == "R":
                lastR = float(t[1:])
            elif k == "D":
                tm, a = t[1:].split(":")
                prevD = (float(tm), int(a), lastR is not None and float(tm) - lastR < 5, lastR)
            elif k == "W" and prevD:
                tm = float(t[1:])
                if tm > 4_000_000:
                    break
                (postR if prevD[2] else other).append(tm - prevD[0] - prevD[1])
                if prevD[2]:
                    rw.append((tm - prevD[3], prevD[1]))
                prevD = None
print(f"draw right after an RX: n {len(postR)}, wake later than drawn by median {med(postR, 1)} ms, p90 {round(q(postR, .9), 1)}")
print(f"other draws: n {len(other)}, late by median {med(other, 1)} ms, p90 {round(q(other, .9), 1)}")
small = [w for w, d in rw if d < 100]
big = [(w - d) for w, d in rw if d >= 115]
print(f"draws < 100 ms after an RX: wake at RX + min {min(small):.1f} / med {med(small, 1)} / max {max(small):.1f} ms (n {len(small)})")
if big:
    print(f"draws >= 115 ms after an RX: wake - draw med {med(big, 1)} ms (n {len(big)})")
