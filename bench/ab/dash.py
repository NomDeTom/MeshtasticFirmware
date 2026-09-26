"""Sidecar: mirror the lean A/B run into a bench-format run dir so `bench serve` shows it.

Reads run_ab.log and ~/bench-runs/ab-lean/C-*.jsonl; writes ~/bench-runs/ab-lean-dash/
{state.json, results.json, status.jsonl, events.jsonl}. Heartbeats stop when the log shows
the run finished or failed, so a dead run reads DIED rather than a frozen RUNNING.
"""

import glob
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
LOG = HERE / "run_ab.log"
REC = Path.home() / "bench-runs" / "ab-lean"
OUT = Path.home() / "bench-runs" / "ab-lean-dash"
ROWS = ["C-SF-dev", "C-NF-dev", "C-NS-dev", "C-NS-lbt", "C-NF-lbt", "C-SF-lbt"]
PAIRS = {"SF": 80, "NF": 60, "NS": 50}
WITNESSES = ("witness", "lr", "w2")
STAGE = {"flash": "2-flash", "provision": "3-provision", "contend": "4-execute"}
PAT = re.compile(r"^C-(\w+)-(\w+)-(dut|peer)-(\d+)$")
OUT.mkdir(parents=True, exist_ok=True)
from abrun import NODES, SENDERS  # noqa: E402

NODE_ROLE = {n: ("sender" if n in SENDERS else "witness") for n in NODES}
BUDGET = {"flash": 330, "provision": 330, "contend": {"SF": 240, "NF": 280, "NS": 340}}
PLAN = (
    [("dev", "flash", None)]
    + [("dev", ph, k) for k in ("SF", "NF", "NS") for ph in ("provision", "contend")]
    + [("lbt", "flash", None)]
    + [("lbt", ph, k) for k in ("NS", "NF", "SF") for ph in ("provision", "contend")]
)


def row_file(rid):
    files = sorted(glob.glob(str(REC / f"{rid}-*.jsonl")))
    return files[-1] if files else None


def measure(path):
    sent, seen, t0, t1 = {"dut": set(), "peer": set()}, {}, None, None
    for line in open(path, encoding="utf-8"):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        t0 = t0 or d.get("ts")
        t1 = d.get("ts") or t1
        m = PAT.match(d.get("text") or "")
        if not m:
            continue
        if d["kind"] == "tx":
            sent[m.group(3)].add(int(m.group(4)))
        elif d["kind"] == "rx" and d["node"] != m.group(3):
            seen.setdefault(d["node"], set()).add((m.group(3), int(m.group(4))))
    pairs = sorted(sent["dut"] & sent["peer"])
    anyw = set().union(*(seen.get(w, set()) for w in WITNESSES))
    both = lambda s: sum(
        1 for i in pairs if ("dut", i) in s and ("peer", i) in s
    )  # noqa: E731
    return {
        "n": len(pairs),
        "both_any": both(anyw),
        "dut_from_peer": sum(1 for i in pairs if ("peer", i) in seen.get("dut", set())),
        "peer_from_dut": sum(1 for i in pairs if ("dut", i) in seen.get("peer", set())),
        "per_witness": {w: both(seen.get(w, set())) for w in WITNESSES},
        "frames_at_witnesses": sum(len(seen.get(w, set())) for w in WITNESSES),
        "t0": t0,
        "t1": t1,
    }


def pct(a, b):
    return f"{a}/{b} ({a / b:.0%})" if b else f"{a}/0"


def result_for(rid, m):
    ok = m["n"] > 0 and m["frames_at_witnesses"] > 0
    ev = [
        ("pairs delivered whole (any witness)", pct(m["both_any"], m["n"])),
        ("dut decoded peer's frame", pct(m["dut_from_peer"], m["n"])),
        ("peer decoded dut's frame", pct(m["peer_from_dut"], m["n"])),
    ] + [(f"pairs whole at {w}", pct(v, m["n"])) for w, v in m["per_witness"].items()]
    return {
        "scenario_id": rid,
        "verdict": "PASS" if ok else "INVALID",
        "outcomes": [
            {"name": k, "verdict": "PASS" if ok else "INVALID", "evidence": v}
            for k, v in ev
        ],
        "images": {"all": "ab_develop" if rid.endswith("dev") else "ab_lbt"},
        "release_representative": True,
        "started_at": m["t0"],
        "ended_at": m["t1"],
        "error": None if ok else "no pairs or nothing heard",
    }


