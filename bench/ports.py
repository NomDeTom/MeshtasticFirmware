"""Sole ownership of a device's serial port, with a budget on every operation.

A serial port is exclusive, and this library's connect() and close() can both block
indefinitely - so every bounded call abandons a thread that is still holding the handle.
With several components opening ports independently, that produced two state machines
racing for one device and four distinct failures that all looked like hardware: "no UF2
volume appeared", "enumerated but not answering", "did not become ready within 90s", and
a run that simply stopped emitting events.

The rule here is one owner per device, and one operation at a time on it. Nothing outside
this module opens a node's port. Everything else asks the owner, which serialises the
work and hands back the interface it already holds rather than opening another.

Two things every operation must state, because a bench that runs unattended cannot
afford either to be implicit:

  a budget - how long this is allowed to take, always bounded, never open-ended; and
  an exit state - exactly one of the outcomes below, so "it did not work" is never
  ambiguous between refused, absent, busy, timed out and failed.

Those two together make the schedule knowable in advance: a run's duration is the sum of
its budgets, and any step that ends early or late says which outcome it ended with.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from . import devices

# -- exit states ----------------------------------------------------------------
# Every device operation ends in exactly one of these. They are deliberately distinct:
# conflating "the device is not there" with "it did not answer in time" is what turned a
# rebooting node into a refused flash and lost a whole matrix.

OK = "ok"
TIMED_OUT = "timed_out"  # the budget was spent; the device may still be working
ABSENT = "absent"  # not enumerated at all
BUSY = "busy"  # another operation owns this device
REFUSED = "refused"  # policy forbids it - a never-command observer, say
FAILED = "failed"  # it ran and went wrong, with a reason

TERMINAL = (OK, TIMED_OUT, ABSENT, BUSY, REFUSED, FAILED)

# -- port states ----------------------------------------------------------------

ST_ABSENT = "absent"  # not enumerated
ST_IDLE = "idle"  # enumerated, nothing open
ST_HELD = "held"  # the observer holds it for continuous capture
ST_LEASED = "leased"  # exclusively leased to one operation
ST_REBOOTING = "rebooting"  # deliberately going away; do not touch
ST_LOST = "lost"  # was held, dropped unexpectedly
ST_GAVE_UP = "gave_up"  # reconnect ceiling exhausted; needs a human


class PortBusy(RuntimeError):
    """Another operation owns this device and did not finish within the budget."""


@dataclass
class Result:
    """What an operation did, how long it had, and how long it took."""

    outcome: str
    detail: str = ""
    elapsed_s: float = 0.0
    budget_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.outcome == OK

    @property
    def overran(self) -> bool:
        return self.budget_s > 0 and self.elapsed_s > self.budget_s

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "detail": self.detail,
            "elapsed_s": round(self.elapsed_s, 1),
            "budget_s": self.budget_s,
            "overran": self.overran,
        }


class Budget:
    """A deadline you can ask how much is left of."""

    def __init__(self, seconds: float) -> None:
        self.seconds = float(seconds)
        self.started = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    @property
    def remaining(self) -> float:
        return max(0.0, self.seconds - self.elapsed)

    @property
    def spent(self) -> bool:
        return self.remaining <= 0

    def result(self, outcome: str, detail: str = "") -> Result:
        return Result(outcome, detail, self.elapsed, self.seconds)


class PortOwner:
    """The only thing that opens one node's port.

    Capture holds the port continuously; anything else takes an exclusive lease. A lease
    suspends reconnection for its duration and hands over the interface already open, so
    the port is never released and reacquired - which is the only way to be certain
    nothing races for it.
    """

    def __init__(
        self,
        node: devices.BenchNode,
        recorder: Any = None,
        connect_timeout: float = 25.0,
    ) -> None:
        self.node = node
        self.recorder = recorder
        self.connect_timeout = connect_timeout

        self._lock = threading.RLock()  # one operation at a time on this device
        self._busy = threading.Lock()  # held for the duration of a lease
        self.state = ST_ABSENT
        self.iface: Any = None
        self.port: str | None = None
        self.reconnects = 0
        self.dropped_at: float | None = None
        self.last_error: str | None = None
        # What the DEVICE said it is, once anything has asked. Distinct from the board
        # declared in the node table: the table is hand-written and is exactly the thing
        # that gets a board wrong, so both are shown and a mismatch is visible.
        self.observed_model: str | None = None
        self.observed_node_id: str | None = None
        self.firmware: str | None = None

    # -- reporting -------------------------------------------------------------

    def _event(self, kind: str, **data: Any) -> None:
        if self.recorder is not None:
            self.recorder.event(kind, node=self.node.name, **data)

    def _to(self, state: str, why: str = "") -> None:
        if state != self.state:
            self._event("port_state", was=self.state, now=state, why=why)
            self.state = state

    def observe(self, iface: Any) -> None:
        """Record what the device says it is, from an interface already open."""
        try:
            info = iface.getMyNodeInfo() or {}
            user = info.get("user") or {}
            self.observed_model = user.get("hwModel") or self.observed_model
            self.observed_node_id = user.get("id") or self.observed_node_id
            meta = getattr(iface, "metadata", None)
            self.firmware = getattr(meta, "firmware_version", None) or self.firmware
        except Exception:  # noqa: BLE001 - identity is a nicety here, never a blocker
            pass

    def status(self) -> dict:
        return {
            "node": self.node.name,
            "state": self.state,
            "port": self.port,
            "reconnects": self.reconnects,
            "dropped_for_s": (
                None if self.dropped_at is None else round(time.time() - self.dropped_at, 1)
            ),
            "last_error": self.last_error,
            # Identity, declared and observed side by side.
            "role": self.node.role,
            "serial_number": self.node.serial_number,
            "declared_board": self.node.board,
            "observed_model": self.observed_model,
            "node_id": self.observed_node_id,
            "firmware": self.firmware,
            "board_matches": (
                None if not (self.node.board and self.observed_model)
                else self.node.board.strip().upper() == self.observed_model.strip().upper()
            ),
            "never_command": self.node.never_command,
            "never_flash": self.node.never_flash,
            "capture": "raw serial" if self.node.never_command else "protobuf api",
        }

    # -- presence --------------------------------------------------------------

    def resolve(self) -> str | None:
        """Current port for this node's USB serial, re-resolved every time.

        Never cached: a node that reboots can come back on a different port, and one
        that enumerates without its serial descriptor does not come back at all.
        """
        return devices.try_resolve_port(self.node.serial_number)

    def wait_present(self, budget_s: float = 90.0) -> Result:
        """Wait for the device to enumerate."""
        budget = Budget(budget_s)
        while not budget.spent:
            port = self.resolve()
            if port is not None:
                self.port = port
                if self.state == ST_ABSENT:
                    self._to(ST_IDLE, "enumerated")
                return budget.result(OK, port)
            time.sleep(1.0)
        self._to(ST_ABSENT, "did not enumerate")
        return budget.result(ABSENT, f"{self.node.serial_number} did not enumerate")

    # -- capture ---------------------------------------------------------------

    def hold(self, budget_s: float = 60.0) -> Result:
        """Open the port for continuous capture. The observer's normal state."""
        with self._lock:
            if self.state == ST_LEASED:
                return Result(BUSY, "leased to another operation", 0.0, budget_s)
            if self.iface is not None:
                return Result(OK, "already held", 0.0, budget_s)

            budget = Budget(budget_s)
            present = self.wait_present(min(budget.remaining, 30.0))
            if not present.ok:
                return budget.result(ABSENT, present.detail)

            iface, error = self._open(self.port, budget.remaining)
            if iface is None:
                self.last_error = error
                self._to(ST_LOST if self.dropped_at else ST_IDLE, error or "open failed")
                return budget.result(TIMED_OUT if error is None else FAILED, error or "open timed out")

            self.iface = iface
            self.observe(iface)
            if self.dropped_at is not None:
                self._event("capture_gap_closed", gap_s=round(time.time() - self.dropped_at, 1))
                self.dropped_at = None
            self._to(ST_HELD, "capture open")
            return budget.result(OK, self.port or "")

    def note_raw_capture(self, port: str | None, running: bool) -> None:
        """Record that a raw-serial reader owns this port instead of an API connection.

        The never-commanded observer is captured on raw serial, so it never takes a hold.
        Without this its owner reports `absent` on the dashboard while the node is in fact
        being watched perfectly well - a display that contradicts the evidence it is
        rendering is worse than no display.
        """
        self.port = port
        self._to(ST_HELD if running else ST_ABSENT, "raw serial capture")

    def release(self, reason: str, abandon: bool = False) -> None:
        """Stop holding the port.

        `abandon` skips the close entirely, and is mandatory whenever the device is
        going away - a reboot, a DFU entry. close() blocks on a node the library is
        still draining, so it runs on a thread that gets abandoned anyway, and that
        thread keeps the exclusive handle against whatever needs the port next.
        """
        with self._lock:
            iface, self.iface = self.iface, None
            if iface is not None:
                _let_go(iface, abandon=abandon)
            self.dropped_at = time.time()
            self._to(ST_REBOOTING if abandon else ST_IDLE, reason)
            self._event("capture_gap_opened", reason=reason, abandoned=abandon)

    # -- exclusive work --------------------------------------------------------

    @contextmanager
    def lease(
        self,
        reason: str,
        budget_s: float = 120.0,
        reboots: bool = False,
    ) -> Iterator[Any]:
        """Take the device exclusively and hand back a live interface.

        Blocks reconnection for the duration, so nothing reclaims the port underneath
        the operation. `reboots=True` means the caller expects the device to disappear,
        so the handle is abandoned rather than closed on the way out.
        """
        budget = Budget(budget_s)
        if not self._busy.acquire(timeout=max(1.0, budget.remaining)):
            raise PortBusy(f"{self.node.name} is busy; waited {budget.elapsed:.0f}s")

        acquired_state = self.state
        try:
            with self._lock:
                self._to(ST_LEASED, reason)
                self._event("lease_start", reason=reason, budget_s=budget_s)
                iface = self.iface
                self.iface = None  # ownership moves to the caller for the lease

            if iface is None:
                iface, error = self._open(self.resolve(), budget.remaining)
                if iface is None:
                    self._event("lease_failed", reason=reason, error=error or "timed out")
                    raise PortBusy(f"{self.node.name}: could not open ({error or 'timed out'})")

            yield iface
        finally:
            # A reboot means the device is disappearing: abandon. Otherwise give the
            # interface back to capture rather than closing and reopening it.
            with self._lock:
                if reboots:
                    _let_go(iface, abandon=True)
                    self.iface = None
                    self.dropped_at = time.time()
                    self._to(ST_REBOOTING, f"{reason} (device rebooting)")
                else:
                    self.iface = iface
                    self._to(acquired_state if acquired_state == ST_HELD else ST_IDLE, "lease ended")
                self._event(
                    "lease_end", reason=reason, elapsed_s=round(budget.elapsed, 1),
                    budget_s=budget_s, overran=budget.elapsed > budget_s, reboots=reboots,
                )
            self._busy.release()

    def expect_reboot(self, reason: str) -> None:
        """Declare that the device is about to vanish, and drop the handle for it."""
        self.release(reason, abandon=True)
        self._to(ST_REBOOTING, reason)

    def wait_answering(self, budget_s: float = 180.0, spacing: float = 4.0) -> Result:
        """Wait until the device is enumerated AND holding a capture connection again.

        A single bounded open is not liveness. Flashing and provisioning both reboot the
        node, and an open attempted while it is still enumerating times out and reports
        healthy hardware as dead - a verdict expensive enough to fail the row and every
        row after it.
        """
        budget = Budget(budget_s)
        attempt = 0
        while not budget.spent:
            attempt += 1
            result = self.hold(min(budget.remaining, 30.0))
            if result.ok:
                self.reconnects += 1 if attempt > 1 else 0
                return budget.result(OK, f"answering after {attempt} attempt(s)")
            self._event("node_not_ready", attempt=attempt, detail=result.detail[:120])
            time.sleep(spacing)
        self._to(ST_GAVE_UP, f"no answer within {budget_s:.0f}s")
        return budget.result(TIMED_OUT, f"did not answer within {budget_s:.0f}s")

    # -- the one place a port is opened ----------------------------------------

    def _open(self, port: str | None, budget_s: float) -> tuple[Any, str | None]:
        """Open a SerialInterface, bounded. Returns (iface, error).

        The only call to SerialInterface() in the bench. The thread is abandoned on
        timeout, which is why nothing else may open this port: an abandoned open still
        holds it.
        """
        if port is None:
            return None, "not enumerated"

        import meshtastic.serial_interface as si

        out: dict[str, Any] = {}

        def _go() -> None:
            try:
                out["iface"] = si.SerialInterface(devPath=port)
            except Exception as exc:  # noqa: BLE001
                out["error"] = f"{type(exc).__name__}: {exc}"[:160]

        thread = threading.Thread(target=_go, daemon=True, name=f"bench-open-{port}")
        thread.start()
        thread.join(min(budget_s, self.connect_timeout))
        if "iface" in out:
            return out["iface"], None
        return None, out.get("error")


