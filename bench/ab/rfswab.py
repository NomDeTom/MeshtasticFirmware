"""A/B the LR2021 RF switch table on b6d6 (COM23) against the SX1262 0e3c (COM31).

Per table: lr -> sx with unicast POSITION/ROUTING frames to a node that does not exist (sx logs "Lora RX ... rxRSSI"),
sx -> lr with raw bench emits (lr logs its RX, good or bad, with RSSI). Both at pwr=-9 so a wrong path costs frames.
"""

import json
import re
import statistics
import sys
import time

import meshtastic.serial_interface
from pubsub import pub

OUT = sys.argv[1]
PWR = int(sys.argv[2]) if len(sys.argv) > 2 else -9
N = 8
TABLES = [
    ("A-default", "default"),
    ("E-all-low-6", "0,0,0,0,0,0"),
    ("J-10rx-11tx", "0,0,0,0,2,4"),
    ("K-10tx-11rx", "0,0,0,0,4,2"),
    ("L-10-11-both", "0,0,0,0,6,6"),
    ("M-10tx-11rxtx", "0,0,0,0,4,6"),
    ("N-10rxtx-11tx", "0,0,0,0,6,4"),
    ("O-default-plus-10rx-11tx", "6,4,0,6,2,4"),
    ("P-all-high-rxtx", "6,6,6,6,6,6"),
    ("A-default-again", "default"),
]
NOBODY = 0x0BADF00D

log = open(OUT, "a", encoding="utf-8")
names, state, lines = {}, {"label": "setup"}, []


def emit(s):
    s = f"{time.strftime('%H:%M:%S')} {s}"
    print(s, flush=True)
    log.write(s + "\n")
    log.flush()


def on_log(line, interface=None):
    who = names.get(id(interface), "?")
    if "Lora RX" in line or "Ignore rx packet" in line or "BENCH rfsw" in line or "BENCH e emit" in line or "Started Tx" in line:
        lines.append((state["label"], who, line.strip()))
        emit(f"[{state['label']}] {who}: {line.strip()[:200]}")


pub.subscribe(on_log, "meshtastic.log.line")
sx = meshtastic.serial_interface.SerialInterface("COM31")
lr = meshtastic.serial_interface.SerialInterface("COM23")
names[id(sx)], names[id(lr)] = "sx", "lr"
time.sleep(2)


def bench(i, cmd, wait=1.0):
    i.sendText(cmd, wantAck=False)
    time.sleep(wait)


def rssi_snr(ls):
    r, s = [], []
    for l in ls:
        m = re.search(r"rxSNR=(-?[\d.]+) rxRSSI=(-?\d+)", l)
        if m:
            s.append(float(m.group(1)))
            r.append(int(m.group(2)))
    return r, s


def med(x):
    return statistics.median(x) if x else None


results = []
try:
    bench(sx, f"!bench reset pwr={PWR} lbt=off", 2)
    bench(lr, f"!bench reset pwr={PWR} lbt=off", 2)
    for name, tbl in TABLES:
        state["label"] = name
        emit(f"=== {name}: rfsw={tbl}")
        bench(lr, f"!bench rfsw={tbl}", 2)
        # lr -> sx
        for n in range(N):
            lr.sendData(b"", destinationId=NOBODY, portNum=3 if n % 2 == 0 else 5, wantAck=False, hopLimit=0)
            time.sleep(2.5)
        # sx -> lr
        for n in range(N):
            bench(sx, "!bench emit=32", 1.0)
        time.sleep(1)
        lr_tx = [l for (lb, w, l) in lines if lb == name and w == "lr" and "Started Tx" in l]
        sx_rx = [l for (lb, w, l) in lines if lb == name and w == "sx" and ("Lora RX" in l or "Ignore rx" in l)]
        sx_tx = [l for (lb, w, l) in lines if lb == name and w == "sx" and "BENCH e emit" in l]
        lr_rx = [l for (lb, w, l) in lines if lb == name and w == "lr" and ("Lora RX" in l or "Ignore rx" in l) and "len=32" in l]
        r1, s1 = rssi_snr(sx_rx)
        r2, s2 = rssi_snr(lr_rx)
        r = dict(table=name, rfsw=tbl, lr_to_sx=f"{len(sx_rx)}/{len(lr_tx)}", lr_to_sx_rssi=med(r1), lr_to_sx_snr=med(s1),
                 sx_to_lr=f"{len(lr_rx)}/{len(sx_tx)}", sx_to_lr_rssi=med(r2), sx_to_lr_snr=med(s2))
        results.append(r)
        emit("### " + json.dumps(r))
finally:
    state["label"] = "restore"
    emit("RESULTS " + json.dumps(results))
    for i in (lr, sx):
        try:
            bench(i, "!bench reset rfsw=default pwr=default", 1)
        except Exception:
            pass
        i.close()
    emit("DONE")
