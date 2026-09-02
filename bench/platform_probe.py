"""Platform discovery and the WSL refusal.

Two abstractions only, per the plan's "define Windows, cope with Linux, refuse WSL":

  * UF2 bootloader volume discovery - a labelled drive letter on Windows, a mount
    under /media or /run/media on Linux.
  * Toolchain location - pio, adafruit-nrfutil and uhubctl are rarely on PATH.

Everything discovered here is recorded into the run artifact, so a capture can say
which machine produced it. That is the "cope" half of the rule: a difference that only
changes how we reach the device is discovered and written down, never hardcoded.

WSL is refused outright rather than coped with. It has no native USB; serial requires a
usbipd-win attach from an elevated Windows shell and detaches on every DFU
re-enumeration, and USB mass storage does not pass through usbipd at all - which removes
the UF2 recovery path entirely. A bench whose recovery path silently does not exist is
how you lose a second node.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

WINDOWS = "windows"
LINUX = "linux"
DARWIN = "darwin"


class UnsupportedPlatform(RuntimeError):
    """Raised when the bench refuses to run here at all."""


def is_wsl() -> bool:
    """True under WSL 1 or 2.

    Two independent signals because neither alone is reliable: WSL_DISTRO_NAME is unset
    for some non-interactive invocations, and /proc/version is absent on WSL 1 under
    certain kernels. Either one is enough to refuse.
    """
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        with open("/proc/version", encoding="utf-8", errors="replace") as fh:
            return "microsoft" in fh.read().lower()
    except OSError:
        return False


def host_os() -> str:
    if sys.platform.startswith("win"):
        return WINDOWS
    if sys.platform.startswith("linux"):
        return LINUX
    if sys.platform == "darwin":
        return DARWIN
    return sys.platform


def find_uf2_volume() -> Path | None:
    """The mounted UF2 bootloader volume, identified by INFO_UF2.TXT.

    Identified by content, never by volume label: the label varies per board
    (NRF52BOOT, FTHR840BOOT, WM1110BOOT ...) while INFO_UF2.TXT is mandated by the
    UF2 spec and is present on every one of them.
    """
    for root in _uf2_candidate_roots():
        try:
            if (root / "INFO_UF2.TXT").exists():
                return root
        except OSError:
            continue  # unreadable drive letter, disconnected mount - both expected
    return None


def _uf2_candidate_roots() -> list[Path]:
    if host_os() == WINDOWS:
        # A: and B: are floppy letters and C: is the system disk; a bootloader never
        # lands on any of them.
        return [Path(f"{c}:/") for c in "DEFGHIJKLMNOPQRSTUVWXYZ"]
    roots: list[Path] = []
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    for base in (f"/media/{user}", "/media", f"/run/media/{user}", "/mnt", "/Volumes"):
        p = Path(base)
        try:
            if p.is_dir():
                roots.extend(sorted(c for c in p.iterdir() if c.is_dir()))
        except OSError:
            continue
    return roots


@dataclass(frozen=True)
class NrfutilTool:
    """How to invoke adafruit-nrfutil here: argv prefix plus any env it needs."""

    argv: list[str]
    env: dict[str, str]

    def as_dict(self) -> dict:
        return {"argv": list(self.argv), "env": dict(self.env)}


def find_pio() -> str | None:
    """PlatformIO, which is almost never on PATH on Windows."""
    found = shutil.which("pio") or shutil.which("platformio")
    if found:
        return found
    candidates = [
        Path.home() / ".platformio" / "penv" / "Scripts" / "pio.exe",
        Path.home() / ".platformio" / "penv" / "bin" / "pio",
    ]
    return next((str(c) for c in candidates if c.exists()), None)


def find_nrfutil() -> NrfutilTool | None:
    """adafruit-nrfutil, shipped as a pio package rather than installed globally.

    The pio package is not an executable: it is a bare `adafruit-nrfutil.py` beside a
    vendored `site-packages`, so it only runs as `python adafruit-nrfutil.py` with that
    directory on PYTHONPATH. Returning a runnable argv prefix plus the env it needs
    keeps that detail here instead of leaking into the flasher.
    """
    found = shutil.which("adafruit-nrfutil")
    if found:
        return NrfutilTool(argv=[found], env={})

    pkg = Path.home() / ".platformio" / "packages" / "tool-adafruit-nrfutil"
    script = pkg / "adafruit-nrfutil.py"
    if script.exists():
        env = {}
        vendored = pkg / "site-packages"
        if vendored.is_dir():
            env["PYTHONPATH"] = str(vendored)
        return NrfutilTool(argv=[sys.executable, str(script)], env=env)
    return None


def find_uhubctl() -> str | None:
    """uhubctl drives a hard USB power cycle - the recovery rung below a DFU touch."""
    return shutil.which("uhubctl")


@dataclass
class PlatformInfo:
    """Everything discovered about this host, recorded into the run artifact."""

    os: str
    release: str
    machine: str
    python: str
    wsl: bool
    pio: str | None = None
    nrfutil: NrfutilTool | None = None
    uhubctl: str | None = None
    uf2_volume: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "os": self.os,
            "release": self.release,
            "machine": self.machine,
            "python": self.python,
            "wsl": self.wsl,
            "pio": self.pio,
            "nrfutil": self.nrfutil.as_dict() if self.nrfutil else None,
            "uhubctl": self.uhubctl,
            "uf2_volume": self.uf2_volume,
            "notes": list(self.notes),
        }


def probe(*, refuse_wsl: bool = True) -> PlatformInfo:
    """Discover the host. Raises UnsupportedPlatform under WSL.

    `refuse_wsl=False` exists only so the unit tests can exercise the probe on a
    hypothetical WSL host; no bench entry point passes it.
    """
    wsl = is_wsl()
    if wsl and refuse_wsl:
        raise UnsupportedPlatform(
            "WSL is not supported and will not be coped with. Serial devices reach WSL "
            "only through a usbipd-win attach that detaches on every DFU "
            "re-enumeration, and USB mass storage does not pass through usbipd at all - "
            "so the UF2 recovery path does not exist here. Run the bench from Windows "
            "directly, or from a native Linux host."
        )

    info = PlatformInfo(
        os=host_os(),
        release=platform.release(),
        machine=platform.machine(),
        python=platform.python_version(),
        wsl=wsl,
        pio=find_pio(),
        nrfutil=find_nrfutil(),
        uhubctl=find_uhubctl(),
    )
    vol = find_uf2_volume()
    info.uf2_volume = str(vol) if vol else None

    if info.os not in (WINDOWS, LINUX):
        info.notes.append(f"{info.os} is untested; Windows is the defined platform")
    if info.uhubctl is None:
        info.notes.append("uhubctl absent - hard USB power-cycle recovery unavailable")
    if info.nrfutil is None:
        info.notes.append("adafruit-nrfutil absent - serial DFU unavailable, UF2 only")
    return info


def bus_inventory(timeout: float = 20.0) -> dict:
    """What is actually on the USB bus right now, and what state each device is in.

    Preflight used to assert that discovery COULD run and then say nothing about what it
    found, so a blocked run reported a missing node with no account of the bus it went
    missing from - and diagnosing it meant running these enumerations by hand.

    Three views, because each answers a question the others cannot:

      Serial ports name the devices a run can talk to.

      Mounted volumes name the devices it cannot: a node in its bootloader answers no
      protobuf, and its drive letter is the only sign it is there at all.

      The kernel's own device list distinguishes ABSENT from REMEMBERED. Windows keeps
      an entry for every device ever plugged into a port, so "COM16 exists" in the
      registry says nothing about whether anything is on the end of it.

    It also settles a question this board makes hard to ask. A nice!nano keeps the same
    USB PID in its bootloader as in its application, so a PID cannot tell the two apart -
    but a bootloader exposes mass storage alongside its serial interface, and the
    application does not. A device presenting both is in DFU.
    """
    out: dict = {"serial": [], "volumes": [], "devices": [], "in_bootloader": []}

    try:
        import serial.tools.list_ports as list_ports

        for port in list_ports.comports():
            out["serial"].append({
                "port": port.device,
                "vid": f"{port.vid:04x}" if port.vid else None,
                "pid": f"{port.pid:04x}" if port.pid else None,
                "serial_number": port.serial_number,
                "description": port.description,
            })
    except Exception as exc:  # noqa: BLE001 - an unreadable bus is a finding, not a crash
        out["serial_error"] = f"{type(exc).__name__}: {exc}"

    for root in _uf2_candidate_roots():
        try:
            if (root / "INFO_UF2.TXT").exists():
                out["volumes"].append({"path": str(root), "uf2": True})
        except OSError:
            continue

    if host_os() == WINDOWS:
        out.update(_windows_bus(timeout))
    return out


def _windows_bus(timeout: float) -> dict:
    """Present-only USB devices, and which of them are in a bootloader.

    Status OK means the device is on the bus now; every other status is an entry Windows
    remembers for a port nothing is plugged into.
    """
    import json as _json
    import subprocess

    script = (
        "Get-PnpDevice -Class Ports,USB -Status OK -ErrorAction SilentlyContinue | "
        "Select-Object FriendlyName,InstanceId,Class | ConvertTo-Json -Compress"
    )
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
        )
        parsed = _json.loads(done.stdout or "[]")
    except Exception as exc:  # noqa: BLE001
        return {"devices_error": f"{type(exc).__name__}: {exc}"}
    if isinstance(parsed, dict):
        parsed = [parsed]

    devices, by_instance = [], {}
    for entry in parsed:
        instance = str(entry.get("InstanceId") or "")
        name = str(entry.get("FriendlyName") or "")
        devices.append({"name": name, "instance": instance})
        # The trailing token is shared by every interface of one physical device, so it
        # is what groups a composite device's serial and mass-storage halves together.
        parts = instance.rsplit("\\", 1)
        if len(parts) == 2:
            by_instance.setdefault(parts[1].rsplit("&", 1)[0], []).append(name)

    in_bootloader = []
    for key, names in by_instance.items():
        joined = " ".join(names).lower()
        if "mass storage" in joined and ("serial" in joined or "com" in joined):
            in_bootloader.append({"instance": key, "interfaces": names})
    return {"devices": devices, "in_bootloader": in_bootloader}
