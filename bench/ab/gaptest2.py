"""Over-the-air TX->RX and RX->RX deaf-gap sweeps, scored from firmware lines only.

Frames go as unicast PRIVATE_APP to a node that does not exist: unsigned (no XEdDSA), no PKI, no text rate limit, and
the receiver does only the AES-CTR decode, so the main loop is not held ~300 ms by a signature check.

ota:  DUT sends a frame; responder holds a queued frame (trig=DUT at=X lbt=off nobackoff) and keys up X ms after its
      RX_DONE. Heard = the DUT logs an rxgap line with tx= (ms since its own TX_DONE) under 200.
rxrx: SX1262 sends a frame, then emits a raw frame efollow=X ms after its own TX_DONE. The LR2021 must re-arm after
      the first to catch the second. Heard = an rxgap line with prev= under 200.
"""

import json
import re
import sys
import time

import meshtastic.serial_interface
from pubsub import pub

PORTS = {"sx": "COM31", "lr": "COM23"}
NOBODY = 0x0BADF00D
OUT = sys.argv[1]
PHASES = sys.argv[2].split(",") if len(sys.argv) > 2 else ["ota", "rxrx"]
N = 5
OTA_AT = [0, 1, 2, 3, 4, 5, 6, 8, 10]
EFOLLOW = [0, 1, 2, 3, 4, 5, 6, 8, 10]
CFGS = [("develop", "!bench develop txgap=1"), ("fixed", "!bench reset txgap=1")]

log = open(OUT, "a", encoding="utf-8")
ifaces, nums, names = {}, {}, {}
state = {"label": "setup"}
lines = []  # (label, who, line)


def emit(s):
    line = f"{time.strftime('%H:%M:%S')} {s}"
    print(line, flush=True)
    log.write(line + "\n")
    log.flush()


def on_log(line, interface=None):
    who = names.get(id(interface), "?")
    if "BENCH" in line and "knobs" not in line:
        lines.append((state["label"], who, line.strip()))
        emit(f"[{state['label']}] {who}: {line.strip()}")


pub.subscribe(on_log, "meshtastic.log.line")
for k, p in PORTS.items():
    i = meshtastic.serial_interface.SerialInterface(p)
    ifaces[k], names[id(i)], nums[k] = i, k, i.myInfo.my_node_num
    emit(f"{k} on {p}: node {nums[k]:08x} fw {i.metadata.firmware_version if i.metadata else '?'}")
time.sleep(2)


def bench(k, cmd, wait=1.0):
    ifaces[k].sendText(cmd, wantAck=False)
    time.sleep(wait)


def frame(k, tag):
    ifaces[k].sendData(b"", destinationId=NOBODY, portNum=3, wantAck=False, hopLimit=0)


def field(line, name):
    m = re.search(rf"\b{name}=(-?\d+)", line)
    return int(m.group(1)) if m else None


def round_lines(label, who, key):
    return [l for (lb, w, l) in lines if lb == label and w == who and key in l]


results = {"ota": [], "rxrx": []}
try:
    if "ota" in PHASES:
        for dut, resp in (("sx", "lr"), ("lr", "sx")):
            for cfg, cmd in CFGS:
                bench(dut, cmd, 2)
                for at in OTA_AT:
                    bench(resp, f"!bench reset txgap=1 lbt=off nobackoff=1 trig={nums[dut]} at={at}", 1.5)
                    heard, starts, txs = 0, [], []
                    for n in range(N):
                        state["label"] = lb = f"ota-{dut}-{cfg}-at{at}-{n}"
                        frame(resp, f"pong {n}")
                        time.sleep(0.8)
                        frame(dut, f"ping {n}")
                        time.sleep(1.2)
                        rx = [field(l, "tx") for l in round_lines(lb, dut, "rxgap")]
                        rx = [t for t in rx if t is not None and t < 200]
                        st = [int(l.split("+")[1]) for l in round_lines(lb, resp, "t txstart")]
                        heard += bool(rx)
                        starts += st
                        txs += rx
                        emit(f"--- {lb}: heard={int(bool(rx))} dut_tx_to_rxdone_ms={rx} resp_txstart_us={st}")
                    r = dict(dut=dut, cfg=cfg, at=at, heard=heard, n=N, resp_txstart_us=starts, rx_after_tx_ms=txs)
                    results["ota"].append(r)
                    emit(f"### ota {json.dumps(r)}")
            bench(resp, "!bench reset txgap=1", 11)  # a held frame times out and clears

    if "rxrx" in PHASES:
        dut, snd = "lr", "sx"
        bench(snd, "!bench reset txgap=1", 1.5)
        for cfg, cmd in CFGS:
            bench(dut, cmd, 2)
            for ef in EFOLLOW:
                heard, prevs, lates, valid = 0, [], [], 0
                for n in range(N):
                    state["label"] = lb = f"rxrx-{dut}-{cfg}-ef{ef}-{n}"
                    bench(snd, f"!bench efollow={ef}", 0.8)
                    frame(snd, f"lead {n}")
                    time.sleep(1.5)
                    gaps = round_lines(lb, dut, "rxgap")
                    pv = [field(l, "prev") for l in gaps]
                    pv = [p for p in pv if p is not None and p < 200]
                    late = [field(l, "late") for l in round_lines(lb, snd, "e follow")]
                    valid += bool(late)
                    heard += bool(pv) and bool(late)
                    prevs += pv
                    lates += late
                    emit(f"--- {lb}: heard={int(bool(pv))} rx_rx_ms={pv} emit_late_ms={late} rxgap_lines={len(gaps)}")
                r = dict(dut=dut, cfg=cfg, efollow=ef, heard=heard, n=valid, rx_rx_ms=prevs, emit_late_ms=lates)
                results["rxrx"].append(r)
                emit(f"### rxrx {json.dumps(r)}")
    if "epre" in PHASES:
        # efollow=0 puts the emitted preamble ~3 ms after the lead frame ends; a shorter preamble leaves the receiver
        # less of it once RX is re-armed. The efollow=400 control checks the receiver decodes that preamble at all.
        dut, snd = "lr", "sx"
        results["epre"] = []
        bench(dut, "!bench reset txgap=1", 2)
        for pre in [4, 3, 2, 1]:
            for ef in (0, 400):
                bench(snd, f"!bench reset txgap=1 epre={pre}", 1.5)
                heard, prevs, valid = 0, [], 0
                for n in range(8):
                    state["label"] = lb = f"epre-{pre}-ef{ef}-{n}"
                    bench(snd, f"!bench efollow={ef}", 0.8)
                    frame(snd, f"lead {n}")
                    time.sleep(2.0)
                    late = round_lines(lb, snd, "e follow")
                    pv = [field(l, "prev") for l in round_lines(lb, dut, "rxgap")]
                    pv = [p for p in pv if p is not None and p < 1000]
                    valid += bool(late)
                    heard += bool(pv) and bool(late)
                    prevs += pv
                    emit(f"--- {lb}: heard={int(bool(pv))} rx_rx_ms={pv} emitted={int(bool(late))}")
                r = dict(epre=pre, efollow=ef, heard=heard, n=valid, rx_rx_ms=prevs)
                results["epre"].append(r)
                emit(f"### epre {json.dumps(r)}")
finally:
    state["label"] = "restore"
    emit("RESULTS " + json.dumps(results))
    for k in ifaces:
        try:
            bench(k, "!bench reset", 1)
        except Exception:
            pass
        ifaces[k].close()
    emit("DONE")
