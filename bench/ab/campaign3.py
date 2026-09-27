"""Round 3 of the LBT campaign (C1d true develop arm, C2s ShortFast rerun with TX-path event ring, TCXO delay A/B).
Based on the overnight driver - see overnight-2026-09-26-campaign.md in the lbt-bench notes.

Cycles: C0 one-offs, C1 foreign-frame stomp (+peek), C2 3-way contention, C3 CAD-exit contradiction, C4 noise sightings,
C5 deaf gaps. Roles rotate by hardware-class ID; arms interleave inside every block; health gate after every block.

usage: campaign.py outdir [hours] [smoke]
"""

import json
import os
import random
import re
import statistics
import sys
import time
import traceback

import meshtastic.serial_interface
from meshtastic.protobuf import config_pb2
from pubsub import pub

OUTDIR = sys.argv[1]
HOURS = float(sys.argv[2]) if len(sys.argv) > 2 else 9.0
END_AT = float(sys.argv[4]) if len(sys.argv) > 4 else None
SMOKE = len(sys.argv) > 3 and sys.argv[3] == "smoke"
os.makedirs(OUTDIR, exist_ok=True)
DEADLINE = END_AT if END_AT else time.time() + HOURS * 3600

NODES = {"TC1": "COM3", "TC2": "COM31", "XT": "COM32", "LR": "COM23"}
SX = ["TC1", "TC2", "XT"]
NOBODY = 0x0BADF00D
FOREIGN = "0x12"

# per preset: C1 foreign frame length, decision points, post-marker wait; C2/C3 frame payload bytes; skew unit (s)
PRESETS = {
    "SHORT_FAST": dict(elen=249, at=[180, 250, 310], wait=2.5, c2len=84, c3len=200, air100=0.085),
    "MEDIUM_FAST": dict(elen=160, at=[300, 420, 540], wait=3.5, c2len=84, c3len=200, air100=0.33),
    "LONG_FAST": dict(elen=48, at=[300, 500, 650], wait=6.0, c2len=24, c3len=None, air100=1.0),
    "SHORT_SLOW": dict(elen=249, at=[250, 380, 480], wait=3.0, c2len=84, c3len=None, air100=0.16),
    "MEDIUM_SLOW": dict(elen=100, at=[250, 420, 600], wait=4.5, c2len=44, c3len=None, air100=0.6),
}

C1_ARMS = {
    "dev": "lbt=rx pre=deadline",
    "hold": "lbt=rx pre=hold",
    "peek": "lbt=rx pre=hold pk=6 pksym=2 pkfree=1",
    "ln": "lbt=default pre=soft",
    "lnhold": "lbt=default pre=hold",
    "lnpeek": "lbt=default pre=hold pk=6 pksym=2 pkfree=1",
    "off": "lbt=off",
}
C1_ROT = [("TC1", "TC2", "XT"), ("TC2", "XT", "TC1"), ("XT", "TC1", "TC2")]
C2_ARMS = {
    "default": "lbt=default",
    "cadrx4": "lbt=cadrx sym=4",  # A/A with default
    "cad4": "lbt=cad sym=4",
    "rx": "lbt=rx",
    "off": "lbt=off",
    "lnpeek": "lbt=default pre=hold pk=6 pksym=2 pkfree=1",
}
C2_TRIPLES = [("TC1", "TC2", "XT"), ("TC1", "TC2", "LR"), ("TC1", "XT", "LR"), ("TC2", "XT", "LR")]
C3_PAIRS = [("TC1", "TC2"), ("TC2", "XT"), ("XT", "TC1")]
C5_CONFIGS = [
    ("develop", "!bench develop txgap=1"),
    ("develop+pr", "!bench develop txgap=1 israrm=2"),
    ("develop+pr+xosc", "!bench develop txgap=1 israrm=2 xosc=1"),
    ("knobs13", "!bench reset txgap=1"),
    ("knobs13+pr", "!bench reset txgap=1 israrm=2"),
]

rec_f = open(os.path.join(OUTDIR, "records.jsonl"), "a", encoding="utf-8")
log_f = open(os.path.join(OUTDIR, "campaign.log"), "a", encoding="utf-8")
raw_f = open(os.path.join(OUTDIR, "nodes.log"), "a", encoding="utf-8")
ifaces, nums, names, lines = {}, {}, {}, []
ctx = {"label": "setup", "pass": 0, "preset": None}
counters = {"busyRx": {}, "standby_aborts": {}, "gps_on": {}}
last_text = {}


def emit(s):
    s = f"{time.strftime('%H:%M:%S')} {s}"
    print(s, flush=True)
    log_f.write(s + "\n")
    log_f.flush()


def record(kind, **kw):
    r = dict(t=time.strftime("%H:%M:%S"), kind=kind, pass_=ctx["pass"], preset=ctx["preset"], **kw)
    rec_f.write(json.dumps(r) + "\n")
    rec_f.flush()
    return r