def _let_go(iface: Any, abandon: bool) -> None:
    """Release an interface. `abandon` skips the protocol close, never the OS handle.

    This distinction cost a whole matrix. close() is slow because it performs a protocol
    disconnect and can block forever on a node the library is still draining - so on a
    device that is going away we skip it. But skipping the WHOLE close leaks the serial
    handle, and the port then stays owned by this process: every later open fails with
    "Access is denied", the owner gives up, and six rows report a healthy node as dead.

    So abandon means "do not wait for a graceful goodbye", not "do not hang up". The
    underlying stream is closed directly, which is immediate and frees the port.
    """
    if iface is None:
        return
    if abandon:
        try:
            iface._wantExit = True  # stop its reader logging the vanish as an error
        except Exception:  # noqa: BLE001
            pass
        # Release the OS handle without the protocol drain that makes close() block.
        for attr in ("stream", "_serial", "serial"):
            handle = getattr(iface, attr, None)
            if handle is not None and hasattr(handle, "close"):
                try:
                    handle.close()
                except Exception:  # noqa: BLE001
                    pass
                break
        return
    thread = threading.Thread(target=_safe_close, args=(iface,), daemon=True)
    thread.start()
    thread.join(5.0)  # abandoned past this; the handle is released below regardless
    if thread.is_alive():
        _let_go(iface, abandon=True)  # the graceful close hung: take the handle back


