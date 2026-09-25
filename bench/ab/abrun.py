"""Lean A/B runner: LBT contention, develop vs listening-now, on promicros.

Every step that reboots a node runs in its own process (meshtastic CLI) or with no
process holding the port (UF2 copy), so no serial handle can survive a re-enumeration.
The contention/identify step opens the nodes once, after they have settled, and nothing
reboots while it holds them.

  python abrun.py flash IMAGE.uf2 [node ...]
  python abrun.py provision PRESETKEY [node ...]       PRESETKEY in AIR
  python abrun.py contend PRESETKEY LABEL --pairs N --interval S
  python abrun.py identify --count N
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import string
import subprocess
import sys
import threading
import time
from pathlib import Path

import serial
import serial.tools.list_ports as list_ports

NODES = {  # name -> USB serial number
    "dut": "43C2192F2DFEE099",
    "peer": "69DCAE3411236A3E",
    "witness": "C4BB0930D953D0A3",
    "lr": "06E3F87E62341649",
    "w2": "962A7036E02D0A91",
}
ROLES_FILE = Path(__file__).with_name("roles.json")  # optional override of NODES after identify
if ROLES_FILE.exists():
    NODES.update(json.loads(ROLES_FILE.read_text()))

SENDERS = ("dut", "peer")
WITNESSES = ("witness", "lr", "w2")
OUT = Path(os.environ.get("AB_OUT", Path.home() / "bench-runs" / "ab-lean"))

# key -> (region, preset, channel_num)
AIR = {
    "SF": ("EU_868", "SHORT_FAST", 0),
    "NF": ("EU_N_868", "NARROW_FAST", 3),
    "NS": ("EU_N_868", "NARROW_SLOW", 3),
    "SS": ("EU_868", "SHORT_SLOW", 0),
    "MF": ("EU_868", "MEDIUM_FAST", 0),
    "MS": ("EU_868", "MEDIUM_SLOW", 0),
    "LM": ("EU_868", "LONG_MODERATE", 0),
}
TX_POWER = 10


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def port_of(name: str) -> str | None:
    sn = NODES[name]
    for p in list_ports.comports():
        if (p.serial_number or "").upper() == sn.upper():
            return p.device
    return None


def cli(port: str, *args: str, timeout: float = 90.0) -> tuple[int, str]:
    try:
        r = subprocess.run(["meshtastic", "--port", port, *args], capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or "") if isinstance(e.stdout, str) else "timeout"


def wait_answering(name: str, budget: float = 180.0) -> str:
    t0 = time.time()
    while time.time() - t0 < budget:
        port = port_of(name)
        if port:
            rc, out = cli(port, "--info", timeout=45)
            if rc == 0 and "Owner:" in out:
                return out
        time.sleep(5)
    raise RuntimeError(f"{name} did not answer within {budget:.0f}s")


# -- flash ----------------------------------------------------------------------------

def uf2_drives() -> set[str]:
    return {f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\INFO_UF2.TXT")}


NRFUTIL = [sys.executable, str(Path.home() / ".platformio/packages/tool-adafruit-nrfutil/adafruit-nrfutil.py")]


def flash(name: str, image: Path) -> None:
    """enterDFUMode over the API, then serial DFU with the matching -ota.zip.

    The bootloader serves serial DFU whether or not it also mounts a UF2 volume, so this
    path does not depend on the volume appearing (it did not, on these boards, today).
    """
    pkg = image.with_name(image.stem + "-ota.zip") if image.suffix == ".uf2" else image
    if not pkg.exists():
        raise RuntimeError(f"no OTA package {pkg}")
    port = port_of(name)
    if not port:
        raise RuntimeError(f"{name} not enumerated")
    log(f"{name}: enter DFU via API on {port}")
    cli(port, "--enter-dfu", timeout=40)
    time.sleep(8)
    t0 = time.time()
    while time.time() - t0 < 60 and not port_of(name):
        time.sleep(1)
    dport = port_of(name)
    log(f"{name}: serial DFU {pkg.name} on {dport}")
    r = subprocess.run([*NRFUTIL, "dfu", "serial", "--package", str(pkg), "-p", dport, "-b", "115200",
                        "--singlebank"], capture_output=True, text=True, timeout=300)
    if "Device programmed" not in r.stdout + r.stderr:
        raise RuntimeError(f"{name}: nrfutil failed rc={r.returncode}: {(r.stdout + r.stderr)[-400:]}")
    time.sleep(5)
    out = wait_answering(name)
    ver = re.search(r'"firmwareVersion": "([^"]+)"', out)
    log(f"{name}: answering, firmware {ver.group(1) if ver else '?'} ({time.time()-t0:.0f}s)")


# -- provision ------------------------------------------------------------------------

def enum_value(field: str, name: str) -> int:
    from meshtastic.protobuf import config_pb2
    enums = {
        "lora.region": config_pb2.Config.LoRaConfig.RegionCode,
        "lora.modem_preset": config_pb2.Config.LoRaConfig.ModemPreset,
        "device.role": config_pb2.Config.DeviceConfig.Role,
    }
    return enums[field].Value(name)


def provision(name: str, key: str, tx: bool) -> dict:
    region, preset, ch = AIR[key]
    want = {
        "lora.region": region,
        "lora.modem_preset": preset,
        "lora.use_preset": True,
        "lora.channel_num": ch,
        "lora.tx_enabled": tx,
        "lora.tx_power": TX_POWER,
        "lora.override_duty_cycle": False,
        "device.role": "CLIENT_MUTE",
        "security.debug_log_api_enabled": True,
    }
    port = port_of(name)
    for attempt in range(3):
        cur = read_config(name, list(want))
        diff = {k: v for k, v in want.items() if not same(k, cur.get(k), v)}
        if not diff:
            log(f"{name}: {key} settled (tx={tx})")
            return cur
        log(f"{name}: setting {diff}")
        args = []
        for k, v in diff.items():
            args += ["--set", k, str(v).lower() if isinstance(v, bool) else str(v)]
        # region first on its own: other lora writes are refused while region is UNSET
        if "lora.region" in diff and cur.get("lora.region") in ("0", None):
            rc, out = cli(port_of(name), "--set", "lora.region", region)
            wait_answering(name)
        rc, out = cli(port_of(name), *args)
        if rc != 0:
            log(f"{name}: set failed rc={rc}: {out[-300:]}")
        time.sleep(8)  # config commits reboot the node; let it leave before we look for it
        wait_answering(name)
    raise RuntimeError(f"{name}: config would not settle to {key}")


def read_config(name: str, keys: list[str]) -> dict:
    args = []
    for k in keys:
        args += ["--get", k]
    rc, out = cli(port_of(name) or "", *args)
    got = {}
    for line in out.splitlines():
        m = re.match(r"^([a-z_]+\.[a-z_0-9]+): (.*)$", line.strip())
        if m:
            got[m.group(1)] = m.group(2).strip()
    return got


def same(key: str, have: str | None, want) -> bool:
    if have is None:
        return False
    if isinstance(want, bool):
        return have.lower() == str(want).lower()
    if key in ("lora.region", "lora.modem_preset", "device.role"):
        return have == str(enum_value(key, want)) or have == want
    return have == str(want)


# -- contention / identify ------------------------------------------------------------

class Rig:
    """Holds all four nodes for one measurement. Nothing reboots while it is open."""

    def __init__(self, names, record: Path):
        import meshtastic.serial_interface as si
        from pubsub import pub

        self.rec = open(record, "a", encoding="utf-8")
        self.lock = threading.Lock()
        self.ifaces = {}
        self.name_of = {}
        self.last_offset = {}
        self.dead = set()
        self.rx_texts = []  # (node, text) of every decoded frame, for in-run health checks
        pub.subscribe(self.on_receive, "meshtastic.receive")
        pub.subscribe(self.on_log, "meshtastic.log.line")
        for n in names:
            port = port_of(n)
            iface = si.SerialInterface(devPath=port)
            self.ifaces[n] = iface
            self.name_of[id(iface)] = n
            self.write({"kind": "open", "node": n, "port": port,
                        "num": iface.myInfo.my_node_num if iface.myInfo else None,
                        "fw": getattr(iface.metadata, "firmware_version", None)})

    def write(self, d):
        d["ts"] = time.time()
        with self.lock:
            if self.rec.closed:
                return
            self.rec.write(json.dumps(d) + "\n")
            self.rec.flush()

    def on_log(self, line, interface):
        n = self.name_of.get(id(interface))
        m = re.search(r"Corrected frequency offset: (-?[\d.]+)", line)
        if m:
            self.last_offset[n] = float(m.group(1))
        if n and ("frequency offset" in line or "CAD" in line or "busy" in line.lower() or "BENCH" in line
                  or "RSSI look" in line):
            self.write({"kind": "log", "node": n, "line": line[:200]})

    def on_receive(self, packet, interface):
        n = self.name_of.get(id(interface))
        dec = packet.get("decoded", {})
        text = dec.get("text") or (dec["payload"].decode("utf-8", "replace")
                                   if dec.get("portnum") == "PRIVATE_APP" and isinstance(dec.get("payload"), bytes) else None)
        if text:
            self.rx_texts.append((n, text))
        self.write({
            "kind": "rx", "node": n, "from": packet.get("from"), "id": packet.get("id"),
            "portnum": dec.get("portnum"),
            "text": dec.get("text") or (dec["payload"].decode("utf-8", "replace")
                                        if dec.get("portnum") == "PRIVATE_APP" and isinstance(dec.get("payload"), bytes) else None),
            "snr": packet.get("rxSnr"), "rssi": packet.get("rxRssi"),
            "offset": self.last_offset.pop(n, None),
        })

    def send(self, n: str, text: str, timeout: float = 10.0, port: str | None = None) -> bool:
        """sendText with a bound. The library blocks forever on a node whose API stopped answering;
        such a node is declared dead, logged, and skipped from then on rather than stalling the run."""
        if n in self.dead:
            return False
        done = threading.Event()

        def go():
            try:
                if port == "private" and not text.startswith("!bench"):
                    from meshtastic.protobuf import portnums_pb2
                    self.ifaces[n].sendData(text.encode(), portNum=portnums_pb2.PortNum.PRIVATE_APP)
                else:
                    self.ifaces[n].sendText(text)
            finally:
                done.set()

        threading.Thread(target=go, daemon=True).start()
        if done.wait(timeout):
            return True
        self.dead.add(n)
        self.write({"kind": "dead", "node": n, "while": text[:60]})
        log(f"{n}: API not answering within {timeout:.0f}s - declared dead, skipped from now on")
        return False

    def send_pair(self, names, text_of, timeout=15.0):
        self.send_round({n: 0.0 for n in names}, text_of, timeout=timeout)

    def send_round(self, offsets: dict, text_of, burst: int = 1, timeout=15.0, port: str | None = None):
        """Release every sender from one barrier; sender n then waits offsets[n] seconds and queues
        `burst` messages back to back. text_of(n) or text_of(n, b) names each message."""
        barrier = threading.Barrier(len(offsets))

        def go(n):
            barrier.wait(timeout)
            if offsets[n] > 0:
                time.sleep(offsets[n])
            for b in range(burst):
                t = text_of(n, b) if burst > 1 else text_of(n)
                if self.send(n, t, port=port):
                    self.write({"kind": "tx", "node": n, "text": t})

        ts = [threading.Thread(target=go, args=(n,), daemon=True) for n in offsets]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout)

    def close(self):
        from pubsub import pub
        for topic, fn in (("meshtastic.receive", self.on_receive), ("meshtastic.log.line", self.on_log)):
            try:
                pub.unsubscribe(fn, topic)
            except Exception:  # noqa: BLE001
                pass
        # the library's close() can block forever on a node whose API died; bound it
        def shut(iface):
            try:
                iface.close()
            except Exception:  # noqa: BLE001
                pass
        ts = [threading.Thread(target=shut, args=(i,), daemon=True) for i in self.ifaces.values()]
        for t in ts:
            t.start()
        for t in ts:
            t.join(8)
        with self.lock:
            self.rec.close()


def contend(key: str, label: str, pairs: int, interval: float, settle: float = 20.0, size=None) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    rec = OUT / f"C-{key}-{label}-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    rig = Rig(SENDERS + WITNESSES, rec)
    try:
        rig.write({"kind": "row", "key": key, "label": label, "pairs": pairs, "interval": interval,
                   "air": AIR[key], "senders": SENDERS, "witnesses": WITNESSES, "nodes": NODES})
        log(f"settling {settle:.0f}s")
        time.sleep(settle)
        for i in range(pairs):
            rig.send_pair(SENDERS, lambda n: padded(f"C-{key}-{label}-{n}-{i}", size))
            if i % 10 == 9:
                log(f"  {i+1}/{pairs} pairs sent")
            time.sleep(interval)
        log("tail 15s")
        time.sleep(15)
    finally:
        rig.close()
    return rec


RADIO_KNOBS = ("sync", "iq")  # every node must follow these, or the witnesses go deaf
HEADER_B, DATA_OVERHEAD_B, MAX_ONAIR_B = 16, 6, 249


def airtime_s(key: str, onair_b: int) -> float:
    import math
    sf, bw, cr = {"SF": (7, 250, 5), "NF": (7, 62.5, 6), "NS": (8, 62.5, 6), "SS": (8, 250, 5),
                  "MF": (9, 250, 5), "MS": (10, 250, 5), "LM": (11, 125, 8)}[key]
    ts = 2 ** sf / (bw * 1e3)
    de = 1 if ts > 0.016 else 0
    n = 8 + max(math.ceil((8 * onair_b - 4 * sf + 44) / (4 * (sf - 2 * de))) * cr, 0)
    return (16 + 4.25 + n) * ts


def padded(base: str, size) -> str:
    """base, padded with '-xxx..' so the frame is about `size` bytes on air (header + Data + text)."""
    if not size:
        return base
    pad = min(size, MAX_ONAIR_B) - HEADER_B - DATA_OVERHEAD_B - len(base) - 1
    return base + ("-" + "x" * pad if pad > 0 else "")


def split_knobs(knobs: str):
    toks = knobs.split()
    return (" ".join(t for t in toks if t.split("=")[0] in RADIO_KNOBS),
            " ".join(t for t in toks if t.split("=")[0] not in RADIO_KNOBS))


def sweep(key: str, variants: list, pairs: int, interval: float, settle: float = 15.0) -> Path:
    """One Rig session, many variants. Requires the BENCH_KNOBS image.

    A variant is [label, knobs] or an object:
      label       name used in every frame's text (no '-')
      knobs       "!bench" settings for the counted senders; its sync/iq part goes to every node
      node_knobs  {node: extra settings} applied after that, per node (an interferer's sync, say)
      senders     counted senders (default dut, peer); everything else listens unless an interferer
      interferers nodes that also send each round but are not counted (text "J-...")
      size        approx bytes on air (default: unpadded, ~38)
      skew        stagger between successive senders, in airtimes of this frame size (default 0)
      burst       messages each sender queues per round (default 1)
      pairs, interval  per-variant overrides
      rotate      true: each round, the send order (senders + interferers) rotates by one place
      skews       list of skews (airtimes) cycled per round, in place of skew
    AB_DEADLINE=HH:MM: start no new variant after that time (the next such time after launch).
    Every node gets "!bench reset" first, so variants never inherit each other.
    """
    everyone = SENDERS + WITNESSES
    # Nodes on a non-knob image: they listen, but a "!bench" text would be transmitted as a message.
    plain = {n for n in os.environ.get("AB_PLAIN", "").split(",") if n}
    global_knobs = os.environ.get("AB_GLOBAL", "").strip()
    OUT.mkdir(parents=True, exist_ok=True)
    rec = OUT / f"S-{key}-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    rig = Rig(everyone, rec)
    try:
        rig.write({"kind": "row", "key": key, "label": "sweep", "pairs": pairs, "interval": interval,
                   "air": AIR[key], "nodes": NODES, "variants": variants})
        time.sleep(settle)
        deadline = None
        dl_file = Path(__file__).with_name("deadline.txt")  # overrides AB_DEADLINE for chains already running
        if dl_file.exists():
            os.environ["AB_DEADLINE"] = dl_file.read_text().strip()
        if os.environ.get("AB_DEADLINE"):
            hh, mm = map(int, os.environ["AB_DEADLINE"].split(":"))
            now = time.localtime()
            deadline = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, hh, mm, 0, 0, 0, -1))
            if deadline < time.time():
                deadline += 86400
        done_path = Path(os.environ["AB_DONE"]) if os.environ.get("AB_DONE") else None
        done = set(done_path.read_text().split()) if done_path and done_path.exists() else set()
        for v in variants:
            if (v["label"] if isinstance(v, dict) else v[0]) in done:
                continue
            if deadline and time.time() > deadline:
                log(f"deadline {os.environ['AB_DEADLINE']} passed; skipping the remaining variants")
                break
            v = {"label": v[0], "knobs": v[1]} if isinstance(v, (list, tuple)) else dict(v)
            label, knobs = v["label"], v.get("knobs", "")
            senders = list(v.get("senders", SENDERS))
            interferers = list(v.get("interferers", []))
            size, burst = v.get("size"), int(v.get("burst", 1))
            n_pairs, gap = int(v.get("pairs", pairs)), float(v.get("interval", interval))
            radio, lbt = split_knobs(knobs)
            for n in everyone:
                if n in plain:
                    continue
                parts = ["!bench reset", global_knobs, radio, lbt if n in senders else "",
                         v.get("node_knobs", {}).get(n, "")]
                cmd = " ".join(x for x in parts if x)
                if rig.send(n, cmd):
                    rig.write({"kind": "knob", "node": n, "label": label, "cmd": cmd})
            frame = HEADER_B + DATA_OVERHEAD_B + len(padded(f"C-{key}-{label}-peer-{n_pairs}", size))
            skew_s = float(v.get("skew", 0)) * airtime_s(key, frame)
            order = senders + interferers
            offsets = {n: j * skew_s for j, n in enumerate(order)}
            jammer, jam_ms = v.get("jammer"), int(v.get("jam_ms", 0))
            if jammer and jam_ms:
                offsets[jammer] = float(v.get("jam_at", 0.5)) * airtime_s(key, frame)
            rig.write({"kind": "variant", **v, "frame_b": frame, "skew_s": skew_s, "offsets": offsets})
            log(f"variant {label}: senders={senders} interferers={interferers} jammer={jammer}:{jam_ms}ms ~{frame}B "
                f"skew={skew_s * 1000:.0f}ms burst={burst} knobs='{knobs}' node_knobs={v.get('node_knobs', {})}")
            time.sleep(4)
            skews, rotate = v.get("skews"), bool(v.get("rotate"))
            for i in range(n_pairs):
                if skews or rotate:
                    sk = float(skews[i % len(skews)]) * airtime_s(key, frame) if skews else skew_s
                    r = i % len(order) if rotate else 0
                    offsets = {n: j * sk for j, n in enumerate(order[r:] + order[:r])}
                    rig.write({"kind": "sround", "label": label, "i": i, "offsets": offsets})
                def text_of(n, b=0, i=i):
                    if n == jammer and jam_ms:
                        return f"!bench jam={jam_ms}"
                    tag = "J" if n in interferers else "C"
                    return padded(f"{tag}-{key}-{label}-{n}-{i * burst + b}", size)
                rig.send_round(offsets, text_of, burst=burst, port=v.get("port"))
                time.sleep(gap)
            time.sleep(gap * 2)
            if done_path:
                # a sender nobody heard, that itself heard nothing, has a stuck radio: stop so the caller can heal it
                silent = [s for s in senders if s not in plain and n_pairs >= 6
                          and not any(t.startswith(f"C-{key}-{label}-{s}-") and n != s for n, t in rig.rx_texts)
                          and not any(n == s and f"-{label}-" in t for n, t in rig.rx_texts)]
                if silent:
                    rig.write({"kind": "silent", "label": label, "nodes": silent})
                    log(f"variant {label}: {silent} deaf and mute; stopping for a health check")
                    raise SystemExit(3)
                with open(done_path, "a") as fh:
                    fh.write(label + chr(10))
        for n in everyone:
            if n not in plain:
                rig.send(n, "!bench reset")
        time.sleep(3)
    finally:
        rig.close()
    return rec


def health(names=None, heal=True) -> list:
    """Each node sends two short frames in turn; a node that nobody hears and that hears nobody has a stuck radio
    (seen: peer, 22:53, logging busyRx forever) and is rebooted over the API, then everything is re-checked once.
    Knobs are reset first so a sweep that died mid-variant cannot leave a node on another syncword or IQ."""
    names = list(names or NODES)
    OUT.mkdir(parents=True, exist_ok=True)
    rig = Rig(names, OUT / f"H-{time.strftime('%Y%m%d-%H%M%S')}.jsonl")
    try:
        for n in names:
            rig.send(n, "!bench reset")
        time.sleep(4)
        for n in names:
            for k in range(2):
                rig.send(n, f"H-{n}-{k}", port="private")
                time.sleep(1.5)
        time.sleep(4)
        heard = {n: sorted({m for m, t in rig.rx_texts if t.startswith(f"H-{n}-") and m != n}) for n in names}
        hears = {n: sorted({t.split("-")[1] for m, t in rig.rx_texts if m == n and t.startswith("H-")}) for n in names}
        dead = set(rig.dead)
        rig.write({"kind": "health", "heard_by": heard, "hears": hears, "dead": sorted(dead)})
    finally:
        rig.close()
    bad = [n for n in names if n in dead or (not heard[n] and not hears[n])]
    for n in names:
        log(f"health {n}: heard by {heard[n] or '-'}, hears {hears[n] or '-'}{' DEAD API' if n in dead else ''}")
    if bad and heal:
        for n in bad:
            log(f"health: rebooting {n}")
            cli(port_of(n) or "", "--reboot", timeout=40)
        time.sleep(20)
        for n in bad:
            try:
                wait_answering(n)
            except RuntimeError as e:
                log(f"health: {e}")
        return health(names, heal=False)
    if bad:
        log(f"health: still bad after reboot: {bad}")
    return bad


POOL = ("dut", "peer", "w2")  # SX1262 knob nodes that rotate through talker/joiner roles


def late_sweep(key: str, variants: list, settle: float = 15.0) -> Path:
    """Late joiners: a talker starts a frame; joiners that could not hear its start decide at a known moment.

    A variant is an object:
      label     name used in frame text (no '-')
      knobs     "!bench" LBT settings for the joiners (the talker always sends promptly: nobackoff=1)
      ways      2 (one joiner) or 3 (two joiners); roles rotate over POOL every round
      mode      "deaf": each joiner goes deaf just before the talker sends and wakes at a swept point in its
                        frame (deaf_fracs, fractions of the frame, plus lead_ms for the talker's start latency)
                "dc":   joiners run a sleep/wake cycle (dc_knobs, e.g. "dcsleep=40 dcwake=40" or "rxdc=4/4") and
                        queue their frame at a random point in the talker's frame; they decide when next awake
      size, pairs, interval
    Every frame goes on PRIVATE_APP, so PhoneAPI's one-text-per-2-s limit never drops one. The analysis dates each
    decision from the joiner's "BENCH undeaf" line (or its send, under rxdc) against the talker's frame at the
    witnesses, so timing jitter on the host only moves a round between bins.
    """
    import random
    everyone = SENDERS + WITNESSES
    global_knobs = os.environ.get("AB_GLOBAL", "").strip()
    OUT.mkdir(parents=True, exist_ok=True)
    rec = OUT / f"L-{key}-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    rig = Rig(everyone, rec)
    rnd = random.Random(1)
    try:
        rig.write({"kind": "row", "key": key, "label": "late", "air": AIR[key], "nodes": NODES, "variants": variants})
        time.sleep(settle)
        for v in variants:
            label, knobs, ways = v["label"], v.get("knobs", ""), int(v.get("ways", 2))
            mode, size = v.get("mode", "deaf"), v.get("size", 249)
            n_rounds, gap = int(v.get("pairs", 40)), float(v.get("interval", 3.0))
            frame = HEADER_B + DATA_OVERHEAD_B + len(padded(f"C-{key}-{label}-peer-{n_rounds}", size))
            air = airtime_s(key, frame)
            fracs = v.get("deaf_fracs", [0.1, 0.25, 0.4, 0.55, 0.7, 0.85, 1.0, 1.2])
            lead = float(v.get("lead_ms", 30)) / 1000.0
            log(f"late {label}: ways={ways} mode={mode} ~{frame}B air={air * 1000:.0f}ms knobs='{knobs}' {v.get('dc_knobs', '')}")
            for i in range(n_rounds):
                order = POOL[i % 3:] + POOL[:i % 3]
                talker, joiners = order[0], list(order[1:ways])
                # per-round roles: joiners get the LBT knobs (plus the duty cycle), the talker just sends promptly
                for n in everyone:
                    if n == talker:
                        cmd = f"!bench reset {global_knobs} nobackoff=1"
                    elif n in joiners:
                        cmd = f"!bench reset {global_knobs} nobackoff=1 {knobs} {v.get('dc_knobs', '') if mode == 'dc' else ''}"
                    else:
                        cmd = f"!bench reset {global_knobs}"
                    rig.send(n, " ".join(cmd.split()))
                time.sleep(2.1)  # PhoneAPI text limit: the next text to each node must be >2 s later
                rig.write({"kind": "round", "label": label, "i": i, "talker": talker, "joiners": joiners, "air_s": air})
                barrier = threading.Barrier(1 + len(joiners))

                def go_talker():
                    barrier.wait(10)
                    time.sleep(0.015)
                    t = padded(f"C-{key}-{label}-{talker}-{i}", size)
                    if rig.send(talker, t, port="private"):
                        rig.write({"kind": "tx", "node": talker, "text": t, "role": "talker"})

                def go_joiner(j, k):
                    barrier.wait(10)
                    t = padded(f"C-{key}-{label}-{j}-{i}", size)
                    if mode == "deaf":
                        frac = fracs[(i // 3 + 3 * k) % len(fracs)]  # joiners in one round wake at different points
                        d_ms = int((lead + frac * air) * 1000)
                        rig.send(j, f"!bench deaf={d_ms}")
                        rig.write({"kind": "deaf", "node": j, "i": i, "label": label, "ms": d_ms, "frac_target": frac})
                        time.sleep(0.03)
                    else:
                        time.sleep(0.015 + lead + rnd.uniform(0.0, 1.0) * air)
                    if rig.send(j, t, port="private"):
                        rig.write({"kind": "tx", "node": j, "text": t, "role": "joiner"})

                ts = [threading.Thread(target=go_talker, daemon=True)]
                ts += [threading.Thread(target=go_joiner, args=(j, k), daemon=True) for k, j in enumerate(joiners)]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join(15)
                time.sleep(gap)
        for n in everyone:
            rig.send(n, "!bench reset")
        time.sleep(3)
    finally:
        rig.close()
    return rec


def blast_sweep(key: str, phases: list, settle: float = 10.0) -> Path:
    """Timed phases with one node transmitting back to back while the rest of the mesh measures.

    A phase is an object:
      label         name (no '-'), written as a 'variant' record so noise/probe analysis groups by it
      dur_s         how long the phase lasts
      blaster       node that transmits continuously, or omit for a quiet phase
      blaster_knobs its "!bench" settings (e.g. "iq=1 lbt=off nobackoff=1"); its queue is fed every feed_s
      mesh_knobs    settings for every other node (e.g. "probe=250 lbt=cad sym=4 rssim=6")
      pair_every    dut and peer each send one frame this often, to measure delivery under the blast
      size          blaster frame size (default 249)
    Frames go on PRIVATE_APP so PhoneAPI's text limit cannot pace the blaster.
    """
    everyone = SENDERS + WITNESSES
    global_knobs = os.environ.get("AB_GLOBAL", "").strip()
    OUT.mkdir(parents=True, exist_ok=True)
    rec = OUT / f"B-{key}-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    rig = Rig(everyone, rec)
    try:
        rig.write({"kind": "row", "key": key, "label": "blast", "air": AIR[key], "nodes": NODES, "phases": phases})
        time.sleep(settle)
        for ph in phases:
            label, blaster = ph["label"], ph.get("blaster")
            for n in everyone:
                extra = ph.get("blaster_knobs", "") if n == blaster else ph.get("mesh_knobs", "")
                rig.send(n, " ".join(f"!bench reset {global_knobs} {extra}".split()))
            time.sleep(2.1)
            rig.write({"kind": "variant", "label": label, **ph})
            log(f"blast phase {label}: {ph.get('dur_s')}s blaster={blaster} '{ph.get('blaster_knobs', '')}' "
                f"mesh='{ph.get('mesh_knobs', '')}'")
            t_end = time.time() + float(ph.get("dur_s", 30))
            feed, pair_every = float(ph.get("feed_s", 0.15)), float(ph.get("pair_every", 3.0))
            next_feed, next_pair, i, k = time.time(), time.time() + 1.0, 0, 0
            while time.time() < t_end:
                now = time.time()
                if blaster and now >= next_feed:
                    rig.send(blaster, padded(f"J-{key}-{label}-{blaster}-{k}", ph.get("size", 249)), port="private")
                    k += 1
                    next_feed = now + feed
                if now >= next_pair:
                    for sdr in ("dut", "peer"):
                        t = padded(f"C-{key}-{label}-{sdr}-{i}", 100)
                        if rig.send(sdr, t, port="private"):
                            rig.write({"kind": "tx", "node": sdr, "text": t})
                    i += 1
                    next_pair = now + pair_every
                time.sleep(0.01)
            rig.write({"kind": "phase_end", "label": label, "blaster_frames_queued": k})
        for n in everyone:
            rig.send(n, "!bench reset")
        time.sleep(3)
    finally:
        rig.close()
    return rec


def identify(count: int, interval: float) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    rec = OUT / f"I1-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    names = SENDERS + WITNESSES
    rig = Rig(names, rec)
    try:
        time.sleep(15)
        for i in range(count):
            for n in names:
                rig.ifaces[n].sendText(f"I1-{n}-{i}")
                rig.write({"kind": "tx", "node": n, "text": f"I1-{n}-{i}"})
                time.sleep(interval)
        time.sleep(10)
    finally:
        rig.close()
    return rec


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("flash"); f.add_argument("image"); f.add_argument("nodes", nargs="*")
    p = sub.add_parser("provision"); p.add_argument("key"); p.add_argument("nodes", nargs="*")
    p.add_argument("--all-tx", action="store_true")
    c = sub.add_parser("contend"); c.add_argument("key"); c.add_argument("label")
    c.add_argument("--pairs", type=int, default=60); c.add_argument("--interval", type=float, default=4.0)
    c.add_argument("--size", type=int, default=None)
    w = sub.add_parser("sweep"); w.add_argument("key"); w.add_argument("variants", help="JSON file: [[label, knobs], ...]")
    w.add_argument("--pairs", type=int, default=30); w.add_argument("--interval", type=float, default=3.0)
    lt = sub.add_parser("late"); lt.add_argument("key"); lt.add_argument("variants", help="JSON file of late variants")
    bl = sub.add_parser("blast"); bl.add_argument("key"); bl.add_argument("phases", help="JSON file of blast phases")
    sub.add_parser("health")
    i = sub.add_parser("identify"); i.add_argument("--count", type=int, default=6)
    i.add_argument("--interval", type=float, default=2.5)
    a = ap.parse_args()
    if a.cmd == "flash":
        for n in a.nodes or list(NODES):
            flash(n, Path(a.image))
    elif a.cmd == "provision":
        for n in a.nodes or list(NODES):
            provision(n, a.key, tx=a.all_tx or n in SENDERS)
    elif a.cmd == "contend":
        print(contend(a.key, a.label, a.pairs, a.interval, size=a.size))
    elif a.cmd == "sweep":
        print(sweep(a.key, json.loads(Path(a.variants).read_text()), a.pairs, a.interval))
    elif a.cmd == "late":
        print(late_sweep(a.key, json.loads(Path(a.variants).read_text())))
    elif a.cmd == "blast":
        print(blast_sweep(a.key, json.loads(Path(a.phases).read_text())))
    elif a.cmd == "health":
        print(health())
    elif a.cmd == "identify":
        print(identify(a.count, a.interval))


if __name__ == "__main__":
    # exit hard: a dead node's reader thread can otherwise keep the process alive after main() returns or raises
    code = 0
    try:
        main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except BaseException:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        code = 1
    sys.stdout.flush(); sys.stderr.flush()
    os._exit(code)