def on_log(line, interface=None):
    who = names.get(id(interface), "?")
    if "Can not send yet, busyRx" in line:
        counters["busyRx"][who] = counters["busyRx"].get(who, 0) + 1
        return
    if "standby aborts TX" in line:
        counters["standby_aborts"][who] = counters["standby_aborts"].get(who, 0) + 1
    if "GPS power state OFF -> ACTIVE" in line:
        counters["gps_on"][who] = counters["gps_on"].get(who, 0) + 1
    keep = ("BENCH", "Lora RX", "Ignore rx packet", "Started Tx", "Rate limit", "GPS power", "standby aborts", "Set radio")
    if any(k in line for k in keep):
        lines.append((ctx["label"], who, line.strip()))
        raw_f.write(f"{time.strftime('%H:%M:%S')} [{ctx['label']}] {who}: {line.strip()[:240]}\n")
        if len(lines) > 200000:
            del lines[:100000]


pub.subscribe(on_log, "meshtastic.log.line")


def connect(n):
    old = ifaces.get(n)
    if old is not None:
        try:
            old.close()
        except Exception:
            pass
    i = meshtastic.serial_interface.SerialInterface(NODES[n])
    ifaces[n], names[id(i)], nums[n] = i, n, i.myInfo.my_node_num
    return i


def safe(n, fn, *a, **kw):
    for attempt in range(3):
        try:
            return fn(ifaces[n], *a, **kw)
        except Exception as e:
            emit(f"{n}: {type(e).__name__} {e}; reconnecting")
            time.sleep(3)
            try:
                connect(n)
            except Exception as e2:
                emit(f"{n}: reconnect failed {e2}")
                time.sleep(10)
    return None


def cmd(n, text, wait=1.2, expect=()):
    for attempt in range(3):
        gap = 2.5 - (time.time() - last_text.get(n, 0))
        if gap > 0:
            time.sleep(gap)
        mark = len(lines)
        safe(n, lambda i: i.sendText(text, wantAck=False))
        last_text[n] = time.time()
        time.sleep(wait)
        echo = " ".join(l + " " for (_, w, l) in lines[mark:] if w == n and ("BENCH knobs" in l or "BENCH txgap=" in l or "BENCH osc" in l))
        if all(e in echo for e in expect):
            return True
    emit(f"{n}: not confirmed: {text}")
    return False


def frame(n, payload=b""):
    safe(n, lambda i: i.sendData(payload, destinationId=NOBODY, portNum=5, wantAck=False, hopLimit=0))


def field(l, k):
    m = re.search(rf"\b{k}=(-?\d+)", l)
    return int(m.group(1)) if m else None


def plus(l):
    m = re.search(r"\+(\d+)", l)
    return int(m.group(1)) if m else None


def heard(label_prefix, listener, sender, since):
    return [l for (b, w, l) in lines[since:] if w == listener and "Lora RX" in l and f"fr=0x{nums[sender]:08x}" in l]


def set_preset(preset):
    want = getattr(config_pb2.Config.LoRaConfig.ModemPreset, preset)
    for n in NODES:
        def w(i):
            lc = i.localNode.localConfig.lora
            if lc.modem_preset != want or not lc.use_preset:
                lc.use_preset, lc.modem_preset = True, want
                i.localNode.writeConfig("lora")
        safe(n, w)
    ctx["preset"] = preset
    time.sleep(10)
    for n in NODES:
        cmd(n, "!bench reset txgap=1", 1.0)
    emit(f"=== preset {preset}")


def health(block):
    """Each node sends one frame on the home config; every other node must hear at least all but one."""
    ctx["label"] = f"health|{block}"
    # Every node back to plain defaults first: a leftover trig= holds a node's frame, lbt/pre knobs change timing.
    for n in NODES:
        cmd(n, "!bench reset txgap=1 sync=-1 iq=-1", 1.0)
    since = len(lines)
    for n in NODES:
        frame(n)
        time.sleep(PRESETS[ctx["preset"]]["air100"] * 3 + 1.5)
    time.sleep(2)
    silent = []
    for s in NODES:
        got = sum(bool(heard("", l, s, since)) for l in NODES if l != s)
        if got < len(NODES) - 2:
            silent.append(s)
            emit(f"health {block}: {s} heard by {got}/{len(NODES) - 1}")
    ctx["silent"] = silent
    record("health", block=block, ok=not silent, silent=silent, busyRx=dict(counters["busyRx"]))
    return not silent


def gated(block, fn):
    """Run a block; on a failed health gate repeat it once; a second failure reboots the silent nodes."""
    for attempt in range(2):
        if time.time() > DEADLINE:
            return
        try:
            fn(block + ("-retry" if attempt else ""))
        except Exception:
            emit(f"block {block} raised:\n{traceback.format_exc()}")
        if health(block):
            return
        emit(f"--- health FAIL after {block}, attempt {attempt + 1}")
    bad = ctx.get("silent") or []
    for n in bad:
        emit(f"{n}: reboot after repeated health failure")
        safe(n, lambda i: i.localNode.reboot(3))
    time.sleep(25)
    for n in bad:
        try:
            connect(n)
        except Exception as e:
            emit(f"{n}: reconnect after reboot failed {e}")
    set_preset(ctx["preset"])


