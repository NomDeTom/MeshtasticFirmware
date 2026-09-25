"""Sidecar: mirror a knob sweep (run_sweep.sh + S-*.jsonl) into ~/bench-runs/ab-sweep-dash for `bench serve`.

  python dash_sweep.py [variants.json] [runlog]
"""
import glob
import json
import os
import re
import sys
import time
from itertools import permutations
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from abrun import NODES  # noqa: E402

VARIANTS = json.loads((HERE / (sys.argv[1] if len(sys.argv) > 1 else "sweep2.json")).read_text())
LABELS = [v["label"] if isinstance(v, dict) else v[0] for v in VARIANTS]
SPEC = {(v["label"] if isinstance(v, dict) else v[0]): v for v in VARIANTS}
LOG = HERE / (sys.argv[2] if len(sys.argv) > 2 else "run_sweep.log")
REC = Path.home() / "bench-runs" / "ab-lean"
OUT = Path.home() / "bench-runs" / "ab-sweep-dash"
OUT.mkdir(parents=True, exist_ok=True)
PAT = re.compile(r"^([CJ])-([A-Z]{2})-(\w+?)-(\w+?)-(\d+)(?:-x*)?$")
STAGE = {"flash": "2-flash", "provision": "3-provision", "contend": "4-execute"}
started = LOG.stat().st_ctime if LOG.exists() else time.time()


def write(name, obj):
    tmp = OUT / (name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
    os.replace(tmp, OUT / name)


def measure(files):
    sent, seen, t = {}, {}, {}
    for f in files:
        for line in open(f, encoding="utf-8"):
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            m = PAT.match(d.get("text") or "")
            if not m or m.group(1) != "C":
                continue
            lab, s, i = m.group(3), m.group(4), int(m.group(5))
            t.setdefault(lab, [d["ts"], d["ts"]])[1] = d["ts"]
            if d["kind"] == "tx":
                sent.setdefault(lab, {}).setdefault(s, set()).add(i)
            elif d["kind"] == "rx" and d["node"] != s:
                seen.setdefault(lab, {}).setdefault(d["node"], set()).add((s, i))
    out = {}
    for lab, by in sent.items():
        senders = sorted(by)
        idx = sorted(set.intersection(*by.values()))
        heard = seen.get(lab, {})
        listeners = [n for n in heard if n not in senders]
        anyl = set().union(*(heard[w] for w in listeners)) if listeners else set()
        whole = sum(1 for i in idx if all((s, i) in anyl for s in senders))
        pairs = list(permutations(senders, 2))
        xh = sum(1 for i in idx for a, b in pairs if (b, i) in heard.get(a, set()))
        out[lab] = {"n": len(idx), "whole": whole, "xh": xh, "xn": len(idx) * len(pairs), "senders": senders,
                    "t0": t[lab][0], "t1": t[lab][1]}
    return out


def pct(a, b):
    return f"{a}/{b} ({a / b:.0%})" if b else "-"


mirrored = 0
last, since = None, time.time()
while True:
    lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines() if LOG.exists() else []
    markers = [l for l in lines if l.startswith("=== ")]
    done_all = any("ALL DONE" in l for l in markers)
    failed = any("Traceback" in l or l.startswith("RuntimeError") for l in lines)
    vlines = [re.search(r"variant (\w+):", l).group(1) for l in lines if re.search(r"variant (\w+):", l)]
    cur_v = vlines[-1] if vlines else None
    cur = markers[-1] if markers else None
    marker_key = (cur, cur_v)
    if marker_key != last:
        last, since = marker_key, time.time()
    stage = "1-build"
    if cur:
        m = re.match(r"=== [^:]+: (\w+)", cur)
        stage = STAGE.get(m.group(1), stage) if m else stage
    files = sorted(glob.glob(str(REC / "S-*.jsonl")), key=os.path.getmtime)
    files = [f for f in files if os.path.getmtime(f) >= started - 5]
    meas = measure(files) if files else {}

    results, counts = {}, {"PASS": 0, "INVALID": 0, "PLANNED": 0}
    finished = set(vlines[:-1]) | ({cur_v} if done_all and cur_v else set())
    for lab in LABELS:
        v = SPEC[lab]
        desc = v.get("knobs", "") if isinstance(v, dict) else v[1]
        extra = {k: v[k] for k in ("size", "skew", "burst", "senders", "interferers", "node_knobs") if isinstance(v, dict) and k in v}
        m = meas.get(lab)
        if m:
            ok = m["n"] > 0
            evidence = [("knobs", f"'{desc}' {json.dumps(extra) if extra else ''}"),
                        ("rounds delivered whole", pct(m["whole"], m["n"])),
                        ("senders decoded each other", pct(m["xh"], m["xn"]))]
            verdict = ("PASS" if ok else "INVALID") if lab in finished else "RUNNING"
            results[lab] = {"scenario_id": lab, "verdict": verdict, "error": None,
                            "outcomes": [{"name": k, "verdict": "PASS", "evidence": e} for k, e in evidence],
                            "started_at": m["t0"], "ended_at": m["t1"], "images": {"all": "knobs2"}}
            if verdict in counts:
                counts[verdict] += 1
        else:
            results[lab] = {"scenario_id": lab, "verdict": "PLANNED", "error": None,
                            "outcomes": [{"name": "knobs", "verdict": "PLANNED", "evidence": f"'{desc}' {json.dumps(extra) if extra else ''}"}]}
            counts["PLANNED"] += 1
    write("results.json", results)

    waiting = (cur[4:] if cur else "starting")
    if stage == "4-execute" and cur_v and cur_v in meas and not done_all:
        m = meas[cur_v]
        waiting = f"variant {cur_v} ({LABELS.index(cur_v) + 1}/{len(LABELS)}): {m['n']} rounds, {m['whole']} whole"
    tail = next((l for l in reversed(lines) if l.strip() and not l.startswith("===")), "")
    if tail and stage != "4-execute":
        waiting += f" | {tail[:120]}"
    if done_all:
        stage, waiting = "done", "sweep finished"
    elif failed:
        waiting = "FAILED: " + next((l for l in reversed(lines) if "Error" in l), "")[:160]

    new = lines[mirrored:]
    if new:
        with open(OUT / "logs.jsonl", "a", encoding="utf-8") as f:
            for l in new:
                mm = re.match(r"^\d\d:\d\d:\d\d (\w+):", l)
                f.write(json.dumps({"ts": time.time(), "node": mm.group(1) if mm and mm.group(1) in NODES else "runner",
                                    "source": "runner", "line": l}) + "\n")
        mirrored = len(lines)

    done = sum(1 for r in results.values() if r["verdict"] in ("PASS", "INVALID"))
    write("state.json", {
        "run_dir": str(OUT), "started_at": started, "elapsed_s": round(time.time() - started, 1),
        "operator_note": "knob sweep (abrun.py sweep), mirrored by dash_sweep.py - not python -m bench",
        "stage": stage, "row": cur_v, "done": done, "total": len(LABELS), "waiting_for": waiting,
        "waiting_since": since, "counts": counts, "scenarios": LABELS,
        "nodes": [{"name": n, "serial_number": sn, "role": "node", "board": "NRF52_PROMICRO_DIY"} for n, sn in NODES.items()],
    })
    if done_all or failed:
        break
    with open(OUT / "status.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "component": "runner", "stage": stage, "row": cur_v, "done": done,
                            "total": len(LABELS)}) + "\n")
    time.sleep(5)
