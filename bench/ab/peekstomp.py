"""Foreign-mesh stomp test: does the DUT transmit over a frame from a mesh it cannot decode?

Per round: the emitter (SX126x) sends a marker on our mesh, then efollow ms after its TX_DONE a raw frame on a foreign
sync word. The DUT holds a queued frame until it hears the marker (trig) and decides at `at` ms, inside the foreign frame.
LR sits on the foreign sync word and reports whether the foreign frame survived (stomp = lost); the observer confirms the
DUT's frame. Roles rotate over the SX126x nodes block by block (Latin square); arms interleave, shuffled, every round.

usage: peekstomp.py out.log [preset] [elen] [reps]
"""

import json
import random
import re
import sys
import time

import meshtastic.serial_interface
from meshtastic.protobuf import config_pb2
from pubsub import pub

OUT = sys.argv[1]
PRESET = sys.argv[2] if len(sys.argv) > 2 else "MEDIUM_FAST"
ELEN = int(sys.argv[3]) if len(sys.argv) > 3 else 160
REPS = int(sys.argv[4]) if len(sys.argv) > 4 else 2
FOREIGN_SYNC = "0x12"
EFOLLOW = 150  # after the DUT's ~100 ms decode of the marker, so the peek watcher is polling when the foreign preamble lands
AT = [300, 420, 540]
NODES = {"TC1": "COM3", "TC2": "COM31", "XT": "COM32", "LR": "COM23"}
SX = ["TC1", "TC2", "XT"]
ROTATIONS = [("TC1", "TC2", "XT"), ("TC2", "XT", "TC1"), ("XT", "TC1", "TC2")]  # (emitter, dut, observer)
ARMS = {
    "dev": "lbt=rx pre=deadline",
    "hold": "lbt=rx pre=hold",
    "peek": "lbt=rx pre=hold pk=6 pksym=2 pkfree=1",
    "ln": "lbt=default pre=soft",
    "lnhold": "lbt=default pre=hold",
    "lnpeek": "lbt=default pre=hold pk=6 pksym=2 pkfree=1",
    "off": "lbt=off",
}
NOBODY = 0x0BADF00D

log = open(OUT, "a", encoding="utf-8")
ifaces, nums, names, lines = {}, {}, {}, []
state = {"label": "setup"}


def emit(s):
    s = f"{time.strftime('%H:%M:%S')} {s}"
    print(s, flush=True)
    log.write(s + "\n")
    log.flush()


def on_log(line, interface=None):
    who = names.get(id(interface), "?")
    keep = ("BENCH knobs", "BENCH e ", "BENCH t ", "BENCH pk", "Lora RX", "Ignore rx packet", "BENCH rxgap", "Started Tx", "Rate limit")
    if any(k in line for k in keep):
        lines.append((state["label"], who, line.strip()))
        log.write(f"{time.strftime('%H:%M:%S')} [{state['label']}] {who}: {line.strip()[:220]}\n")


pub.subscribe(on_log, "meshtastic.log.line")
for n, p in NODES.items():
    i = meshtastic.serial_interface.SerialInterface(p)
    ifaces[n], names[id(i)], nums[n] = i, n, i.myInfo.my_node_num
    emit(f"{n} {p} {nums[n]:08x} fw {i.metadata.firmware_version if i.metadata else '?'}")
time.sleep(2)


last_text = {}


def cmd(n, text, wait=1.2, expect=()):
    """Send a !bench command, >= 2.5 s after the last text to that node (phone text limit: one per 2 s), then check the
    node's BENCH knobs echo for every expected substring; retry twice. Returns False if it never took."""
    for attempt in range(3):
        gap = 2.5 - (time.time() - last_text.get(n, 0))
        if gap > 0:
            time.sleep(gap)
        mark = len(lines)
        ifaces[n].sendText(text, wantAck=False)
        last_text[n] = time.time()
        time.sleep(wait)
        echo = " ".join(l for (_, w, l) in lines[mark:] if w == n and "BENCH knobs" in l)
        if all(e in echo for e in expect):
            return True
        emit(f"{n}: command not confirmed (attempt {attempt + 1}): {text}")
    return False


def frame(n):
    ifaces[n].sendData(b"", destinationId=NOBODY, portNum=5, wantAck=False, hopLimit=0)  # ROUTING: no phone rate limit


def field(l, k):
    m = re.search(rf"\b{k}=(-?\d+)", l)
    return int(m.group(1)) if m else None


def plus(l):
    m = re.search(r"\+(\d+)", l)
    return int(m.group(1)) if m else None


