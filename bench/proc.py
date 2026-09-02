"""Subprocess execution with the encoding traps already sprung.

Two Windows-specific failures cost real time on the beacon run, and both look like
hardware faults rather than encoding bugs:

  * A child that prints a node's long_name hits the console's cp1252 codec on any emoji
    and dies with UnicodeEncodeError - AFTER connecting successfully. The exit code is 1
    and the output is empty, which reads exactly like "the node is not answering".
  * text=True decodes the child's output with the PARENT's default codec. Having forced
    the child to emit UTF-8, that then raises inside subprocess's reader thread and
    leaves stdout as None, so the caller sees a successful run with no output.

So every child gets UTF-8 forced in both directions, explicitly, and decode errors are
replaced rather than raised. Losing one character is always better than losing the run.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


@dataclass
class Result:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def output(self) -> str:
        return (self.stdout or "") + (self.stderr or "")

    def tail(self, lines: int = 25) -> str:
        return "\n".join(self.output.strip().splitlines()[-lines:])


def run(
    argv: Sequence[str],
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 600.0,
) -> Result:
    """Run a child with UTF-8 forced both ways. Never raises on a non-zero exit."""
    child_env = dict(os.environ)
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    child_env.setdefault("PYTHONUTF8", "1")
    if env:
        child_env.update(env)

    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(argv),
            cwd=str(cwd) if cwd else None,
            env=child_env,
            capture_output=True,
            timeout=timeout,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return Result(
            argv=list(argv),
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            duration_s=round(time.monotonic() - started, 2),
        )
    except subprocess.TimeoutExpired as exc:
        return Result(
            argv=list(argv),
            returncode=124,
            stdout=_as_text(exc.stdout),
            stderr=_as_text(exc.stderr),
            duration_s=round(time.monotonic() - started, 2),
            timed_out=True,
        )
    except OSError as exc:
        return Result(
            argv=list(argv),
            returncode=127,
            stdout="",
            stderr=f"{type(exc).__name__}: {exc}",
            duration_s=round(time.monotonic() - started, 2),
        )


def stream(
    argv: Sequence[str],
    log_path: Path,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 3600.0,
    on_line: "Callable[[str], None] | None" = None,
) -> Result:
    """Run a child, writing its output to `log_path` line by line as it arrives.

    A 29-minute build that reveals nothing until it finishes is indistinguishable from a
    hung one, which is exactly the ambiguity the status server exists to remove. Writing
    progressively lets the page tail the build while it runs, and leaves the output on
    disk if the run is killed - `run()` would lose it entirely.
    """
    child_env = dict(os.environ)
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    child_env.setdefault("PYTHONUTF8", "1")
    if env:
        child_env.update(env)

    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    collected: list[str] = []

    try:
        # stderr folded into stdout so the log reads in the order things happened.
        with subprocess.Popen(
            list(argv),
            cwd=str(cwd) if cwd else None,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        ) as proc_handle, log_path.open("w", encoding="utf-8", buffering=1) as log:
            assert proc_handle.stdout is not None
            for line in proc_handle.stdout:
                log.write(line)
                collected.append(line)
                if on_line is not None:
                    try:
                        on_line(line.rstrip("\n"))
                    except Exception:  # noqa: BLE001 - a progress hook cannot kill a build
                        pass
                if time.monotonic() - started > timeout:
                    proc_handle.kill()
                    return Result(
                        argv=list(argv),
                        returncode=124,
                        stdout="".join(collected),
                        stderr="",
                        duration_s=round(time.monotonic() - started, 2),
                        timed_out=True,
                    )
            code = proc_handle.wait()
    except OSError as exc:
        return Result(
            argv=list(argv),
            returncode=127,
            stdout="".join(collected),
            stderr=f"{type(exc).__name__}: {exc}",
            duration_s=round(time.monotonic() - started, 2),
        )

    return Result(
        argv=list(argv),
        returncode=code,
        stdout="".join(collected),
        stderr="",
        duration_s=round(time.monotonic() - started, 2),
    )


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