# ---------------- C1 foreign-frame stomp ----------------
def c1_block(rot, reps=1):
    E, D, O = rot
    P = PRESETS[ctx["preset"]]

    def run(block):
        cmd("LR", f"!bench sync={FOREIGN}", 2.0, expect=("sync=18",))
        for rep in range(reps):
            for at in P["at"]:
                order = list(C1_ARMS)
                random.shuffle(order)
                for arm in order:
                    if time.time() > DEADLINE:
                        return
                    ctx["label"] = lb = f"C1|{block}|{arm}|at{at}"
                    since = len(lines)
                    exp = [f"trig={nums[E]:08x}", f"at={at} "] + [("lbt=rx " if kv == "lbt=rx" else kv) for kv in C1_ARMS[arm].split() if kv.startswith(("lbt=", "pre=", "pkfree="))]
                    armed = cmd(D, f"!bench reset txgap=1 {C1_ARMS[arm]} trig={nums[E]} at={at}", expect=tuple(exp))
                    frame(D)
                    time.sleep(0.6)
                    armed &= cmd(E, f"!bench efollow=150 esync={FOREIGN} elen={P['elen']}", 1.0, expect=("efollow=150", "esync=18"))
                    frame(E)
                    time.sleep(P["wait"])
                    L = [(w, l) for (b, w, l) in lines[since:] if b == lb]
                    em = [l for w, l in L if w == E and "BENCH e emit" in l]
                    late = [field(l, "late") for w, l in L if w == E and "e follow" in l]
                    st = [plus(l) for w, l in L if w == D and "BENCH t txstart" in l]
                    pk = [l.split("BENCH pk ")[1][:70] for w, l in L if w == D and "BENCH pk" in l]
                    air = field(em[0], "air") if em else None
                    f0 = 150 + (late[0] or 0 if late else 0)
                    f1 = f0 + (air / 1000 if air else 0)
                    tx = st[0] / 1000 if st and st[0] is not None and st[0] < 60_000_000 else None
                    record("C1", block=block, emitter=E, dut=D, observer=O, arm=arm, at=at, armed=armed, emitted=bool(em),
                           foreign_ms=[round(f0), round(f1)] if em else None, dut_tx_ms=tx,
                           overlap=bool(em and tx is not None and tx < f1), after_end_ms=(round(tx - f1) if em and tx is not None else None),
                           foreign_ok=any(w == "LR" and "Lora RX" in l for w, l in L),
                           obs_heard=any(w == O and "Lora RX" in l and f"fr=0x{nums[D]:08x}" in l for w, l in L),
                           decide_busy=next((field(l, "busy") for w, l in L if w == D and "t decide" in l), None),
                           pk=pk, pk_released=any("released" in p for p in pk))
        cmd("LR", "!bench sync=-1", 1.5, expect=("sync=-1",))
    return run


# ---------------- C1iq: the foreign frame is inverted IQ on our own sync word ----------------
def c1iq_block(rot, reps=1):
    E, D, O = rot
    P = PRESETS[ctx["preset"]]

    def run(block):
        cmd("LR", "!bench iq=1", 2.0, expect=("iq=1",))
        for rep in range(reps):
            for at in P["at"]:
                order = list(C1_ARMS)
                random.shuffle(order)
                for arm in order:
                    if time.time() > DEADLINE:
                        return
                    ctx["label"] = lb = f"C1iq|{block}|{arm}|at{at}"
                    since = len(lines)
                    exp = [f"trig={nums[E]:08x}", f"at={at} "] + [("lbt=rx " if kv == "lbt=rx" else kv) for kv in C1_ARMS[arm].split() if kv.startswith(("lbt=", "pre=", "pkfree="))]
                    armed = cmd(D, f"!bench reset txgap=1 {C1_ARMS[arm]} trig={nums[E]} at={at}", expect=tuple(exp))
                    frame(D)
                    time.sleep(0.6)
                    armed &= cmd(E, f"!bench efollow=150 esync=own eiq=1 elen={P['elen']}", 1.0, expect=("efollow=150", "esync=-1", "eiq=1"))
                    frame(E)
                    time.sleep(P["wait"])
                    L = [(w, l) for (b, w, l) in lines[since:] if b == lb]
                    em = [l for w, l in L if w == E and "BENCH e emit" in l]
                    late = [field(l, "late") for w, l in L if w == E and "e follow" in l]
                    st = [plus(l) for w, l in L if w == D and "BENCH t txstart" in l]
                    pk = [l.split("BENCH pk ")[1][:70] for w, l in L if w == D and "BENCH pk" in l]
                    air = field(em[0], "air") if em else None
                    f0 = 150 + (late[0] or 0 if late else 0)
                    f1 = f0 + (air / 1000 if air else 0)
                    tx = st[0] / 1000 if st and st[0] is not None and st[0] < 60_000_000 else None
                    record("C1iq", block=block, emitter=E, dut=D, observer=O, arm=arm, at=at, armed=armed, emitted=bool(em),
                           foreign_ms=[round(f0), round(f1)] if em else None, dut_tx_ms=tx,
                           overlap=bool(em and tx is not None and tx < f1), after_end_ms=(round(tx - f1) if em and tx is not None else None),
                           foreign_ok=any(w == "LR" and "Lora RX" in l for w, l in L),
                           obs_heard=any(w == O and "Lora RX" in l and f"fr=0x{nums[D]:08x}" in l for w, l in L),
                           decide_busy=next((field(l, "busy") for w, l in L if w == D and "t decide" in l), None),
                           pk=pk, pk_released=any("released" in p for p in pk))
        cmd("LR", "!bench iq=-1", 1.5)
    return run


