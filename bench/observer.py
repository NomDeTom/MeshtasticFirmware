"""Continuous capture: one held interface per node, for the whole session.

This is the component the whole bench rests on. Windowed capture - opening a connection
around a test and closing it after - is the worst available policy, because the lines
that matter most are the ones nobody scheduled: boot-time single-shot validator
warnings, crashes between scenarios, late retransmits, and the slow drift only visible
across cycles. Worse, with a window, silence and inattention are indistinguishable, so
every NOT OBSERVED verdict becomes worthless.

Two capture paths, chosen by role:

  API path (dut, peer, exciter). A held SerialInterface. Logs arrive as LogRecord over
  the same link used for commands, so a node can be captured and commanded at once.
  Requires security.debug_log_api_enabled, which a factory reset wipes - the
  provisioner re-applies it.

  Raw serial path (observer). A node that is NEVER commanded keeps emitting plain-text
  logs forever, because the thing that silences them - the first protobuf API touch of
  the port - never happens. That makes raw serial exactly the right instrument here,
  and the only place it is right. It is also the only way to watch a node without
  perturbing what it reports: an attached client changes how received packets are
  logged, so an API-attached "observer" is not observing the same thing.

Reconnection is explicit rather than silent. A node vanishes from USB during DFU and may
return on a different port, so we reconnect by USB serial and write a gap event with its
duration. An unrecorded gap is a silent hole in the evidence.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import devices, packets, streams

# CSI escapes the firmware uses to colour its log prefix.
_ANSI_RE = re.compile(chr(27) + r'\[[0-9;]*[A-Za-z]')

# Spacing between reconnect attempts, and how many to make before giving up. A node
# that is mid-DFU cannot answer, and hammering it is the most likely way to lose it.
RECONNECT_SPACING_S = 5.0
RECONNECT_MAX_ATTEMPTS = 30

# iface.close() sends a disconnect and can block forever if the device just rebooted -
# the library waits on TX-queue space that never frees. Close on a daemon thread and
# abandon it; the OS reclaims the handle when the device re-enumerates or we exit.
CLOSE_TIMEOUT_S = 5.0

CONNECT_TIMEOUT_S = 20.0


@dataclass
class Held:
    """One node's live capture state."""

    node: devices.BenchNode
    port: str | None = None
    iface: Any = None
    serial_reader: Any = None
    connected: bool = False
    attempts: int = 0
    last_attempt: float = 0.0
    dropped_at: float | None = None
    packets_seen: int = 0
    log_lines: int = 0

    @property
    def raw_mode(self) -> bool:
        return self.node.never_command


