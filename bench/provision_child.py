"""The child half of an isolated provisioning: one node, one operation, then exit.

    python -m bench.provision_child   < job.json

Started by provision.IsolatedProvisioner, never by hand. The job arrives as one JSON
object on stdin - never argv, because a spec can carry a channel URL and that is key
material. Everything the child has to say goes to stdout as tagged JSON lines:

    {"t": "event", "kind": ..., "data": {...}}   what the Provisioner and the port owner
                                                 report, under the kinds they always had
    {"t": "log", "line": ...}                    a log line the node sent this connection
    {"t": "result", ...}                         last: the state, the problems, or why not

The point of the process boundary is what happens at the end. Whatever this process
opened - an open that overran against a silent node, a reader thread that never
stopped, an interface abandoned across a reboot - the OS closes when it exits. So it
exits hard, with os._exit, as soon as the result is written: nothing it holds is worth
an orderly interpreter shutdown, and nothing it leaves behind can reach the run.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any, Callable, TextIO

from . import devices, ports
from .provision import (
    CHILD_TAG,
    NodeSpec,
    ProvisionError,
    Provisioner,
)


class _Emitter:
    """Tagged JSON lines on the protocol stream, safe from any thread."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def send(self, message: dict) -> None:
        text = CHILD_TAG + json.dumps(message, default=str)
        with self._lock:
            try:
                self._stream.write(text + "\n")
                self._stream.flush()
            except (OSError, ValueError):
                pass  # the parent has gone; nothing left to tell

    def event(self, kind: str, data: dict) -> None:
        self.send({"t": "event", "kind": kind, "data": data})


class _Recorder:
    """What a PortOwner reports to, carried to the parent instead of to a file."""

    def __init__(self, emit: _Emitter) -> None:
        self._emit = emit

    def event(self, kind: str, **data: Any) -> None:
        self._emit.event(kind, data)


class _OneNode:
    """The slice of the observer a Provisioner uses, for the single node this child owns.

    interface() always answers with what the owner holds right now. After a reboot the
    owner reopens, and anything that kept its own copy of the old interface would read
    config from the cache of a connection that no longer exists.
    """

    def __init__(self, node: devices.BenchNode, recorder: _Recorder) -> None:
        self.node = node
        self.owner = ports.PortOwner(node, recorder)

    def owner_for(self, name: str) -> ports.PortOwner:
        self._check(name)
        devices.assert_commandable(self.node)
        return self.owner

    def interface(self, name: str) -> Any:
        self._check(name)
        devices.assert_commandable(self.node)
        if self.owner.iface is None:
            raise RuntimeError(f"{name} is not connected")
        return self.owner.iface

    def _check(self, name: str) -> None:
        if name != self.node.name:
            raise KeyError(f"this child owns {self.node.name!r}, not {name!r}")


def run_job(
    job: dict,
    emit: _Emitter,
    make_provisioner: Callable[[Any, Callable[[str, dict], None]], Any] | None = None,
) -> dict:
    """Do the one operation the job names. Returns the result message; never raises."""
    op = job.get("op")
    node = devices.BenchNode(**job["node"])
    spec = NodeSpec(**job["spec"]) if job.get("spec") else None
    view = _OneNode(node, _Recorder(emit))
    factory = make_provisioner or (lambda obs, on_event: Provisioner(obs, on_event=on_event))
    provisioner = factory(view, emit.event)
    result: dict[str, Any] = {"t": "result", "op": op, "answered": False}
    try:
        try:
            if op == "provision":
                state = provisioner.provision(node, spec or NodeSpec())
                result.update(state=state.to_dict(), problems=[])
            elif op == "verify":
                state, problems = provisioner.verify(node, spec or NodeSpec())
                result.update(state=state.to_dict(), problems=list(problems))
            elif op == "reboot":
                provisioner.reboot(node)
            else:
                raise ProvisionError(f"unknown operation {op!r}")
        finally:
            # Whether the node was answering when the child finished, so the parent knows
            # whether a full wait to take capture back is worth spending.
            result["answered"] = view.owner.iface is not None
            if result["answered"]:
                # A clean goodbye, for the node's sake. The port is freed by the exit
                # regardless; this only spares the firmware a client that just vanished.
                view.owner.release("child finished", abandon=False)
    except ProvisionError as exc:
        result["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 - reported to the parent, which decides
        result["error"] = f"{node.name}: {op} raised {type(exc).__name__}: {exc}"
    return result


def _forward_logs(emit: _Emitter) -> None:
    """Carry the node's log lines to the parent, which owns the run's capture."""
    try:
        from pubsub import pub
    except Exception:  # noqa: BLE001 - no library, no logs; the operation still runs
        return

    def _on_log(line: str = "", interface: Any = None, **_: Any) -> None:
        emit.send({"t": "log", "line": line})

    _forward_logs.handler = _on_log  # pubsub holds weak references
    pub.subscribe(_on_log, "meshtastic.log.line")


def main() -> None:
    protocol = sys.stdout
    # Anything the client library prints goes to stderr, where the parent keeps a tail
    # for diagnosis, and cannot corrupt the protocol stream.
    sys.stdout = sys.stderr
    emit = _Emitter(protocol)
    code = 0
    try:
        job = json.loads(sys.stdin.read())
        _forward_logs(emit)
        result = run_job(job, emit)
        code = 0 if not result.get("error") else 1
    except Exception as exc:  # noqa: BLE001
        result = {"t": "result", "answered": False,
                  "error": f"child could not run: {type(exc).__name__}: {exc}"}
        code = 2
    emit.send(result)
    try:
        protocol.flush()
        sys.stderr.flush()
    finally:
        os._exit(code)


if __name__ == "__main__":
    main()