# ---------------- C2 3-way contention ----------------
def c2_block(triple, rounds_per_arm):
    senders = list(triple)
    listener = next(n for n in NODES if n not in senders)
    P = PRESETS[ctx["preset"]]

    def run(block):
        for r in range(rounds_per_arm):
            order = list(C2_ARMS)
            random.shuffle(order)
            for arm in order:
                if time.time() > DEADLINE:
                    return
                ctx["label"] = lb = f"C2|{block}|{arm}|r{r}"
                armed = all(cmd(s, f"!bench reset txgap=1 {C2_ARMS[arm]}", 0.8,
                                expect=tuple(("lbt=rx " if kv == "lbt=rx" else kv) for kv in C2_ARMS[arm].split() if kv.startswith(("lbt=", "sym=", "pkfree="))))
                            for s in senders)
                skew = [0, 0.25, 0.5][r % 3] * P["air100"]
                random.shuffle(senders)
                since = len(lines)
                for k, s in enumerate(senders):
                    frame(s, bytes([k]) * P["c2len"])
                    if k < len(senders) - 1:
                        time.sleep(skew)
                time.sleep(P["air100"] * 6 + 2.5)
                got = {}
                for s in senders:
                    by = [l for l in [listener] + [x for x in senders if x != s] if heard("", l, s, since)]
                    got[s] = by
                record("C2", block=block, senders=senders, listener=listener, arm=arm, skew_airtimes=[0, 0.25, 0.5][r % 3], armed=armed,
                       whole=all(bool(v) for v in got.values()), heard_by=got,
                       x_heard=sum(len([x for x in v if x != listener]) for v in got.values()))
    return run


# ---------------- C3 CAD exit contradiction ----------------
def c3_block(pair, rounds_per_cell):
    P = PRESETS[ctx["preset"]]
    listeners = [n for n in NODES if n not in pair]

    def run(block):
        syncs = ["-1", FOREIGN]
        random.shuffle(syncs)
        for sy in syncs:
            for n in NODES:
                cmd(n, f"!bench reset txgap=1 sync={sy} nobackoff=1", 1.5, expect=(f"sync={-1 if sy == '-1' else 18}",))
            for r in range(rounds_per_cell):
                order = ["cad4", "cadrx4"]
                random.shuffle(order)
                for arm in order:
                    if time.time() > DEADLINE:
                        return
                    ctx["label"] = f"C3|{block}|{arm}|sync{sy}|r{r}"
                    knob = "lbt=cad sym=4" if arm == "cad4" else "lbt=cadrx sym=4"
                    armed = all(cmd(s, f"!bench {knob}", 0.8, expect=(knob.split()[0],)) for s in pair)
                    since = len(lines)
                    for s in pair:
                        frame(s, b"\x55" * P["c3len"])
                    time.sleep(P["air100"] * 2.5 * 3 + 2.5)
                    got = {s: [l for l in listeners + [x for x in pair if x != s] if heard("", l, s, since)] for s in pair}
                    record("C3", block=block, pair=list(pair), sync=sy, arm=arm, armed=armed,
                           whole=all(bool(v) for v in got.values()), heard_by=got,
                           x_heard=sum(len([x for x in v if x in pair]) for v in got.values()))
        for n in NODES:
            cmd(n, "!bench reset txgap=1 sync=-1", 1.5)
    return run


# ---------------- C4 noise sightings ----------------
def c4_block(minutes):
    def run(block):
        for n in NODES:
            cmd(n, "!bench reset txgap=1 pk=8 pksym=2 pktrig=pre pkfree=0", 1.0, expect=("pk=8",))
        ctx["label"] = lb = f"C4|{block}"
        since = len(lines)
        end = min(time.time() + minutes * 60, DEADLINE)
        while time.time() < end:
            time.sleep(10)
        per = {}
        for n in NODES:
            ser = [l for (b, w, l) in lines[since:] if w == n and "BENCH pk " in l]
            per[n] = dict(series=len(ser), all_free=sum(1 for l in ser if re.search(r"v=F+ ", l)),
                          any_busy=sum(1 for l in ser if re.search(r"v=\S*B", l)), ends=[l.split("BENCH pk ")[1].split()[0] for l in ser])
        record("C4", block=block, minutes=minutes, per_node={k: {kk: vv for kk, vv in v.items() if kk != "ends"} for k, v in per.items()},
               ends={k: {e: v["ends"].count(e) for e in set(v["ends"])} for k, v in per.items()})
        for n in NODES:
            cmd(n, "!bench reset txgap=1", 1.0)
    return run