class Observer:
    """Holds every node open and fans their traffic into the recorder."""

    def __init__(self, recorder: streams.Recorder, nodes: list[devices.BenchNode]) -> None:
        self.recorder = recorder
        self.held: dict[str, Held] = {n.name: Held(node=n) for n in nodes}
        self._lock = threading.RLock()
        self._wired = False
        self._stop = threading.Event()
        self._health: threading.Thread | None = None

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> dict:
        """Open every node. Returns a per-node opened/failed report."""
        self._wire_pubsub()
        report: dict[str, Any] = {}
        for name, held in self.held.items():
            ok, detail = self._open(held)
            report[name] = {"opened": ok, "detail": detail, "mode": "raw" if held.raw_mode else "api"}
        self._stop.clear()
        self._health = threading.Thread(target=self._health_loop, daemon=True, name="bench-health")
        self._health.start()
        self.recorder.event("observer_start", nodes=report)
        return report

    def stop(self) -> dict:
        self._stop.set()
        if self._health is not None:
            self._health.join(timeout=2.0)
        summary = self.status()
        for held in self.held.values():
            self._close(held, reason="observer_stop")
        self._unwire_pubsub()
        self.recorder.event("observer_stop", summary=summary)
        return summary

    def __enter__(self) -> Observer:
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # -- opening / closing -----------------------------------------------------

    def _open(self, held: Held) -> tuple[bool, str]:
        port = devices.try_resolve_port(held.node.serial_number)
        if port is None:
            return False, "not enumerated"
        held.port = port
        try:
            if held.raw_mode:
                held.serial_reader = _RawSerialReader(held, self.recorder)
                held.serial_reader.start()
            else:
                held.iface = self._connect_api(port)
            held.connected = True
            held.attempts = 0
            if held.dropped_at is not None:
                gap = round(time.time() - held.dropped_at, 1)
                self.recorder.event(
                    "capture_gap_closed", node=held.node.name, port=port, gap_s=gap
                )
                held.dropped_at = None
            self.recorder.event(
                "connection_established",
                node=held.node.name,
                port=port,
                mode="raw" if held.raw_mode else "api",
            )
            return True, port
        except Exception as exc:  # noqa: BLE001 - any open failure is just "not open"
            held.connected = False
            self.recorder.event(
                "connection_failed", node=held.node.name, port=port, error=str(exc)
            )
            return False, f"{type(exc).__name__}: {exc}"

    def _connect_api(self, port: str) -> Any:
        import meshtastic.serial_interface as si

        result: dict[str, Any] = {}

        def _do() -> None:
            try:
                result["iface"] = si.SerialInterface(devPath=port)
            except Exception as exc:  # noqa: BLE001
                result["error"] = exc

        # The library's connect can block past any useful deadline; bound it.
        t = threading.Thread(target=_do, daemon=True, name=f"bench-connect-{port}")
        t.start()
        t.join(CONNECT_TIMEOUT_S)
        if "error" in result:
            raise result["error"]
        if "iface" not in result:
            raise TimeoutError(f"connect to {port} did not return in {CONNECT_TIMEOUT_S}s")
        return result["iface"]

    def _close(self, held: Held, reason: str) -> None:
        if held.serial_reader is not None:
            try:
                held.serial_reader.stop()
            except Exception:  # noqa: BLE001
                pass
            held.serial_reader = None
        iface, held.iface = held.iface, None
        held.connected = False
        if iface is not None:
            t = threading.Thread(target=_safe_close, args=(iface,), daemon=True)
            t.start()
            t.join(CLOSE_TIMEOUT_S)
        self.recorder.event("connection_closed", node=held.node.name, reason=reason)

    # -- health / reconnect ----------------------------------------------------

    def _health_loop(self) -> None:
        while not self._stop.wait(RECONNECT_SPACING_S):
            try:
                self.health_tick()
                self.recorder.heartbeat(component="observer", nodes=self._brief())
            except Exception as exc:  # noqa: BLE001 - the watchdog must not die
                self.recorder.event("observer_health_error", error=str(exc))

    def health_tick(self) -> None:
        """Reconnect anything that dropped, with spacing and a ceiling."""
        now = time.time()
        with self._lock:
            for held in self.held.values():
                if held.connected:
                    continue
                if held.dropped_at is None:
                    held.dropped_at = now
                if now - held.last_attempt < RECONNECT_SPACING_S:
                    continue
                if held.attempts >= RECONNECT_MAX_ATTEMPTS:
                    continue  # left dropped; status() reports it and the run can decide
                held.attempts += 1
                held.last_attempt = now
                ok, detail = self._open(held)
                if not ok:
                    self.recorder.event(
                        "reconnect_failed",
                        node=held.node.name,
                        attempt=held.attempts,
                        max_attempts=RECONNECT_MAX_ATTEMPTS,
                        detail=detail,
                    )

    def mark_dropped(self, name: str, reason: str) -> None:
        """Tell the observer a node is about to go away (a flash, a reboot).

        Called before a deliberate disconnection so the gap is attributed rather than
        looking like an unexplained capture hole.
        """
        held = self.held.get(name)
        if held is None:
            return
        with self._lock:
            self._close(held, reason=reason)
            held.dropped_at = time.time()
            held.attempts = 0
        self.recorder.event("capture_gap_opened", node=name, reason=reason)

    # -- pubsub ----------------------------------------------------------------

    def _wire_pubsub(self) -> None:
        if self._wired:
            return
        from pubsub import pub

        pub.subscribe(self._on_receive, "meshtastic.receive")
        pub.subscribe(self._on_log, "meshtastic.log.line")
        pub.subscribe(self._on_established, "meshtastic.connection.established")
        pub.subscribe(self._on_lost, "meshtastic.connection.lost")
        self._wired = True

    def _unwire_pubsub(self) -> None:
        if not self._wired:
            return
        from pubsub import pub

        for fn, topic in (
            (self._on_receive, "meshtastic.receive"),
            (self._on_log, "meshtastic.log.line"),
            (self._on_established, "meshtastic.connection.established"),
            (self._on_lost, "meshtastic.connection.lost"),
        ):
            try:
                pub.unsubscribe(fn, topic)
            except Exception:  # noqa: BLE001
                pass
        self._wired = False

    def _resolve_iface(self, interface: Any) -> Held | None:
        """Which node this interface belongs to.

        Identity first, then the device path. During a reconnect the library emits log
        lines on an interface the observer has not finished adopting, and those arrived
        unattributed - which silently drops them from every per-node assertion. The boot
        banner is exactly such a line, so the build tag itself was landing with no node
        against it.
        """
        for held in self.held.values():
            if held.iface is not None and held.iface is interface:
                return held
        port = _interface_port(interface)
        if port:
            for held in self.held.values():
                if held.port and held.port.upper() == port.upper():
                    return held
        return None

    def _on_receive(self, packet: Any = None, interface: Any = None, **_: Any) -> None:
        held = self._resolve_iface(interface)
        name = held.node.name if held else None
        try:
            row = packets.summarize(
                packet,
                node_nums=_node_nums(interface),
                direction=packets.SEEN,
                observer=name,
            )
            self.recorder.packet(row)
            if held is not None:
                held.packets_seen += 1
        except Exception as exc:  # noqa: BLE001 - never let a parse kill capture
            self.recorder.event("packet_parse_error", node=name, error=str(exc))

    def _on_log(self, line: str = "", interface: Any = None, **_: Any) -> None:
        held = self._resolve_iface(interface)
        name = held.node.name if held else None
        self.recorder.log(node=name, source="api", **_parse_log_line(line))
        if held is not None:
            held.log_lines += 1

    def _on_established(self, interface: Any = None, **_: Any) -> None:
        held = self._resolve_iface(interface)
        if held is not None:
            held.connected = True
        self.recorder.event(
            "connection_established_pubsub",
            node=held.node.name if held else None,
        )

    def _on_lost(self, interface: Any = None, **_: Any) -> None:
        held = self._resolve_iface(interface)
        if held is not None:
            held.connected = False
            held.dropped_at = time.time()
        self.recorder.event("connection_lost", node=held.node.name if held else None)

    # -- commanding ------------------------------------------------------------

    def interface(self, name: str) -> Any:
        """The held interface for a node, for commands that must ride the same link.

        Refuses a never_command node: opening or using a protobuf session against the
        stock observer destroys the property that makes it worth having.
        """
        held = self.held.get(name)
        if held is None:
            raise KeyError(f"unknown node {name!r}")
        devices.assert_commandable(held.node)
        if held.iface is None:
            raise RuntimeError(f"{name} is not connected")
        return held.iface

    def send_text(self, name: str, text: str, channel_index: int = 0, **kw: Any) -> Any:
        iface = self.interface(name)
        result = iface.sendText(text, channelIndex=channel_index, **kw)
        self.recorder.event("bench_send_text", node=name, text=text, channel=channel_index)
        return result

    # -- status ----------------------------------------------------------------

    def _brief(self) -> dict:
        return {
            name: {"connected": h.connected, "packets": h.packets_seen, "logs": h.log_lines}
            for name, h in self.held.items()
        }

    def status(self) -> dict:
        with self._lock:
            return {
                "nodes": {
                    name: {
                        "role": h.node.role,
                        "serial": h.node.serial_number,
                        "port": h.port,
                        "mode": "raw" if h.raw_mode else "api",
                        "connected": h.connected,
                        "attempts": h.attempts,
                        "dropped_for_s": (
                            None if h.dropped_at is None else round(time.time() - h.dropped_at, 1)
                        ),
                        "packets": h.packets_seen,
                        "log_lines": h.log_lines,
                    }
                    for name, h in self.held.items()
                },
                "dropped": [n for n, h in self.held.items() if not h.connected],
            }


