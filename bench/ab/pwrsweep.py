"""TX power sweep: the transmitter steps pwr, the receiver logs RSSI/SNR. RSSI should follow the set power 1 dB/dB;
a step, plateau or drop points at a PA or RF-switch path that changes with power.

usage: pwrsweep.py out.log tx(lr|sx) [powers csv]
"""

import json
import re
import statistics
import sys
import time

import meshtastic.serial_interface
from pubsub import pub

OUT, TX = sys.argv[1], sys.argv[2]
POWERS = [int(p) for p in sys.argv[3].split(",")] if len(sys.argv) > 3 and sys.argv[3] else list(range(-9, 23))
EXTRA = sys.argv[4] if len(sys.argv) > 4 else ""  # knobs applied with every step, e.g. "patab=ds"
N = 4
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
    if "BENCH front" in line or "Lora RX" in line or "setOutputPower" in line or "Started Tx" in line or "rror" in line:
        lines.append((state["label"], who, line.strip()))


pub.subscribe(on_log, "meshtastic.log.line")
sx = meshtastic.serial_interface.SerialInterface("COM31")
lr = meshtastic.serial_interface.SerialInterface("COM23")
names[id(sx)], names[id(lr)] = "sx", "lr"
tx, rxn = (lr, "sx") if TX == "lr" else (sx, "lr")
txn = TX
txnum = tx.myInfo.my_node_num
time.sleep(2)
results = []
try:
    for i in (sx, lr):
        i.sendText("!bench reset", wantAck=False)
        time.sleep(1.5)
    for p in POWERS:
        state["label"] = lb = f"p{p}"
        tx.sendText(f"!bench pwr={p} {EXTRA}".strip(), wantAck=False)
        time.sleep(2)
        for n in range(N):
            tx.sendData(b"", destinationId=NOBODY, portNum=3 if n % 2 == 0 else 5, wantAck=False, hopLimit=0)
            time.sleep(2.5)
        rx = [l for (b, w, l) in lines if b == lb and w == rxn and "Lora RX" in l and f"fr=0x{txnum:08x}" in l]
        sent = [l for (b, w, l) in lines if b == lb and w == txn and "Started Tx" in l]
        errs = [l for (b, w, l) in lines if b == lb and w == txn and ("setOutputPower" in l or "rror" in l)]
        rssi = [int(m.group(1)) for l in rx if (m := re.search(r"rxRSSI=(-?\d+)", l))]
        snr = [float(m.group(1)) for l in rx if (m := re.search(r"rxSNR=(-?[\d.]+)", l))]
        r = dict(pwr=p, rx=f"{len(rx)}/{len(sent)}", rssi=statistics.median(rssi) if rssi else None,
                 snr=statistics.median(snr) if snr else None, errs=errs[:2])
        results.append(r)
        emit("### " + json.dumps(r))
finally:
    state["label"] = "restore"
    emit("RESULTS " + json.dumps(results))
    for i in (sx, lr):
        try:
            i.sendText("!bench reset pwr=default", wantAck=False)
            time.sleep(1)
        except Exception:
            pass
        i.close()
    emit("DONE")