# ---------------- C5 deaf gaps ----------------
def c5_block(dut, tx_per):
    checker = "LR" if dut != "LR" else "TC1"
    P = PRESETS[ctx["preset"]]

    def run(block):
        for scen in ("quiet", "hold"):
            for name, c in C5_CONFIGS:
                if time.time() > DEADLINE:
                    return
                ctx["label"] = lb = f"C5|{block}|{name}|{scen}"
                cmd(dut, c + (" txhold=200" if scen == "hold" else ""), 1.5)
                since = len(lines)
                rx_ok = 0
                for k in range(tx_per):
                    frame(dut)
                    time.sleep(P["air100"] + 1.5)
                    m = len(lines)
                    frame(checker)
                    time.sleep(P["air100"] + 1.5)
                    rx_ok += bool(heard("", dut, checker, m))
                g = [l for (b, w, l) in lines[since:] if w == dut and "BENCH txgap id" in l]
                def st(k):
                    v = [field(l, k) for l in g]
                    v = [x for x in v if x is not None and x >= 0]
                    return round(statistics.median(v)) if v else None
                record("C5", block=block, dut=dut, config=name, scen=scen, tx=len(g), adopted=sum(field(l, "adopt") == 1 for l in g),
                       wake=st("wake"), total=st("total"), ready=st("ready"), israrm=st("israrm"), rx_after=f"{rx_ok}/{tx_per}")
        cmd(dut, "!bench reset txgap=1", 1.0)
    return run



# ---------------- C2s / C3s: senders synchronised on the node (trig on one marker frame) ----------------
C2S_ARMS = dict(C2_ARMS, default_nb="lbt=default nobackoff=1", rx_nb="lbt=rx nobackoff=1")
SYNC_AT = 200  # ms after the marker's RX_DONE: past every node's ~115 ms decode of it, so all decide from an idle loop


def trig_frames(senders, marker, ats, payload, knobs, expect_extra=()):
    """Queue a frame on every sender held for the marker (trig=marker at=ats[k]), then have the marker node send."""
    armed = True
    for k, s in enumerate(senders):
        exp = tuple(("lbt=rx " if kv == "lbt=rx" else kv) for kv in knobs.split() if kv.startswith(("lbt=", "sym=", "pkfree=", "nobackoff="))) + (f"trig={nums[marker]:08x}", f"at={ats[k]} ") + expect_extra
        armed &= cmd(s, f"!bench reset txgap=1 {knobs} trig={nums[marker]} at={ats[k]}", 0.9, expect=exp)
    for s in senders:
        frame(s, payload)
    time.sleep(0.8)
    since = len(lines)
    frame(marker)
    return armed, since


def c2s_block(triple, rounds_per_arm):
    senders = list(triple)
    listener = next(n for n in NODES if n not in senders)
    P = PRESETS[ctx["preset"]]

    def run(block):
        for r in range(rounds_per_arm):
            order = list(C2S_ARMS)
            random.shuffle(order)
            for arm in order:
                if time.time() > DEADLINE:
                    return
                ctx["label"] = lb = f"C2s|{block}|{arm}|r{r}"
                sk = [0, 0.25, 0.5][r % 3]
                random.shuffle(senders)
                ats = [SYNC_AT + round(k * sk * P["air100"] * 1000) for k in range(len(senders))]
                armed, since = trig_frames(senders, listener, ats, bytes([r % 250]) * P["c2len"], C2S_ARMS[arm])
                time.sleep(P["air100"] * 6 + 3.0)
                got = {s: [l for l in [listener] + [x for x in senders if x != s] if heard("", l, s, since)] for s in senders}
                dec = {s: [plus(l) for (b, w, l) in lines[since:] if w == s and "BENCH t decide" in l] for s in senders}
                txs = {s: [plus(l) for (b, w, l) in lines[since:] if w == s and "BENCH t txstart" in l] for s in senders}
                record("C2s", block=block, senders=senders, listener=listener, arm=arm, skew_airtimes=sk, ats=ats, armed=armed,
                       triggered=all(dec[s] for s in senders), whole=all(bool(v) for v in got.values()), heard_by=got,
                       x_heard=sum(len([x for x in v if x != listener]) for v in got.values()),
                       decide_us={s: (v[0] if v else None) for s, v in dec.items()}, txstart_us={s: (v[0] if v else None) for s, v in txs.items()})
    return run