def _safe_close(iface: Any) -> None:
    try:
        iface.close()
    except Exception:  # noqa: BLE001
        pass



def _interface_port(interface: Any) -> str | None:
    """The serial device path an interface is bound to, if it exposes one."""
    for attr in ("devPath", "devicePath", "dev_path"):
        value = getattr(interface, attr, None)
        if isinstance(value, str) and value:
            return value
    stream = getattr(interface, "stream", None)
    port = getattr(stream, "port", None)
    return port if isinstance(port, str) and port else None


def _node_nums(interface: Any) -> list[int]:
    """Node numbers this interface currently knows, for last-byte resolution.

    Read at capture time on purpose: the candidate set is time-dependent, so resolving
    later can disagree with what the node actually did.
    """
    try:
        by_num = getattr(interface, "nodesByNum", None)
        if isinstance(by_num, dict) and by_num:
            return [int(k) for k in by_num]
        nodes = getattr(interface, "nodes", None) or {}
        out = []
        for info in nodes.values():
            num = (info or {}).get("num")
            if isinstance(num, int):
                out.append(num)
        return out
    except Exception:  # noqa: BLE001
        return []


def _parse_log_line(line: str) -> dict:
    """Split a firmware log prefix, keeping the raw line whatever happens.

    Device uptime is captured but is only ever used for ordering, never for timing: the
    firmware prints it in whole seconds, which is three orders of magnitude too coarse
    for anything at radio timescales. That is why assertions count events instead.
    """
    out: dict[str, Any] = {"line": line}
    # The firmware colours its log prefix, and the escape codes sit between the level and
    # the pipe - so the prefix never matches and every line lands with no level at all.
    # The raw line is kept verbatim for grepping; only the copy we parse is cleaned.
    text = _ANSI_RE.sub("", line or "").strip()
    if len(text) < 6 or "|" not in text:
        return out
    level = text[:5].strip()
    if level not in ("DEBUG", "INFO", "WARN", "ERROR", "CRIT", "TRACE", "HEAP"):
        return out
    out["level"] = level
    rest = text.split("|", 1)[1].strip()
    parts = rest.split(None, 2)
    if len(parts) >= 2 and parts[1].isdigit():
        out["clock"] = parts[0]
        out["uptime_s"] = int(parts[1])
        out["msg"] = parts[2] if len(parts) > 2 else ""
    return out