def _safe_close(iface: Any) -> None:
    try:
        iface.close()
    except Exception:  # noqa: BLE001
        pass


# -- schedule -------------------------------------------------------------------


# -- schedule -------------------------------------------------------------------

PLANNED = "planned"
RUNNING = "running"
DONE = "done"
SKIPPED = "skipped"
FAILED_STEP = "failed"


@dataclass
class Step:
    """One planned unit of work, its budget, and what became of it.

    Steps nest. A row's provisioning is one line in the plan and eight operations
    underneath it, and the difference matters: most of those are skipped when the node is
    already in the required state, so a flat plan overstates the run by an hour and gives
    no way to see where the time actually went.
    """

    id: str
    name: str
    budget_s: float
    detail: str = ""
    kind: str = ""
    node: str | None = None
    children: list["Step"] = field(default_factory=list)
    status: str = PLANNED
    outcome: str | None = None
    started_at: float | None = None
    ended_at: float | None = None

    @property
    def elapsed_s(self) -> float | None:
        if self.started_at is None:
            return None
        return (self.ended_at or time.time()) - self.started_at

    @property
    def overran(self) -> bool:
        el = self.elapsed_s
        return bool(el and self.budget_s and el > self.budget_s)

    def add(self, step_id: str, name: str, budget_s: float, detail: str = "", **kw) -> "Step":
        child = Step(step_id, name, budget_s, detail, **kw)
        self.children.append(child)
        return child

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "budget_s": self.budget_s,
            "detail": self.detail,
            "kind": self.kind,
            "node": self.node,
            "status": self.status,
            "outcome": self.outcome,
            "elapsed_s": None if self.elapsed_s is None else round(self.elapsed_s, 1),
            "overran": self.overran,
            "children": [c.to_dict() for c in self.children],
        }