def write(name, obj):
    tmp = OUT / (name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
    os.replace(tmp, OUT / name)


mirrored = 0
started = LOG.stat().st_ctime if LOG.exists() else time.time()
last_marker, marker_seen_at = None, time.time()
while True:
    lines = (
        LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        if LOG.exists()
        else []
    )
    markers = [l for l in lines if l.startswith("=== ")]
    done_all = any("ALL DONE" in l for l in markers)
    failed = any(("Traceback" in l or l.startswith("RuntimeError")) for l in lines)
    cur = markers[-1] if markers else None
    if cur != last_marker:
        last_marker, marker_seen_at = cur, time.time()
        with open(OUT / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(
                json.dumps({"ts": time.time(), "kind": "lean_phase", "line": cur})
                + "\n"
            )

    # rows finished = contend markers followed by a later marker (or the end)
    contend_rows = [
        f"C-{m.group(2)}-{m.group(1)}"
        for l in markers
        if (m := re.match(r"=== (\w+) (\w+): contend", l))
    ]
    finished = (
        contend_rows[:-1]
        if (cur and ": contend" in cur and not done_all)
        else contend_rows
    )
    results = {}
    for rid in finished:
        f = row_file(rid)
        if f:
            results[rid] = result_for(rid, measure(f))
    stage, row, waiting = "1-build", None, cur[4:] if cur else "starting"
    if cur:
        m = re.match(r"=== (\w+)(?: (\w+))?: (\w+)", cur)
        if m:
            stage = STAGE.get(m.group(3), stage)
            row = f"C-{m.group(2)}-{m.group(1)}" if m.group(2) else None
    if stage == "4-execute" and row and not done_all:
        f = row_file(row)
        if f and os.path.getmtime(f) >= marker_seen_at - 5:
            mm = measure(f)
            waiting = (
                f"{row}: {mm['n']}/{PAIRS[row.split('-')[1]]} pairs sent, "
                f"{mm['both_any']} whole so far, {mm['frames_at_witnesses']} frames at witnesses"
            )
    tail = next(
        (l for l in reversed(lines) if l.strip() and not l.startswith("===")), ""
    )
    if tail and stage != "4-execute":
        waiting = f"{waiting} | {tail[:120]}"
    if done_all:
        stage, waiting = "done", "all rows finished"
    elif failed:
        waiting = (
            f"FAILED: {next((l for l in reversed(lines) if 'Error' in l), '')[:160]}"
        )

    # live + planned rows, so the table shows the whole run, not only finished rows
    table = dict(results)
    for rid in ROWS:
        if rid in table:
            continue
        if rid == row and stage == "4-execute" and not done_all:
            f = row_file(rid)
            live = (
                result_for(rid, measure(f))
                if f and os.path.getmtime(f) >= marker_seen_at - 5
                else None
            )
            table[rid] = {
                **(live or {"outcomes": []}),
                "scenario_id": rid,
                "verdict": "RUNNING",
                "error": None,
            }
        else:
            table[rid] = {"scenario_id": rid, "verdict": "PLANNED", "outcomes": []}
    write("results.json", {k: table[k] for k in ROWS})

    # devices: node table + what the runner log last said about each node
    ports = {}
    for n in NODES:
        fw = None
        for l in lines:
            mm = re.search(rf"\b{n}: answering, firmware (\S+)", l)
            if mm:
                fw = mm.group(1)
        last = next((l for l in reversed(lines) if re.search(rf"\b{n}:", l)), "")
        busy = bool(last) and not ("answering" in last or "settled" in last)
        ports[n] = {
            "role": NODE_ROLE[n],
            "firmware": fw,
            "declared_board": "NRF52_PROMICRO_DIY",
            "state": (
                "leased"
                if busy and stage in ("2-flash", "3-provision")
                else "held" if stage == "4-execute" else "idle"
            ),
            "capture": "protobuf api (per row)",
        }

    # schedule: one step per phase of the plan, marked from the markers seen so far
    seen = [
        (m.group(1), m.group(3), m.group(2))
        for l in markers
        if (m := re.match(r"=== (\w+)(?: (\w+))?: (\w+)", l))
    ]
    steps = []
    for idx, (lab, ph, key) in enumerate(PLAN):
        status = "planned"
        if (lab, ph, key) in seen:
            status = (
                "running"
                if seen[-1] == (lab, ph, key) and not (done_all or failed)
                else "done"
            )
            if failed and seen[-1] == (lab, ph, key):
                status = "failed"
        budget = BUDGET[ph][key] if ph == "contend" else BUDGET[ph]
        steps.append(
            {
                "id": f"{lab}:{ph}:{key}",
                "name": f"{lab} {ph} {key or 'all nodes'}",
                "budget_s": budget,
                "detail": "",
                "kind": ph,
                "node": None,
                "status": status,
                "recorded_status": status,
                "over_by_s": None,
                "outcome": None,
                "elapsed_s": None,
                "overran": False,
                "children": [],
            }
        )
    schedule = {"steps": steps, "total_s": sum(x["budget_s"] for x in steps)}

    # log tail: runner lines not yet mirrored
    new = lines[mirrored:]
    if new:
        with open(OUT / "logs.jsonl", "a", encoding="utf-8") as f:
            for l in new:
                mm = re.match(r"^\d\d:\d\d:\d\d (\w+):", l)
                node = mm.group(1) if mm and mm.group(1) in NODES else "runner"
                f.write(
                    json.dumps(
                        {"ts": time.time(), "node": node, "source": "runner", "line": l}
                    )
                    + "\n"
                )
        mirrored = len(lines)

    counts = {}
    for r in results.values():
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    counts["PLANNED"] = len(ROWS) - len(results)
    write(
        "state.json",
        {
            "run_dir": str(OUT),
            "started_at": started,
            "elapsed_s": round(time.time() - started, 1),
            "operator_note": "lean A/B runner (abrun.py), mirrored by dash.py - not python -m bench",
            "stage": stage,
            "row": row,
            "done": len(results),
            "total": len(ROWS),
            "waiting_for": waiting,
            "waiting_since": marker_seen_at,
            "counts": counts,
            "scenarios": ROWS,
            "nodes": [
                {
                    "name": n,
                    "serial_number": sn,
                    "role": NODE_ROLE[n],
                    "board": "NRF52_PROMICRO_DIY",
                }
                for n, sn in NODES.items()
            ],
            "ports": ports,
            "schedule": schedule,
        },
    )
    if not (done_all or failed):
        with open(OUT / "status.jsonl", "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "ts": time.time(),
                        "component": "runner",
                        "stage": stage,
                        "row": row,
                        "waiting_for": waiting,
                        "done": len(results),
                        "total": len(ROWS),
                    }
                )
                + "\n"
            )
    else:
        break
    time.sleep(5)