class _RawSerialReader:
    """Plain-text serial tail for a node that is never commanded.

    Correct precisely because nothing ever opens a protobuf session on this port, so the
    firmware never silences its console output. Any use of this against a commanded node
    would capture nothing after that node's first API touch.

    The UART is shared, though. Meshtastic frames protobuf as
    0x94 0xc3 <len_hi> <len_lo> <payload>, and on a node that has ever had a client
    attached those frames interleave byte-for-byte with console text. Reading naive
    lines off the port yields text shredded by frame bytes - observed on this bench as
    lines like "\\x1aPacket RX (noise?) : 927ms\\x15...". So we demultiplex: frames are
    counted and stepped over, and only the text between them becomes a log line.
    """

    FRAME_START = bytes((0x94, 0xC3))
    # Meshtastic's own limit is 512; anything longer means the two header bytes were
    # really text that happened to collide, not a frame.
    MAX_FRAME = 512

    def __init__(self, held: Held, recorder: streams.Recorder, baud: int = 115200) -> None:
        self.held = held
        self.recorder = recorder
        self.baud = baud
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ser: Any = None
        self._buf = bytearray()
        self._frames_skipped = 0

    def start(self) -> None:
        import serial

        self._ser = serial.Serial(self.held.port, self.baud, timeout=1)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name=f"bench-raw-{self.held.node.name}"
        )
        self._thread.start()

    def _loop(self) -> None:
        name = self.held.node.name
        while not self._stop.is_set():
            try:
                chunk = self._ser.read(256)
            except Exception as exc:  # noqa: BLE001 - device unplugged, port closed
                self.recorder.event("raw_serial_error", node=name, error=str(exc))
                self.held.connected = False
                return
            if not chunk:
                continue
            self._buf.extend(chunk)
            for line in self.drain():
                self.recorder.log(node=name, source="raw", **_parse_log_line(line))
                self.held.log_lines += 1

    def drain(self) -> list[str]:
        """Pull complete text lines out of the buffer, stepping over protobuf frames."""
        newline = ord("\n")
        lines: list[str] = []
        while True:
            frame_at = self._buf.find(self.FRAME_START)
            newline_at = self._buf.find(bytes((newline,)))

            # A frame header ahead of the next newline: emit any text before it, then
            # skip the whole frame body so its bytes never reach a log line.
            if frame_at != -1 and (newline_at == -1 or frame_at < newline_at):
                if frame_at:
                    lines.extend(self._text_lines(self._buf[:frame_at]))
                if len(self._buf) < frame_at + 4:
                    del self._buf[:frame_at]
                    return lines  # header split across reads; wait for the length bytes
                size = (self._buf[frame_at + 2] << 8) | self._buf[frame_at + 3]
                if size > self.MAX_FRAME:
                    # Colliding text bytes, not a frame. Drop the two header bytes and
                    # carry on rather than eating good text after them.
                    del self._buf[: frame_at + 2]
                    continue
                end = frame_at + 4 + size
                if len(self._buf) < end:
                    del self._buf[:frame_at]
                    return lines  # frame still arriving
                del self._buf[:end]
                self._frames_skipped += 1
                continue

            if newline_at == -1:
                return lines
            raw = self._buf[:newline_at]
            del self._buf[: newline_at + 1]
            lines.extend(self._text_lines(raw))

    @staticmethod
    def _text_lines(raw: bytes) -> list[str]:
        """Decode a text run, dropping the NULs and CRs that frame padding leaves."""
        text = bytes(raw).decode("utf-8", errors="replace")
        text = text.replace(chr(13), "").replace(chr(0), "").strip()
        return [text] if text else []

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:  # noqa: BLE001
                pass
            self._ser = None
        self.recorder.event(
            "raw_serial_closed",
            node=self.held.node.name,
            frames_skipped=self._frames_skipped,
        )
