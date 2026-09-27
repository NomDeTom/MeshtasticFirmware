import json, collections, statistics, sys
S = sys.argv[1]
P1 = [json.loads(l) for l in open(f"{S}/camp-night/records.jsonl", encoding="utf-8")]
P2 = [json.loads(l) for l in open(f"{S}/camp-night2/records.jsonl", encoding="utf-8")]
ALL = P1 + P2
PRE = ["SHORT_FAST", "SHORT_SLOW", "MEDIUM_FAST", "MEDIUM_SLOW", "LONG_FAST"]
ab = {"SHORT_FAST": "SF", "SHORT_SLOW": "SS", "MEDIUM_FAST": "MF", "MEDIUM_SLOW": "MS", "LONG_FAST": "LF"}
def med(v): return f"{statistics.median(v):.0f}" if v else "—"
def c1table(kind, recs):
    arms = ["dev", "hold", "peek", "ln", "lnhold", "lnpeek", "off"]
    print(f"\n## {kind}: stomped/valid | median after-end ms (non-stomp) | foreign_ok | obs_heard")
    print("arm | " + " | ".join(ab[p] for p in PRE) + " | total | after-end by preset | foreign ok | obs heard")
    for a in arms:
        row = []; tot = [0, 0]; aft = []; fok = [0, 0]; oh = [0, 0]
        for p in PRE:
            rs = [r for r in recs if r["kind"] == kind and r["arm"] == a and r["preset"] == p and r.get("armed") and r.get("emitted") and r.get("dut_tx_ms") is not None]
            st = sum(r["overlap"] for r in rs); row.append(f"{st}/{len(rs)}" if rs else "—"); tot[0] += st; tot[1] += len(rs)
            aft.append(med([r["after_end_ms"] for r in rs if not r["overlap"]]))
            fok[0] += sum(bool(r["foreign_ok"]) for r in rs); fok[1] += len(rs); oh[0] += sum(bool(r["obs_heard"]) for r in rs); oh[1] += len(rs)
        print(f"{a} | " + " | ".join(row) + f" | {tot[0]}/{tot[1]} | " + "/".join(aft) + f" | {fok[0]}/{fok[1]} | {oh[0]}/{oh[1]}")
    rs = [r for r in recs if r["kind"] == kind and r.get("armed") and r.get("emitted") and r.get("dut_tx_ms") is not None]
    for ov in (True, False):
        x = [r for r in rs if r["overlap"] == ov]
        if x: print(f"overlap={ov}: n={len(x)} foreign_ok {sum(bool(r['foreign_ok']) for r in x)/len(x):.0%} obs_heard {sum(bool(r['obs_heard']) for r in x)/len(x):.0%}")
    lost = [r for r in rs if not r["armed"] or not r["emitted"]]
c1table("C1", [r for r in ALL])
c1table("C1iq", P2)
print("\n## C1 by pass (stomp totals)")
for ps in (1, 2, 3):
    print(ps, {a: f"{sum(r['overlap'] for r in ALL if r['kind']=='C1' and r.get('pass_')==ps and r['arm']==a and r.get('armed') and r.get('emitted') and r.get('dut_tx_ms') is not None)}/{sum(1 for r in ALL if r['kind']=='C1' and r.get('pass_')==ps and r['arm']==a and r.get('armed') and r.get('emitted') and r.get('dut_tx_ms') is not None)}" for a in ["dev","hold","peek","ln","lnhold","lnpeek","off"]})
print("\n## C1 unusable rounds:", sum(1 for r in ALL if r["kind"] in ("C1","C1iq") and not (r.get("armed") and r.get("emitted") and r.get("dut_tx_ms") is not None)), "of", sum(1 for r in ALL if r["kind"] in ("C1","C1iq")))
# C2s
arms = ["default", "cadrx4", "lnpeek", "default_nb", "cad4", "rx", "rx_nb", "off"]
print("\n## C2s whole (triggered) by preset | x_heard sum/possible")
print("arm | " + " | ".join(ab[p] for p in PRE) + " | whole total | x-heard total")
for a in arms:
    row = []; w = [0, 0]; xh = [0, 0]
    for p in PRE:
        rs = [r for r in P2 if r["kind"] == "C2s" and r["arm"] == a and r["preset"] == p and r.get("triggered")]
        row.append(f"{sum(r['whole'] for r in rs)}/{len(rs)}" if rs else "—"); w[0] += sum(r['whole'] for r in rs); w[1] += len(rs)
        xh[0] += sum(r["x_heard"] for r in rs); xh[1] += 6 * len(rs)
    print(f"{a} | " + " | ".join(row) + f" | {w[0]}/{w[1]} | {xh[0]}/{xh[1]}")
print("C2s untriggered:", sum(1 for r in P2 if r["kind"] == "C2s" and not r.get("triggered")), "of", sum(1 for r in P2 if r["kind"] == "C2s"))
print("\n## C2s by skew (default vs cad4 vs rx), all presets")
for a in ["default", "cadrx4", "cad4", "rx", "off"]:
    print(a, {sk: f"{sum(r['whole'] for r in P2 if r['kind']=='C2s' and r['arm']==a and r.get('triggered') and r['skew_airtimes']==sk)}/{sum(1 for r in P2 if r['kind']=='C2s' and r['arm']==a and r.get('triggered') and r['skew_airtimes']==sk)}" for sk in sorted({r['skew_airtimes'] for r in P2 if r['kind']=='C2s'})})
print("\n## C3s")
for p in PRE:
    for a in ["cad4", "cadrx4"]:
        for sy in sorted({r["sync"] for r in P2 if r["kind"] == "C3s"}):
            rs = [r for r in P2 if r["kind"] == "C3s" and r["preset"] == p and r["arm"] == a and r["sync"] == sy and r.get("armed")]
            if rs: print(p, a, sy, f"whole {sum(r['whole'] for r in rs)}/{len(rs)} xh {sum(r['x_heard'] for r in rs)}/{2*len(rs)}")
print("\n## C4")
for r in ALL:
    if r["kind"] == "C4": print(r.get("pass_"), r["preset"], r["minutes"], {n: (v["series"], v["all_free"], v["any_busy"]) for n, v in r["per_node"].items()}, r.get("ends"))
print("\n## C5")
c5 = collections.defaultdict(list)
for r in ALL:
    if r["kind"] == "C5": c5[(r["preset"], r["config"], r["scen"])].append(r)
for k, v in sorted(c5.items()):
    print(k, [(x["dut"], x.get("ready") if x.get("adopted") else x.get("total"), x["rx_after"]) for x in v])
print("\n## health", sum(1 for r in ALL if r["kind"] == "health" and r["ok"]), "/", sum(1 for r in ALL if r["kind"] == "health"), [r["silent"] for r in ALL if r["kind"] == "health" and r["silent"]])
