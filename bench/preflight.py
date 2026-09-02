"""Stage -1: refuse to start a run that cannot produce trustworthy evidence.

Every check here exists because its absence produced a misleading result, a wasted hour,
or a lost node during the beacon validation run. The cost of the whole stage is a few
seconds; the cost of skipping it was measured in hours.

The rule is the spec's: a stage that cannot verify itself must fail, not proceed. A
preflight failure is loud and specific, and names the fix.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from . import devices, hardware, platform_probe

# Severity levels. BLOCK stops the run; WARN is recorded into the artifact and shown by
# the status server, but does not stop anything.
BLOCK = "block"
WARN = "warn"
OK = "ok"


@dataclass
class Check:
    name: str
    status: str  # ok | warn | block
    detail: str
    fix: str | None = None

    @property
    def failed(self) -> bool:
        return self.status == BLOCK

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "fix": self.fix,
        }


@dataclass
class PreflightReport:
    checks: list[Check] = field(default_factory=list)
    platform: dict | None = None
    nodes: list[dict] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(c.failed for c in self.checks)

    @property
    def blockers(self) -> list[Check]:
        return [c for c in self.checks if c.failed]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    def to_dict(self) -> dict:
        return {
            "blocked": self.blocked,
            "platform": self.platform,
            "nodes": self.nodes,
            "checks": [c.to_dict() for c in self.checks],
        }

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    def summary(self) -> str:
        lines = []
        for c in self.checks:
            mark = {OK: "ok  ", WARN: "WARN", BLOCK: "FAIL"}[c.status]
            lines.append(f"  [{mark}] {c.name}: {c.detail}")
            if c.fix and c.status != OK:
                lines.append(f"         fix: {c.fix}")
        verdict = "BLOCKED" if self.blocked else "ready"
        return "\n".join(lines + [f"  -> preflight {verdict}"])


class PreflightFailed(RuntimeError):
    def __init__(self, report: PreflightReport) -> None:
        self.report = report
        blockers = "; ".join(f"{c.name}: {c.detail}" for c in report.blockers)
        super().__init__(f"preflight blocked: {blockers}")


def run_preflight(
    nodes: Sequence[devices.BenchNode] = (),
    firmware_root: Path | None = None,
    require_uhubctl: bool = False,
    require_nrfutil: bool = False,
) -> PreflightReport:
    """Run every stage -1 check. Never raises for a failed check; inspect .blocked."""
    report = PreflightReport()

    # -- platform, and the WSL refusal ------------------------------------------
    try:
        info = platform_probe.probe()
        report.platform = info.to_dict()
        report.checks.append(
            Check(
                "platform",
                OK,
                f"{info.os} {info.release} ({info.machine}), Python {info.python}",
            )
        )
    except platform_probe.UnsupportedPlatform as exc:
        report.platform = {"wsl": True, "os": platform_probe.host_os()}
        report.checks.append(
            Check(
                "platform",
                BLOCK,
                str(exc),
                fix="run the bench from Windows directly, or a native Linux host",
            )
        )
        return report  # nothing below is meaningful on a refused platform

    # -- firmware tree ----------------------------------------------------------
    root = firmware_root or Path.cwd()
    if (root / "platformio.ini").exists():
        report.checks.append(Check("firmware_tree", OK, str(root)))
    else:
        report.checks.append(
            Check(
                "firmware_tree",
                BLOCK,
                f"no platformio.ini under {root}",
                fix="run from the firmware checkout, or pass --firmware-root",
            )
        )

    # -- toolchain --------------------------------------------------------------
    if info.pio:
        report.checks.append(Check("pio", OK, info.pio))
    else:
        report.checks.append(
            Check(
                "pio",
                BLOCK,
                "PlatformIO not found on PATH or in ~/.platformio/penv",
                fix="install PlatformIO Core, or add its penv Scripts dir to PATH",
            )
        )

    if info.nrfutil:
        report.checks.append(Check("nrfutil", OK, " ".join(info.nrfutil.argv)))
    else:
        report.checks.append(
            Check(
                "nrfutil",
                BLOCK if require_nrfutil else WARN,
                "adafruit-nrfutil absent - serial DFU unavailable",
                fix="UF2 volume flashing still works and is the preferred path anyway",
            )
        )

    if info.uhubctl:
        report.checks.append(Check("uhubctl", OK, info.uhubctl))
    else:
        report.checks.append(
            Check(
                "uhubctl",
                BLOCK if require_uhubctl else WARN,
                "uhubctl absent - no hard USB power-cycle recovery",
                fix="install uhubctl to recover a wedged node without unplugging it",
            )
        )

    # -- UF2 mount discovery ----------------------------------------------------
    # A volume is only mounted while some node sits in its bootloader, so its absence
    # now is expected and not a fault. What we assert is that the discovery mechanism
    # can run at all - on Linux that means a media root exists to look in.
    roots = platform_probe._uf2_candidate_roots()
    if roots:
        where = info.uf2_volume or f"{len(roots)} candidate mount points"
        report.checks.append(Check("uf2_discovery", OK, where))
    else:
        report.checks.append(
            Check(
                "uf2_discovery",
                WARN,
                "no candidate UF2 mount points; the recovery flash path may not work",
                fix="confirm removable media automounts on this host",
            )
        )

    # -- nodes ------------------------------------------------------------------
    report.nodes = devices.describe(nodes)
    _check_nodes(report, nodes)

    return report



def _listen_briefly(port: str, seconds: float = 5.0) -> int:
    """Bytes a node emits on its own, without being asked anything.

    Read-only by construction: opening the port at the console baud and reading is not a
    protobuf touch, so it cannot silence the very output it is checking for.
    """
    import time

    try:
        import serial

        with serial.Serial(port, 115200, timeout=1) as handle:
            deadline = time.monotonic() + seconds
            total = 0
            while time.monotonic() < deadline:
                total += len(handle.read(512))
            return total
    except Exception:  # noqa: BLE001 - an unreadable port is simply "heard nothing"
        return 0

def _check_nodes(report: PreflightReport, nodes: Iterable[devices.BenchNode]) -> None:
    nodes = list(nodes)
    if not nodes:
        report.checks.append(
            Check("nodes", WARN, "no nodes configured; build-only run")
        )
        return

    missing = [n for n in nodes if not n.present()]
    if missing:
        names = ", ".join(f"{n.name}({n.serial_number})" for n in missing)
        report.checks.append(
            Check(
                "nodes_present",
                BLOCK,
                f"not enumerated: {names}",
                fix="plug the node in, or correct its serial in the node table",
            )
        )
    else:
        found = ", ".join(f"{n.name}={n.resolve()}" for n in nodes)
        report.checks.append(Check("nodes_present", OK, found))

    # Duplicate serials silently alias two roles onto one physical node, which produces
    # a green row for a test that never ran on the hardware it claims.
    seen: dict[str, str] = {}
    dupes: list[str] = []
    for n in nodes:
        key = n.serial_number.upper()
        if key in seen:
            dupes.append(f"{n.name} and {seen[key]} share serial {n.serial_number}")
        seen[key] = n.name
    if dupes:
        report.checks.append(
            Check("nodes_distinct", BLOCK, "; ".join(dupes), fix="correct the node table")
        )
    else:
        report.checks.append(Check("nodes_distinct", OK, f"{len(nodes)} distinct nodes"))

    # Three is the floor, for two independent reasons: a simultaneous negative control
    # needs a second receiver, and any channel-sensing test needs occupier + DUT +
    # unperturbed witness. Below that, silence and inattention are indistinguishable.
    if len(nodes) < 3:
        report.checks.append(
            Check(
                "node_count",
                WARN,
                f"{len(nodes)} nodes; 3 is the floor for a negative control",
                fix="add a third node before trusting any NOT OBSERVED verdict",
            )
        )
    else:
        report.checks.append(Check("node_count", OK, f"{len(nodes)} nodes"))

    # Board identity, checked against the device rather than the table. A node table is
    # hand-written and is exactly the thing that gets a board wrong; on this bench it
    # named a HELTEC_MESH_POCKET as a promicro peer, and nothing downstream would have
    # stopped the promicro image being written to it. That is a destroyed node, not a
    # failed row, so it is checked before any hardware time is spent.
    mismatched, unknown = [], []
    for node in nodes:
        if node.never_flash or not node.board or not node.present():
            continue
        actual = hardware.read_hw_model(node.resolve())
        if actual is None:
            unknown.append(node.name)
        elif hardware.normalize(actual) != hardware.normalize(node.board):
            mismatched.append(f"{node.name} is a {actual}, table says {node.board}")
    if mismatched:
        report.checks.append(
            Check(
                "node_boards",
                BLOCK,
                "; ".join(mismatched),
                fix="correct the node table before flashing - a wrong image destroys the node",
            )
        )
    elif unknown:
        report.checks.append(
            Check(
                "node_boards",
                WARN,
                f"could not read a hardware model from: {', '.join(unknown)}",
                fix="the flasher will refuse these rather than risk a mismatched image",
            )
        )
    else:
        report.checks.append(
            Check("node_boards", OK, "every flashable node matches its declared board")
        )

    # A passive observer that emits nothing witnesses nothing. Its whole value is being
    # the one instrument that sees the air with no client attached - but that depends on
    # its console output actually being on, and a stock node may have it off entirely.
    # Measured here: the bench's Heltec produced zero bytes in twenty seconds, so every
    # row asserting on its silence would have scored NOT OBSERVED for the life of the run.
    # Caught in ten seconds now, rather than after hours of capture.
    for node in nodes:
        if node.role != "observer" or not node.present():
            continue
        heard = _listen_briefly(node.resolve())
        if heard > 0:
            report.checks.append(
                Check("observer_output", OK, f"{node.name} emitted {heard} bytes in 5s")
            )
        else:
            report.checks.append(
                Check(
                    "observer_output",
                    WARN,
                    f"{node.name} emitted nothing on serial; it cannot witness anything",
                    fix=(
                        "enable console logging on it once, before the run, or use a node "
                        "that logs as the observer - a silent witness makes every claim "
                        "about what it heard unprovable"
                    ),
                )
            )

    roles = {n.role for n in nodes}
    if "observer" not in roles:
        report.checks.append(
            Check(
                "observer_role",
                WARN,
                "no passive observer; RF claims rest on perturbed endpoints only",
                fix="add a never-commanded stock node as role=observer",
            )
        )
    else:
        report.checks.append(Check("observer_role", OK, "passive observer present"))
