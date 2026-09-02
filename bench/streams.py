"""Append-only JSONL streams: the logger half of stage 0.

Deliberately dumb. It records and it never interprets - no deduplication, no
correlation, no dropping. Interpretation lives in the ledger, so it can be changed,
fixed or re-run without re-acquiring the evidence, which matters because acquisition
needs the hardware and a rerun costs hours.

Four streams per run, matching what the questions actually need:

  logs.jsonl     firmware log lines (LogRecord over the API, or raw serial text)
  packets.jsonl  full MeshPacket rows from bench.packets
  events.jsonl   connection lifecycle, capture gaps, and scenario markers
  status.jsonl   heartbeats, so a dead run is distinguishable from a slow one

Liveness is a first-class concern: a stream that stopped silently turns every later row
into a false negative, so `status()` reports per-stream counts and last-write times and
the runner asserts on them before trusting any window.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator


# A bench writes continuously for hours, and its own USB activity can disturb the volume
# it writes to. Flashing a node re-enumerates the bus, and on a bench whose artifacts live
# on an external USB drive that surfaced mid-run as WinError 433 / Errno 22 and killed the
# whole thing on one failed write. Evidence gathered over hours must not be lost to a blip
# lasting milliseconds, so artifact writes retry before giving up.
WRITE_RETRIES = 6
WRITE_BACKOFF_S = 0.4


def durable_write_text(path: Path, text: str, retries: int = WRITE_RETRIES) -> bool:
    """Write a file, retrying transient OS errors. True if it landed.

    Returns rather than raises: a status file that could not be written is a degraded
    run, not a failed one, and the caller decides which.
    """
    last = None
    for attempt in range(retries):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            return True
        except OSError as exc:
            last = exc
            time.sleep(WRITE_BACKOFF_S * (attempt + 1))
    logging.getLogger("bench.streams").warning("could not write %s: %s", path, last)
    return False


LOGS = "logs"
PACKETS = "packets"
EVENTS = "events"
STATUS = "status"
STREAM_NAMES = (LOGS, PACKETS, EVENTS, STATUS)


@dataclass
class _Stream:
    path: Path
    count: int = 0
    bytes_written: int = 0
    last_ts: float | None = None
    _fh: Any = field(default=None, repr=False)

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Line-buffered append. A crashed run must leave every row it already wrote.
        self._fh = self.path.open("a", encoding="utf-8", buffering=1)

    def write(self, row: dict) -> None:
        if self._fh is None:
            self.open()
        line = json.dumps(row, default=_fallback, ensure_ascii=False)
        for attempt in range(3):
            try:
                self._fh.write(line + "\n")
                break
            except (OSError, ValueError):
                # The handle can be invalidated under us when the volume blips - the
                # bench's own USB activity disturbs an external drive it writes to.
                # Reopen and retry rather than losing this row and every row after it.
                try:
                    self.close()
                except Exception:  # noqa: BLE001
                    pass
                if attempt == 2:
                    return  # one row dropped; the stream survives
                time.sleep(WRITE_BACKOFF_S)
                self.open()
        self.count += 1
        self.bytes_written += len(line) + 1
        self.last_ts = time.time()

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
                self._fh.close()
            finally:
                self._fh = None


def _fallback(obj: Any) -> Any:
    """Never let an odd protobuf value kill a write. Raw is the archive."""
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    return repr(obj)


class Recorder:
    """Owns the four streams for one run directory.

    Thread-safe: pubsub callbacks arrive on the meshtastic library's reader threads,
    one per held interface, and they all land here.
    """

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)
        self._lock = threading.Lock()
        self._streams = {n: _Stream(self.run_dir / f"{n}.jsonl") for n in STREAM_NAMES}
        self._started = time.time()
        self._paused = False

    # -- writing ---------------------------------------------------------------

    def write(self, stream: str, row: dict) -> None:
        if stream not in self._streams:
            raise KeyError(f"unknown stream {stream!r}")
        with self._lock:
            if self._paused:
                return
            self._streams[stream].write(row)

    def log(self, **row: Any) -> None:
        self.write(LOGS, {"ts": time.time(), **row})

    def packet(self, row: dict) -> None:
        self.write(PACKETS, row)

    def event(self, kind: str, **row: Any) -> None:
        """Connection lifecycle, capture gaps, scenario markers."""
        self.write(EVENTS, {"ts": time.time(), "kind": kind, **row})

    def mark(self, label: str, **data: Any) -> dict:
        """Scenario boundary marker.

        Written to BOTH events and logs, so a single grep over either stream finds it.
        Slicing a continuous capture per row depends on these rather than on wall-clock
        arithmetic, which drifts across a multi-hour session.
        """
        row = {"ts": time.time(), "kind": "mark", "label": label, "data": data}
        self.write(EVENTS, row)
        self.write(LOGS, {**row, "level": "MARK", "line": f"=== MARK {label} ==="})
        return row

    def heartbeat(self, **row: Any) -> None:
        """Aged by the status server to tell DIED from RUNNING."""
        self.write(STATUS, {"ts": time.time(), **row})

    # -- lifecycle -------------------------------------------------------------

    def pause(self, reason: str | None = None) -> None:
        self.event("recorder_pause", reason=reason)
        with self._lock:
            self._paused = True

    def resume(self) -> None:
        with self._lock:
            self._paused = False
        self.event("recorder_resume")

    def close(self) -> None:
        with self._lock:
            for s in self._streams.values():
                s.close()

    # -- liveness --------------------------------------------------------------

    def status(self) -> dict:
        """Per-stream counts and last-write times.

        Assert on this before trusting any window. A stream whose last_ts is far behind
        wall-clock has stopped, and every NOT OBSERVED verdict computed against it is
        worthless.
        """
        now = time.time()
        with self._lock:
            streams = {
                name: {
                    "path": str(s.path),
                    "rows": s.count,
                    "bytes": s.bytes_written,
                    "last_ts": s.last_ts,
                    "age_s": None if s.last_ts is None else round(now - s.last_ts, 1),
                }
                for name, s in self._streams.items()
            }
            paused = self._paused
        return {
            "run_dir": str(self.run_dir),
            "started": self._started,
            "uptime_s": round(now - self._started, 1),
            "paused": paused,
            "streams": streams,
        }

    def assert_live(self, max_age_s: float = 120.0, streams: tuple[str, ...] = (EVENTS,)) -> None:
        """Raise if a stream that should be moving has gone quiet."""
        st = self.status()
        stale = []
        for name in streams:
            info = st["streams"][name]
            if info["last_ts"] is None:
                stale.append(f"{name} has never written")
            elif info["age_s"] > max_age_s:
                stale.append(f"{name} last wrote {info['age_s']}s ago")
        if stale:
            raise CaptureStalled("; ".join(stale))


class CaptureStalled(RuntimeError):
    """A stream stopped silently. Every window computed against it is suspect."""


def read_stream(run_dir: Path, stream: str) -> Iterator[dict]:
    """Replay a stream from disk. Tolerates a truncated final line from a hard kill."""
    path = Path(run_dir) / f"{stream}.jsonl"
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue  # a partial tail from an abrupt exit; the rest is still good


def window(
    run_dir: Path,
    stream: str,
    start: float | None = None,
    end: float | None = None,
) -> list[dict]:
    """Rows within a time window, by the recorder's own timestamps."""
    out = []
    for row in read_stream(run_dir, stream):
        ts = row.get("ts")
        if ts is None:
            continue
        if start is not None and ts < start:
            continue
        if end is not None and ts > end:
            continue
        out.append(row)
    return out


def marks(run_dir: Path) -> list[dict]:
    """Every scenario boundary marker, in order."""
    return [r for r in read_stream(run_dir, EVENTS) if r.get("kind") == "mark"]


def between_marks(run_dir: Path, start_label: str, end_label: str, stream: str) -> list[dict]:
    """Rows between two markers. The per-scenario slice of a continuous capture."""
    found = {m["label"]: m["ts"] for m in marks(run_dir)}
    start, end = found.get(start_label), found.get(end_label)
    if start is None:
        return []
    return window(run_dir, stream, start=start, end=end)