def c3s_block(pair, rounds_per_cell):
    P = PRESETS[ctx["preset"]]
    listeners = [n for n in NODES if n not in pair]
    marker = listeners[0]

    def run(block):
        syncs = ["-1", FOREIGN]
        random.shuffle(syncs)
        for sy in syncs:
            for n in NODES:
                cmd(n, f"!bench reset txgap=1 sync={sy}", 1.5, expect=(f"sync={-1 if sy == '-1' else 18}",))
            for r in range(rounds_per_cell):
                order = ["cad4", "cadrx4"]
                random.shuffle(order)
                for arm in order:
                    if time.time() > DEADLINE:
                        return
                    ctx["label"] = f"C3s|{block}|{arm}|sync{sy}|r{r}"
                    knob = ("lbt=cad sym=4" if arm == "cad4" else "lbt=cadrx sym=4") + f" nobackoff=1 sync={sy}"
                    armed, since = trig_frames(list(pair), marker, [SYNC_AT, SYNC_AT], b"\x55" * P["c3len"], knob)
                    time.sleep(P["air100"] * 2.5 * 3 + 3.0)
                    got = {s: [l for l in listeners + [x for x in pair if x != s] if heard("", l, s, since)] for s in pair}
                    txs = {s: [plus(l) for (b, w, l) in lines[since:] if w == s and "BENCH t txstart" in l] for s in pair}
                    record("C3s", block=block, pair=list(pair), marker=marker, sync=sy, arm=arm, armed=armed,
                           whole=all(bool(v) for v in got.values()), heard_by=got,
                           x_heard=sum(len([x for x in v if x in pair]) for v in got.values()),
                           txstart_us={s: (v[0] if v else None) for s, v in txs.items()})
        for n in NODES:
            cmd(n, "!bench reset txgap=1 sync=-1", 1.5)
    return run


# ---------------- round 3 helpers ----------------
def bench(knobs):
    """A knob string starting with 'develop' starts from develop's radio behaviour; anything else from reset."""
    if knobs.startswith("develop"):
        return f"!bench develop txgap=1 {knobs[len('develop'):].strip()}"
    return f"!bench reset txgap=1 {knobs}"


def exp_of(knobs):
    out = []
    for kv in knobs.split():
        if kv == "develop":
            out += ["txarm=late", "xosc=0"]
        elif kv.startswith(("lbt=", "sym=", "pre=", "pkfree=", "nobackoff=", "slot=", "cwmin=", "tcxo=")):
            out.append(kv + " ")
    return tuple(out)


def trig_frames(senders, marker, ats, payload, knobs, expect_extra=()):
    """Queue a frame on every sender held for the marker (trig=marker at=ats[k]), then have the marker node send."""
    armed = True
    for k, s in enumerate(senders):
        exp = exp_of(knobs) + (f"trig={nums[marker]:08x}", f"at={ats[k]} ") + expect_extra
        armed &= cmd(s, f"{bench(knobs)} trig={nums[marker]} at={ats[k]}", 0.9, expect=exp)
    for s in senders:
        frame(s, payload)
    time.sleep(0.8)
    since = len(lines)
    frame(marker)
    return armed, since


def ev_of(L, who):
    v = [l.split("BENCH t ev ")[1] for w, l in L if w == who and "BENCH t ev " in l]
    return v[0] if v else None


def snr_of(l):
    m = re.search(r"rxSNR=(-?[\d.]+)", l)
    return float(m.group(1)) if m else None


# ---------------- K: TCXO start-delay A/B ----------------
TCXO_VALUES = [5000, 2000, 1000, 500, 200]
tcxo_results = []


def tcxo_block(dut, frames_per):
    P = PRESETS[ctx["preset"]]
    marker = "XT"
    listeners = [n for n in NODES if n != dut]

    def run(block):
        for base in random.sample(["reset", "develop"], 2):
            vals = list(TCXO_VALUES) if not SMOKE else [5000, 1000]
            random.shuffle(vals)
            for v in vals:
                if time.time() > DEADLINE:
                    return
                knobs = ("develop " if base == "develop" else "") + f"lbt=cad sym=4 tcxo={v}"
                armed = cmd(dut, f"{bench(knobs)} trig={nums[marker]} at={SYNC_AT}", 1.2,
                            expect=exp_of(knobs) + (f"trig={nums[marker]:08x}",))
                for k in range(frames_per):
                    ctx["label"] = lb = f"K|{block}|{base}|tcxo{v}|f{k}"
                    since = len(lines)
                    frame(dut, bytes([k]) * P["c2len"])
                    time.sleep(0.8)
                    frame(marker)
                    time.sleep(P["air100"] * 2 + 1.5)
                    m = len(lines)
                    frame(marker)  # does the DUT still hear after its TX
                    time.sleep(P["air100"] + 1.5)
                    L = [(w, l) for (b, w, l) in lines[since:] if b == lb]
                    rx = {n: [snr_of(l) for w, l in L if w == n and "Lora RX" in l and f"fr=0x{nums[dut]:08x}" in l] for n in listeners}
                    g = [l for w, l in L if w == dut and "BENCH txgap id" in l]
                    r = record("K", block=block, dut=dut, base=base, tcxo=v, armed=armed,
                               decide=next((plus(l) for w, l in L if w == dut and "t decide" in l), None),
                               decide_busy=next((field(l, "busy") for w, l in L if w == dut and "t decide" in l), None),
                               txstart=next((plus(l) for w, l in L if w == dut and "t txstart" in l), None),
                               txdone=next((plus(l) for w, l in L if w == dut and "t txdone" in l), None),
                               gap_total=field(g[0], "total") if g else None,
                               heard={n: (v2[0] if v2 else None) for n, v2 in rx.items()},
                               rx_after=bool(heard("", dut, marker, m)), ev=ev_of(L, dut),
                               osc=[l for w, l in L if w == dut and "BENCH osc" in l])
                    tcxo_results.append(r)
        cmd(dut, "!bench reset txgap=1", 1.0)
    return run


