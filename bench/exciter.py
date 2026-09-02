"""The exciter: a bench instrument that puts raw energy on the air on demand.

Reach for this LAST. A stock peer in a tight send loop occupies the channel well enough
for most of an LBT matrix, costs no custom firmware, and is version-matched for free. The
exciter earns its place only where a valid frame will not do - CAD threshold and detPeak
calibration, and the false-preamble path - because those need carrier or preamble with
controlled dwell rather than a well-formed packet.

Two constraints shape the interface.

  It is USB-only. The exciter's radio is busy by definition, so it can never be commanded
  over LoRa. That is a property of the role, not an implementation detail.

  It must carry identity like every other artifact. STATUS returns the firmware's build
  tag, and the bench refuses to use an exciter it cannot identify - an instrument whose
  provenance is unknown produces measurements whose provenance is unknown.

The wire protocol is deliberately line-based ASCII, so the instrument stays debuggable
from a plain serial terminal when a run goes wrong at 3am:

    ->  CONFIG <freq_hz> <sf> <bw_hz>   configure the radio, no emission
    ->  CARRIER <ms>                    unmodulated carrier for <ms>
    ->  PREAMBLE <ms>                   preamble symbols for <ms>
    ->  IDLE                            stop emitting now
    ->  STATUS                          identity and state
    <-  OK <detail>   |   ERR <reason>

Every command answers on one line. A command that would emit for longer than
MAX_DWELL_MS is refused host-side as well as firmware-side: an exciter stuck transmitting
is both a wedged bench and an airtime problem.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from . import devices

# Upper bound on a single emission. Long enough for any CAD scan by orders of magnitude,
# short enough that a lost host cannot leave a carrier running.
MAX_DWELL_MS = 5000

RESPONSE_TIMEOUT_S = 5.0
BAUD = 115200


class ExciterError(RuntimeError):
    pass


@dataclass
class ExciterStatus:
    build_tag: str | None
    state: str
    freq_hz: int | None = None
    sf: int | None = None
    bw_hz: int | None = None
    raw: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class Exciter:
    """Host-side driver for an exciter node.

    Holds the port for the whole session like every other node, because opening it per
    command would add USB enumeration latency to a stimulus that is supposed to be
    tightly controlled.
    """

    def __init__(self, node: devices.BenchNode, recorder: Any = None) -> None:
        if node.role != "exciter":
            raise ExciterError(f"{node.name} has role {node.role!r}, not 'exciter'")
        self.node = node
        self.recorder = recorder
        self._serial: Any = None
        self._lock = threading.Lock()
        self.status_at_open: ExciterStatus | None = None

    # -- lifecycle -------------------------------------------------------------

    def open(self, require_identity: bool = True) -> ExciterStatus:
        import serial

        port = devices.resolve_port(self.node.serial_number)
        self._serial = serial.Serial(port, BAUD, timeout=RESPONSE_TIMEOUT_S)
        time.sleep(0.3)  # let the CDC link settle before the first command
        self._serial.reset_input_buffer()

        status = self.status()
        if require_identity and not status.build_tag:
            self.close()
            raise ExciterError(
                f"{self.node.name} did not report a build tag. An instrument whose "
                "provenance is unknown produces measurements whose provenance is "
                "unknown - build the exciter env with -D BENCH_BUILD_TAG."
            )
        self.status_at_open = status
        self._record("exciter_open", port=port, status=status.to_dict())
        return status

    def close(self) -> None:
        if self._serial is None:
            return
        try:
            self._command("IDLE")  # never leave the bench transmitting
        except ExciterError:
            pass
        try:
            self._serial.close()
        finally:
            self._serial = None
            self._record("exciter_close")

    def __enter__(self) -> Exciter:
        self.open()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- commands --------------------------------------------------------------

    def configure(self, freq_hz: int, sf: int, bw_hz: int) -> str:
        return self._command(f"CONFIG {int(freq_hz)} {int(sf)} {int(bw_hz)}")

    def carrier(self, dwell_ms: int) -> str:
        """Unmodulated carrier. CAD should detect this as activity."""
        return self._command(f"CARRIER {self._dwell(dwell_ms)}")

    def preamble(self, dwell_ms: int) -> str:
        """Preamble symbols - what CAD is actually designed to detect."""
        return self._command(f"PREAMBLE {self._dwell(dwell_ms)}")

    def idle(self) -> str:
        return self._command("IDLE")

    def status(self) -> ExciterStatus:
        line = self._command("STATUS")
        fields = dict(
            part.split("=", 1) for part in line.split() if "=" in part
        )
        return ExciterStatus(
            build_tag=fields.get("tag"),
            state=fields.get("state", "unknown"),
            freq_hz=_int(fields.get("freq")),
            sf=_int(fields.get("sf")),
            bw_hz=_int(fields.get("bw")),
            raw=line,
        )

    def burst(self, count: int, dwell_ms: int, gap_ms: int, mode: str = "carrier") -> dict:
        """Repeated emissions - the shape a counting assertion needs.

        Returns what it actually did rather than what it was asked to do, so a row can
        distinguish "the DUT did not defer" from "the stimulus never ran".
        """
        emit = self.carrier if mode == "carrier" else self.preamble
        sent, failed = 0, []
        self._record("exciter_burst_start", count=count, dwell_ms=dwell_ms, mode=mode)
        for i in range(count):
            try:
                emit(dwell_ms)
                sent += 1
            except ExciterError as exc:
                failed.append({"index": i, "error": str(exc)})
            time.sleep(gap_ms / 1000.0)
        result = {"requested": count, "emitted": sent, "failed": failed, "mode": mode}
        self._record("exciter_burst_done", **result)
        return result

    # -- transport -------------------------------------------------------------

    def _dwell(self, dwell_ms: int) -> int:
        value = int(dwell_ms)
        if value <= 0 or value > MAX_DWELL_MS:
            raise ExciterError(f"dwell {value}ms outside 1..{MAX_DWELL_MS}")
        return value

    def _command(self, line: str) -> str:
        if self._serial is None:
            raise ExciterError("exciter is not open")
        with self._lock:
            self._serial.reset_input_buffer()
            self._serial.write((line + "\n").encode("ascii"))
            self._serial.flush()
            raw = self._serial.readline()
        if not raw:
            raise ExciterError(f"no response to {line!r} within {RESPONSE_TIMEOUT_S}s")
        reply = raw.decode("utf-8", errors="replace").strip()
        if reply.startswith("ERR"):
            raise ExciterError(f"{line!r} refused: {reply[4:].strip()}")
        if not reply.startswith("OK"):
            raise ExciterError(f"{line!r} got an unparseable reply: {reply!r}")
        return reply[3:].strip()

    def _record(self, kind: str, **data: Any) -> None:
        if self.recorder is not None:
            self.recorder.event(kind, node=self.node.name, **data)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
