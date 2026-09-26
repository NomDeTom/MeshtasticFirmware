"""Exhaustive LR2021 RF switch permutations over DIO5,6,7,8,10,11.

Each chip mode reads only its own bit of each DIO's SetDioRfSwitchConfig mask, so every permutation that can change a
path is: all 64 TX-bit patterns measured in TX (LR2021 at +20 dBm, on the plateau; SX1262 reports RSSI), and all 64
RX-bit patterns measured in RX (SX1262 at 0 dBm emits raw frames; LR2021 reports RSSI). The other mode's bits stay at
the shipped table (RX: DIO5, DIO8; TX: DIO5, DIO6, DIO8).

usage: rfswfull.py out.log [tx|rx|both]
"""

import json
import re
import statistics
import sys
import time

import meshtastic.serial_interface
from pubsub import pub

OUT = sys.argv[1]
WHICH = sys.argv[2] if len(sys.argv) > 2 else "both"
RANGE = [int(x) for x in sys.argv[3].split(",")] if len(sys.argv) > 3 else list(range(64))
FRAMES = int(sys.argv[4]) if len(sys.argv) > 4 else 2
NOBODY = 0x0BADF00D
RX_BIT, TX_BIT = 2, 4
DEF_RX = [1, 0, 0, 1, 0, 0]  # DIO5..8, 10, 11
DEF_TX = [1, 1, 0, 1, 0, 0]

log = open(OUT, "a", encoding="utf-8")
names, state, lines = {}, {"label": "setup"}, []


def emit(s):
    s = f"{time.strftime('%H:%M:%S')} {s}"
    print(s, flush=True)
    log.write(s + "\n")
    log.flush()


def on_log(line, interface=None):
    who = names.get(id(interface), "?")
    if "Lora RX" in line or "Started Tx" in line or "BENCH e emit" in line or ("BENCH rfsw dio" in line and "err=0/0" not in line):
        lines.append((state["label"], who, line.strip()))


pub.subscribe(on_log, "meshtastic.log.line")
sx = meshtastic.serial_interface.SerialInterface("COM31")
lr = meshtastic.serial_interface.SerialInterface("COM23")
names[id(sx)], names[id(lr)] = "sx", "lr"
lrnum, sxnum = lr.myInfo.my_node_num, sx.myInfo.my_node_num
time.sleep(2)


def masks(rx, tx):
    return ",".join(str((RX_BIT if r else 0) | (TX_BIT if t else 0)) for r, t in zip(rx, tx))


def bits(n):
    return [(n >> i) & 1 for i in range(6)]


def rssi_of(ls):
    v = [int(m.group(1)) for l in ls if (m := re.search(r"rxRSSI=(-?\d+)", l))]
    return statistics.median(v) if v else None


results = []
try:
    sx.sendText("!bench reset", wantAck=False)
    time.sleep(1.5)
    if WHICH in ("tx", "both"):
        lr.sendText("!bench reset pwr=20", wantAck=False)
        time.sleep(2)
        for n in RANGE:
            tx = bits(n)
            state["label"] = lb = f"tx{n:02d}"
            lr.sendText(f"!bench rfsw={masks(DEF_RX, tx)}", wantAck=False)
            time.sleep(1.5)
            for k in range(FRAMES):
                lr.sendData(b"", destinationId=NOBODY, portNum=3 if k == 0 else 5, wantAck=False, hopLimit=0)
                time.sleep(2.5)
            got = [l for (b, w, l) in lines if b == lb and w == "sx" and "Lora RX" in l and f"fr=0x{lrnum:08x}" in l]
            sent = [l for (b, w, l) in lines if b == lb and w == "lr" and "Started Tx" in l]
            bad = [l for (b, w, l) in lines if b == lb and w == "lr" and "BENCH rfsw dio" in l]
            r = dict(mode="tx", tx_dio5_6_7_8_10_11="".join(map(str, tx)), rfsw=masks(DEF_RX, tx), rx=f"{len(got)}/{len(sent)}",
                     rssi=rssi_of(got), chip_err=bad[:1])
            results.append(r)
            emit("### " + json.dumps(r))
    if WHICH in ("rx", "both"):
        lr.sendText("!bench reset", wantAck=False)
        sx.sendText("!bench reset pwr=0", wantAck=False)
        time.sleep(2)
        for n in RANGE:
            rx = bits(n)
            state["label"] = lb = f"rx{n:02d}"
            lr.sendText(f"!bench rfsw={masks(rx, DEF_TX)}", wantAck=False)
            time.sleep(1.5)
            for k in range(FRAMES):
                sx.sendText("!bench emit=32", wantAck=False)
                time.sleep(2.0)
            got = [l for (b, w, l) in lines if b == lb and w == "lr" and "Lora RX" in l and "len=32" in l]
            sent = [l for (b, w, l) in lines if b == lb and w == "sx" and "BENCH e emit" in l]
            bad = [l for (b, w, l) in lines if b == lb and w == "lr" and "BENCH rfsw dio" in l]
            r = dict(mode="rx", rx_dio5_6_7_8_10_11="".join(map(str, rx)), rfsw=masks(rx, DEF_TX), rx=f"{len(got)}/{len(sent)}",
                     rssi=rssi_of(got), chip_err=bad[:1])
            results.append(r)
            emit("### " + json.dumps(r))
finally:
    state["label"] = "restore"
    emit("RESULTS " + json.dumps(results))
    for i in (lr, sx):
        try:
            i.sendText("!bench reset rfsw=default pwr=default", wantAck=False)
            time.sleep(1)
        except Exception:
            pass
        i.close()
    emit("DONE")