def choose_tcxo():
    """Smallest of 1000 / 2000 us where every K frame on the TCXO boards reached >= 2 listeners and the median SNR
    per listener stayed within 1.5 dB of the 5000 us rows; 0 (skip the tcxo arms) if neither qualifies."""
    rows = [r for r in tcxo_results if r["armed"]]

    def ok(v):
        these = [r for r in rows if r["tcxo"] == v]
        if not these or any(sum(x is not None for x in r["heard"].values()) < 2 for r in these):
            return False
        for n in NODES:
            a = [r["heard"].get(n) for r in rows if r["tcxo"] == 5000 and r["heard"].get(n) is not None]
            b = [r["heard"].get(n) for r in these if r["heard"].get(n) is not None]
            if a and b and statistics.median(b) < statistics.median(a) - 1.5:
                return False
        return True

    for v in (1000, 2000):
        if ok(v):
            return v
    return 0


# ---------------- C2r: ShortFast contention rerun ----------------
def c2r_arms(t):
    a = {
        "default": "lbt=default",
        "cad4": "lbt=cad sym=4",
        "devtrue": "develop lbt=cad",
        "rx_nb": "lbt=rx nobackoff=1",
        "default_slot25": "lbt=default slot=25",
        "cad4_slot25": "lbt=cad sym=4 slot=25",
        "default_cw5": "lbt=default cwmin=5",
    }
    if t:
        a["default_tcxo"] = f"lbt=default tcxo={t}"
        a["cad4_tcxo"] = f"lbt=cad sym=4 tcxo={t}"
    return a


def c2r_block(triple, rounds_per_arm, arms):
    senders = list(triple)
    listener = next(n for n in NODES if n not in senders)
    P = PRESETS[ctx["preset"]]

    def run(block):
        if listener == "LR":  # XT hears LR's 10 dBm marker at about -114 dBm, marginal at SF7: raise the marker only
            cmd("LR", "!bench pwr=17", 1.5, expect=("pwr=17",))
        for r in range(rounds_per_arm):
            order = list(arms)
            random.shuffle(order)
            for arm in order:
                if time.time() > DEADLINE:
                    return
                ctx["label"] = lb = f"C2r|{block}|{arm}|r{r}"
                sk = [0, 0.25, 0.5][r % 3]
                random.shuffle(senders)
                ats = [SYNC_AT + round(k * sk * P["air100"] * 1000) for k in range(len(senders))]
                armed, since = trig_frames(senders, listener, ats, bytes([r % 250]) * P["c2len"], arms[arm])
                time.sleep(P["air100"] * 6 + 3.0)
                L = [(w, l) for (b, w, l) in lines[since:] if b == lb]
                got = {s: [l for l in [listener] + [x for x in senders if x != s] if heard("", l, s, since)] for s in senders}
                dec = {s: [plus(l) for w, l in L if w == s and "BENCH t decide" in l] for s in senders}
                txs = {s: [plus(l) for w, l in L if w == s and "BENCH t txstart" in l] for s in senders}
                record("C2r", block=block, senders=senders, listener=listener, arm=arm, knobs=arms[arm], skew_airtimes=sk, ats=ats,
                       armed=armed, triggered=all(dec[s] for s in senders), whole=all(bool(v) for v in got.values()), heard_by=got,
                       x_heard=sum(len([x for x in v if x != listener]) for v in got.values()),
                       decide_us={s: (v[0] if v else None) for s, v in dec.items()},
                       txstart_us={s: (v[0] if v else None) for s, v in txs.items()},
                       ev={s: ev_of(L, s) for s in senders})
        if listener == "LR":
            cmd("LR", "!bench pwr=default", 1.5)
    return run


# ---------------- C1d: foreign frames against a true develop arm ----------------
C1D_ARMS = {
    "dev": "lbt=rx pre=deadline",
    "devcad": "lbt=cad pre=deadline",
    "devtrue": "develop lbt=cad pre=deadline",
    "cadhold": "lbt=cad pre=hold",
    "cadpeek": "lbt=cad pre=hold pk=6 pksym=2 pkfree=1",
    "lnpeek": "lbt=default pre=hold pk=6 pksym=2 pkfree=1",
    "off": "lbt=off",
}


