"""TX_DONE -> RX deaf gap on the SX1262 (0e3c): develop, the rx-rearm PR (israrm), and the listening-now fixes.

quiet: one unsigned unicast frame - nothing competes with the TX_DONE handler.
stall: two signed broadcasts back to back - the second one's XEdDSA signing holds the main loop while the first one's
       TX_DONE fires, the kind of hold the PR is for.
After each round the LR2021 sends one frame; the SX1262 must still receive it (RX works after an ISR re-arm).
"""

import json
import re
import statistics
import sys
import time

import meshtastic.serial_interface
from pubsub import pub

OUT = sys.argv[1]
N = int(sys.argv[2]) if len(sys.argv) > 2 else 6
NOBODY = 0x0BADF00D
CONFIGS = [
    ("develop", "!bench develop txgap=1"),
    ("develop+pr", "!bench develop txgap=1 israrm=2"),
    ("develop+pr+xosc", "!bench develop txgap=1 israrm=2 xosc=1"),
    ("knobs13", "!bench reset txgap=1"),
    ("knobs13+pr", "!bench reset txgap=1 israrm=2"),
]

log = open(OUT, "a", encoding="utf-8")
names, state, lines = {}, {"label": "setup"}, []


def emit(s):
    s = f"{time.strftime('%H:%M:%S')} {s}"
    print(s, flush=True)
    log.write(s + "\n")
    log.flush()


def on_log(line, interface=None):
    who = names.get(id(interface), "?")
    if "BENCH txgap" in line or "BENCH hold" in line or ("Lora RX" in line and who == "sx") or "standby aborts" in line or "re-arm" in line:
        lines.append((state["label"], who, line.strip()))
        emit(f"[{state['label']}] {who}: {line.strip()[:230]}")


pub.subscribe(on_log, "meshtastic.log.line")
sx = meshtastic.serial_interface.SerialInterface("COM31")
lr = meshtastic.serial_interface.SerialInterface("COM23")
names[id(sx)], names[id(lr)] = "sx", "lr"
time.sleep(2)


def field(l, k):
    m = re.search(rf"\b{k}=(-?\d+)", l)
    return int(m.group(1)) if m else None


def stats(v):
    v = [x for x in v if x is not None]
    return dict(n=len(v), med=statistics.median(v) if v else None, max=max(v) if v else None)


results = []
try:
    lr.sendText("!bench reset", wantAck=False)
    time.sleep(1.5)
    for cfg, cmd in CONFIGS:
        for scen in ("quiet", "hold"):
            state["label"] = lb = f"{cfg}-{scen}"
            sx.sendText(cmd + (" txhold=200" if scen == "hold" else ""), wantAck=False)
            time.sleep(2)
            emit(f"=== {lb}: {cmd}")
            heard = 0
            for n in range(N):
                t0 = len(lines)
                sx.sendData(b"", destinationId=NOBODY, portNum=3 if n % 2 == 0 else 5, wantAck=False, hopLimit=0)
                time.sleep(2.5)
                lr.sendData(b"", destinationId=NOBODY, portNum=5 if n % 2 else 3, wantAck=False, hopLimit=0)
                time.sleep(2.0)
                got = any(w == "sx" and "Lora RX" in l and "fr=0x2f0583bc" in l for (_, w, l) in lines[t0:])
                heard += got
            g = [l for (b, w, l) in lines if b == lb and w == "sx" and "BENCH txgap id" in l]
            r = dict(cfg=cfg, scen=scen, tx=len(g), adopted=sum(field(l, "adopt") == 1 for l in g),
                     wake=stats([field(l, "wake") for l in g]), total=stats([field(l, "total") for l in g]),
                     deaf=stats([field(l, "deaf") for l in g]), ready=stats([field(l, "ready") for l in g if field(l, "ready") != -1]), israrm=stats([field(l, "israrm") for l in g if field(l, "israrm") != -1]),
                     rx_after=f"{heard}/{N}")
            results.append(r)
            emit("### " + json.dumps(r))
finally:
    state["label"] = "restore"
    emit("RESULTS " + json.dumps(results))
    for i in (sx, lr):
        try:
            i.sendText("!bench reset txgap=0", wantAck=False)
            time.sleep(1)
        except Exception:
            pass
        i.close()
    emit("DONE")
