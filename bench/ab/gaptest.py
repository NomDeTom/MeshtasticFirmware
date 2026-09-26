"""TX->RX and RX->RX deaf-gap tests on two knobs13(+rxgap) nodes.

phase txgap: each node sends alone; BENCH txgap per config (develop / xosc only / early only / fixed defaults).
phase ota:   DUT sends a ping; responder holds a queued frame (trig=DUT, at=X, lbt=off) and sends it X ms after its
             RX_DONE. The DUT hears it only if its RX is re-armed in time. Sweep X per DUT config.
phase rxrx:  sender queues two frames back to back (nobackoff, lbt=off, fixed=X); the DUT must re-arm after the first
             to catch the second. BENCH rxgap on the DUT, count of second frames heard.
"""

import json
import sys
import time

import meshtastic.serial_interface
from pubsub import pub

PORTS = {"sx": "COM31", "lr": "COM23"}
OUT = sys.argv[1]
PHASES = sys.argv[2].split(",") if len(sys.argv) > 2 else ["txgap", "ota", "rxrx"]
DUTS = sys.argv[3].split(",") if len(sys.argv) > 3 else ["sx", "lr"]

CONFIGS = [
    ("develop", "!bench develop txgap=1"),
    ("xosc", "!bench develop txgap=1 xosc=1"),
    ("early", "!bench develop txgap=1 txarm=early"),
    ("fixed", "!bench reset txgap=1"),
]
OTA_CONFIGS = [CONFIGS[0], CONFIGS[3]]
OTA_AT = [0, 2, 4, 6, 8, 10, 12, 15]
OTA_N = 6
RXRX_FIXED = [0, 3, 6, 10]
RXRX_N = 6

log = open(OUT, "a", encoding="utf-8")
ifaces, nums, names = {}, {}, {}
state = {"label": "setup"}
rx = []  # (t, receiver, from, text)


def emit(s):
    line = f"{time.strftime('%H:%M:%S')} {s}"
    print(line, flush=True)
    log.write(line + "\n")
    log.flush()


def on_log(line, interface=None):
    who = names.get(id(interface), "?")
    if "BENCH" in line or "standby aborts" in line or "GPS power" in line or "Set radio" in line or "Error" in line:
        emit(f"[{state['label']}] {who}: {line.strip()}")


def on_rx(packet, interface=None):
    who = names.get(id(interface), "?")
    text = packet.get("decoded", {}).get("text")
    rx.append((time.time(), who, packet.get("from"), text))
    emit(f"[{state['label']}] {who}: RX from={packet.get('from', 0):08x} text={text!r} snr={packet.get('rxSnr')}")


pub.subscribe(on_log, "meshtastic.log.line")
pub.subscribe(on_rx, "meshtastic.receive")

for k, p in PORTS.items():
    i = meshtastic.serial_interface.SerialInterface(p)
    ifaces[k], names[id(i)], nums[k] = i, k, i.myInfo.my_node_num
    emit(f"{k} on {p}: node {nums[k]:08x} fw {i.metadata.firmware_version if i.metadata else '?'}")
time.sleep(2)


def bench(k, cmd, wait=1.0):
    ifaces[k].sendText(cmd, wantAck=False)
    time.sleep(wait)


def say(k, text):
    ifaces[k].sendText(text, wantAck=False, hopLimit=0)


def other(k):
    return "lr" if k == "sx" else "sx"


try:
    if "txgap" in PHASES:
        for dut in DUTS:
            peer = other(dut)
            bench(peer, "!bench reset txgap=1")  # the listener logs rxgap for each frame it hears
            for name, cmd in CONFIGS:
                state["label"] = f"txgap-{dut}-{name}"
                emit(f"=== {state['label']}: {cmd}")
                bench(dut, cmd, 2)
                for n in range(6):
                    say(dut, f"txgap {dut} {name} {n}")
                    time.sleep(3)

    if "ota" in PHASES:
        results = []
        for dut in DUTS:
            resp = other(dut)
            # responder: fixed defaults (fast TX start on the SX1262), no LBT, no backoff, fire X ms after the DUT's frame
            for name, cmd in OTA_CONFIGS:
                bench(dut, cmd, 2)
                for at in OTA_AT:
                    state["label"] = f"ota-{dut}-{name}-at{at}"
                    emit(f"=== {state['label']}")
                    bench(resp, f"!bench reset txgap=1 lbt=off nobackoff=1 trig={nums[dut]} at={at}", 1.5)
                    heard = 0
                    for n in range(OTA_N):
                        say(resp, f"pong {dut} {name} {at} {n}")
                        time.sleep(0.5)
                        t0 = time.time()
                        say(dut, f"ping {dut} {name} {at} {n}")
                        time.sleep(2.5)
                        got = [r for r in rx if r[0] >= t0 and r[1] == dut and r[2] == nums[resp]]
                        heard += bool(got)
                        emit(f"--- round {n}: heard={int(bool(got))}")
                    results.append((dut, name, at, heard, OTA_N))
                    emit(f"### ota dut={dut} cfg={name} at={at} heard {heard}/{OTA_N}")
            bench(resp, "!bench reset txgap=1", 12)  # also lets a held frame time out and clear
        emit("OTA " + json.dumps(results))

    if "rxrx" in PHASES:
        results = []
        for dut in DUTS:
            snd = other(dut)
            for name, cmd in OTA_CONFIGS:
                bench(dut, cmd, 2)
                for fx in RXRX_FIXED:
                    state["label"] = f"rxrx-{dut}-{name}-f{fx}"
                    emit(f"=== {state['label']}")
                    bench(snd, f"!bench reset txgap=1 lbt=off nobackoff=1 fixed={fx}", 1.5)
                    both = 0
                    for n in range(RXRX_N):
                        t0 = time.time()
                        say(snd, f"a {fx} {n}")
                        say(snd, f"b {fx} {n}")
                        time.sleep(3)
                        got = [r[3] for r in rx if r[0] >= t0 and r[1] == dut and r[2] == nums[snd]]
                        ok2 = any(t and t.startswith("b ") for t in got)
                        both += ok2
                        emit(f"--- round {n}: got={got}")
                    results.append((dut, name, fx, both, RXRX_N))
                    emit(f"### rxrx dut={dut} cfg={name} fixed={fx} second heard {both}/{RXRX_N}")
            bench(snd, "!bench reset txgap=1", 2)
        emit("RXRX " + json.dumps(results))
finally:
    state["label"] = "restore"
    for k in ifaces:
        try:
            bench(k, "!bench reset", 1)
        except Exception:
            pass
        ifaces[k].close()
    emit("DONE")