def c1d_block(rot, iq=False):
    E, D, O = rot
    P = PRESETS[ctx["preset"]]
    kind = "C1diq" if iq else "C1d"

    def run(block):
        if iq:
            cmd("LR", "!bench iq=1", 2.0, expect=("iq=1",))
        else:
            cmd("LR", f"!bench sync={FOREIGN}", 2.0, expect=("sync=18",))
        for at in (P["at"] if not SMOKE else P["at"][:1]):
            order = list(C1D_ARMS)
            random.shuffle(order)
            for arm in order:
                if time.time() > DEADLINE:
                    return
                ctx["label"] = lb = f"{kind}|{block}|{arm}|at{at}"
                since = len(lines)
                knobs = C1D_ARMS[arm]
                armed = cmd(D, f"{bench(knobs)} trig={nums[E]} at={at}", expect=exp_of(knobs) + (f"trig={nums[E]:08x}", f"at={at} "))
                frame(D)
                time.sleep(0.6)
                if iq:
                    armed &= cmd(E, f"!bench efollow=150 esync=own eiq=1 elen={P['elen']}", 1.0, expect=("efollow=150", "esync=-1", "eiq=1"))
                else:
                    armed &= cmd(E, f"!bench efollow=150 esync={FOREIGN} eiq=0 elen={P['elen']}", 1.0, expect=("efollow=150", "esync=18"))
                frame(E)
                time.sleep(P["wait"])
                L = [(w, l) for (b, w, l) in lines[since:] if b == lb]
                em = [l for w, l in L if w == E and "BENCH e emit" in l]
                late = [field(l, "late") for w, l in L if w == E and "e follow" in l]
                st = [plus(l) for w, l in L if w == D and "BENCH t txstart" in l]
                pk = [l.split("BENCH pk ")[1][:70] for w, l in L if w == D and "BENCH pk" in l]
                air = field(em[0], "air") if em else None
                f0 = 150 + (late[0] or 0 if late else 0)
                f1 = f0 + (air / 1000 if air else 0)
                tx = st[0] / 1000 if st and st[0] is not None and st[0] < 60_000_000 else None
                record(kind, block=block, emitter=E, dut=D, observer=O, arm=arm, at=at, armed=armed, emitted=bool(em),
                       foreign_ms=[round(f0), round(f1)] if em else None, dut_tx_ms=tx,
                       overlap=bool(em and tx is not None and tx < f1), after_end_ms=(round(tx - f1) if em and tx is not None else None),
                       foreign_ok=any(w == "LR" and "Lora RX" in l for w, l in L),
                       obs_heard=any(w == O and "Lora RX" in l and f"fr=0x{nums[D]:08x}" in l for w, l in L),
                       decide_busy=next((field(l, "busy") for w, l in L if w == D and "t decide" in l), None),
                       pk=pk, pk_released=any("released" in p for p in pk), ev=ev_of(L, D))
        cmd("LR", "!bench iq=-1" if iq else "!bench sync=-1", 1.5)
    return run


# ---------------- schedule ----------------
def main():
    for n in NODES:
        connect(n)
        emit(f"{n} {NODES[n]} {nums[n]:08x} fw {ifaces[n].metadata.firmware_version if ifaces[n].metadata else '?'}")
    time.sleep(2)
    s = SMOKE
    ctx["pass"] = 4
    set_preset("SHORT_FAST")
    for d in (("TC1", "TC2") if not s else ("TC1",)):
        gated(f"p4-SHORT_FAST-K-{d}", tcxo_block(d, 1 if s else 4))
    t = choose_tcxo()
    emit(f"TCXO arm for C2r: {t or 'none'}")
    record("K_choice", tcxo=t)
    arms = c2r_arms(t)
    for tri in (C2_TRIPLES if not s else C2_TRIPLES[:1]):
        gated(f"p4-SHORT_FAST-C2r-{''.join(tri)}", c2r_block(tri, 1 if s else 4, arms))
    for rot in (C1_ROT if not s else C1_ROT[:1]):
        gated(f"p4-SHORT_FAST-C1d-{''.join(rot)}", c1d_block(rot))
    gated(f"p4-SHORT_FAST-C1diq-{''.join(C1_ROT[1])}", c1d_block(C1_ROT[1], iq=True))
    emit(f"--- counters after SHORT_FAST: {json.dumps(counters)}")
    if s:
        return
    set_preset("MEDIUM_FAST")
    for rot in C1_ROT:
        gated(f"p4-MEDIUM_FAST-C1d-{''.join(rot)}", c1d_block(rot))
    gated(f"p4-MEDIUM_FAST-C1diq-{''.join(C1_ROT[2])}", c1d_block(C1_ROT[2], iq=True))
    emit(f"--- counters after MEDIUM_FAST: {json.dumps(counters)}")
    set_preset("LONG_FAST")
    for d in ("TC1", "TC2"):
        gated(f"p4-LONG_FAST-K-{d}", tcxo_block(d, 3))
    for rot in C1_ROT:
        gated(f"p4-LONG_FAST-C1d-{''.join(rot)}", c1d_block(rot))
    gated(f"p4-LONG_FAST-C1diq-{''.join(C1_ROT[0])}", c1d_block(C1_ROT[0], iq=True))
    emit(f"--- counters after LONG_FAST: {json.dumps(counters)}")


try:
    main()
except Exception:
    emit(f"campaign raised:\n{traceback.format_exc()}")
finally:
    ctx["label"] = "restore"
    emit(f"counters: {json.dumps(counters)}")
    for n in list(ifaces):
        try:
            cmd(n, "!bench reset txgap=0 sync=-1", 1.0)
        except Exception:
            pass
    for i in ifaces.values():
        try:
            i.close()
        except Exception:
            pass
    emit("CAMPAIGN DONE")