@dataclass
class Schedule:
    """What the run intends to do, how long it has, and how far it has got.

    Written before the run starts, so an unattended bench has a knowable end rather than
    an open-ended one. The budgets are a worst case: a run past the total has something
    wrong rather than something slow, and a step that finishes early says so by leaving
    the difference between its budget and its elapsed time on the page.
    """

    steps: list[Step] = field(default_factory=list)

    def add(self, step_id: str, name: str, budget_s: float, detail: str = "", **kw) -> Step:
        step = Step(step_id, name, budget_s, detail, **kw)
        self.steps.append(step)
        return step

    def find(self, step_id: str) -> Step | None:
        for top in self.steps:
            for step in top.walk():
                if step.id == step_id:
                    return step
        return None

    def begin(self, step_id: str) -> Step | None:
        step = self.find(step_id)
        if step is not None:
            step.status = RUNNING
            step.started_at = time.time()
        return step

    def finish(self, step_id: str, status: str = DONE, outcome: str | None = None) -> Step | None:
        step = self.find(step_id)
        if step is not None:
            step.status = status
            step.outcome = outcome
            step.ended_at = time.time()
        return step

    def skip(self, step_id: str, why: str = "") -> Step | None:
        """Mark a step skipped - the node was already as required, say.

        Distinct from done: a skipped step spent none of its budget, which is why the
        plan's total is a ceiling rather than an estimate.
        """
        return self.finish(step_id, SKIPPED, why)

    @property
    def total_s(self) -> float:
        return sum(s.budget_s for s in self.steps)

    @property
    def counts(self) -> dict:
        out = {PLANNED: 0, RUNNING: 0, DONE: 0, SKIPPED: 0, FAILED_STEP: 0}
        for top in self.steps:
            for step in top.walk():
                out[step.status] = out.get(step.status, 0) + 1
        return out

    def to_dict(self) -> dict:
        return {
            "steps": [s.to_dict() for s in self.steps],
            "total_s": round(self.total_s, 1),
            "count": len(self.steps),
            "counts": self.counts,
        }

    def summary(self) -> str:
        lines = []
        for step in self.steps:
            mark = {PLANNED: " ", RUNNING: ">", DONE: "x", SKIPPED: "-", FAILED_STEP: "!"}
            lines.append(f"  [{mark.get(step.status, ' ')}] {step.name:42} {step.budget_s:6.0f}s  {step.detail}")
            for child in step.children:
                lines.append(f"        {mark.get(child.status, ' ')} {child.name:38} {child.budget_s:6.0f}s")
        hours, rem = divmod(int(self.total_s), 3600)
        lines.append(f"  {'TOTAL (worst case)':46} {self.total_s:6.0f}s  = {hours}h{rem // 60:02d}m")
        return "\n".join(lines)