# air config: every node on the preset under test
want = getattr(config_pb2.Config.LoRaConfig.ModemPreset, PRESET)
for n, i in ifaces.items():
    lc = i.localNode.localConfig.lora
    if lc.modem_preset != want or not lc.use_preset:
        lc.use_preset = True
        lc.modem_preset = want
        i.localNode.writeConfig("lora")
        emit(f"{n}: preset -> {PRESET}")
time.sleep(8)
for n in NODES:
    cmd(n, "!bench reset txgap=1", 1.0)
assert cmd("LR", f"!bench sync={FOREIGN_SYNC}", 2.0, expect=("sync=18",)), "witness not on the foreign sync"

results = []
try:
    for rep in range(REPS):
        for rot in ROTATIONS:
            E, D, O = rot
            for attempt in range(2):
                block = f"r{rep}-{E}{D}{O}" + ("-retry" if attempt else "")
                emit(f"=== block {block}: emitter {E}, dut {D}, observer {O}, foreign witness LR on {FOREIGN_SYNC}")
                block_rows = []
                for at in AT:
                    order = list(ARMS)
                    random.shuffle(order)
                    for arm in order:
                        state["label"] = lb = f"{block}|{arm}|at{at}"
                        exp = [f"trig={nums[E]:08x}", f"at={at} "] + [kv if not kv.startswith("lbt=rx") else "lbt=rx " for kv in ARMS[arm].split() if kv.startswith(("lbt=", "pre=", "pkfree="))]
                        armed = cmd(D, f"!bench reset txgap=1 {ARMS[arm]} trig={nums[E]} at={at}", expect=tuple(exp))
                        frame(D)
                        time.sleep(0.6)
                        armed &= cmd(E, f"!bench efollow={EFOLLOW} esync={FOREIGN_SYNC} elen={ELEN}", 1.0, expect=(f"efollow={EFOLLOW}", "esync=18"))
                        frame(E)
                        time.sleep(3.5)
                        L = [(w, l) for (b, w, l) in lines if b == lb]
                        em = [l for w, l in L if w == E and "BENCH e emit" in l]
                        late = [field(l, "late") for w, l in L if w == E and "e follow" in l]
                        wit = [l for w, l in L if w == "LR" and ("Lora RX" in l or "Ignore rx" in l) and f"len={ELEN}" in l]
                        wit_ok = any("Lora RX" in l for l in wit)
                        trig = any("BENCH t trig" in l for w, l in L if w == D)
                        dec = [l for w, l in L if w == D and "BENCH t decide" in l]
                        st = [plus(l) for w, l in L if w == D and "BENCH t txstart" in l]
                        pk = [l for w, l in L if w == D and "BENCH pk" in l]
                        rel = any("released the preamble hold" in l for l in pk)
                        seen_by_o = any(w == O and "Lora RX" in l and f"fr=0x{nums[D]:08x}" in l for w, l in L)
                        air_us = field(em[0], "air") if em else None
                        f_start = EFOLLOW + (late[0] if late and late[0] is not None else 0)
                        f_end = f_start + (air_us / 1000 if air_us else 0)
                        tx_ms = st[0] / 1000 if st else None
                        overlap = bool(em and tx_ms is not None and tx_ms < f_end)
                        r = dict(block=block, arm=arm, at=at, armed=armed, emitted=bool(em), foreign_ok=wit_ok, dut_trig=trig,
                                 decide_busy=field(dec[0], "busy") if dec else None, dut_tx_ms=tx_ms,
                                 foreign_ms=[round(f_start), round(f_end)] if em else None, overlap=overlap,
                                 pk=[p.split("BENCH pk ")[1][:60] for p in pk], pk_released=rel, obs_heard=seen_by_o)
                        results.append(r)
                        block_rows.append(r)
                        emit("### " + json.dumps(r))
                # health: witness heard foreign frames, DUT heard markers, observer heard the DUT
                ok = (sum(r["foreign_ok"] or r["overlap"] for r in block_rows) >= len(block_rows) // 2 and
                      sum(r["dut_trig"] for r in block_rows) >= len(block_rows) // 2 and
                      any(r["obs_heard"] for r in block_rows))
                emit(f"--- health {block}: {'OK' if ok else 'FAIL'}")
                if ok:
                    break
                for r in block_rows:
                    r["invalid"] = True
finally:
    state["label"] = "restore"
    emit("RESULTS " + json.dumps(results))
    for n in NODES:
        try:
            cmd(n, "!bench reset txgap=0", 1.0)
        except Exception:
            pass
    try:
        cmd("LR", "!bench sync=-1", 1.5)
    except Exception:
        pass
    for i in ifaces.values():
        i.close()
    emit("DONE")
