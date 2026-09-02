"""USB device enumeration, and addressing nodes by serial rather than by port.

Ports are not stable. Across the beacon run a node moved COM5 -> COM18 and changed both
its PID and its USB serial number during a bootloader excursion. Every bench operation
therefore names a node by its USB serial and re-resolves the port immediately before
use; a resolved port is never cached across an operation that can reboot the device.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable

import serial.tools.list_ports as list_ports

# USB vendors that ship Meshtastic-capable hardware. Used to keep enumeration quiet on a
# machine full of other serial devices; pass all_devices=True to bypass it.
KNOWN_VIDS = {
    0x239A,  # Adafruit (nRF52840 boards + their UF2 bootloader)
    0x303A,  # Espressif
    0x10C4,  # Silicon Labs CP210x
    0x1A86,  # WCH CH340 / CH9102
    0x0483,  # STMicroelectronics
    0x2E8A,  # Raspberry Pi (RP2040 / RP2350)
    0x1209,  # pid.codes (community boards)
}

# Adafruit nRF52 UF2 bootloader PIDs. Presence of one is suggestive of DFU but is NOT
# sufficient alone - some application builds enumerate with a colliding PID. Confirm
# with looks_like_dfu(), which requires an observed transition.
NRF52_BOOTLOADER_PIDS = {0x0029, 0x002A}


class NodeNotFound(RuntimeError):
    """No USB device with the requested serial is currently enumerated."""


class CommandRefused(RuntimeError):
    """An operation would touch a node whose role forbids it."""


@dataclass(frozen=True)
class UsbDevice:
    port: str
    vid: int | None
    pid: int | None
    serial_number: str | None
    description: str | None

    @property
    def vid_hex(self) -> str | None:
        return f"0x{self.vid:04x}" if self.vid is not None else None

    @property
    def pid_hex(self) -> str | None:
        return f"0x{self.pid:04x}" if self.pid is not None else None

    def to_dict(self) -> dict:
        return {
            "port": self.port,
            "vid": self.vid_hex,
            "pid": self.pid_hex,
            "serial_number": self.serial_number,
            "description": self.description,
        }


def enumerate_devices(all_devices: bool = False) -> list[UsbDevice]:
    """Every serial device present now."""
    out: list[UsbDevice] = []
    for info in list_ports.comports():
        if not all_devices and info.vid is not None and info.vid not in KNOWN_VIDS:
            continue
        out.append(
            UsbDevice(
                port=info.device,
                vid=info.vid,
                pid=info.pid,
                serial_number=info.serial_number or None,
                description=info.description or None,
            )
        )
    return out


def resolve_port(serial_number: str, *, all_devices: bool = True) -> str:
    """Current port for a USB serial number. Raises NodeNotFound if absent.

    Always call this immediately before touching a node. A port resolved before a
    reboot, a flash, or a DFU excursion is stale by the time the device returns.
    """
    for dev in enumerate_devices(all_devices=all_devices):
        if dev.serial_number and dev.serial_number.upper() == serial_number.upper():
            return dev.port
    raise NodeNotFound(f"no USB device with serial {serial_number!r} is enumerated")


def try_resolve_port(serial_number: str) -> str | None:
    try:
        return resolve_port(serial_number)
    except NodeNotFound:
        return None


def wait_for_port(serial_number: str, timeout: float = 90.0, poll: float = 1.0) -> str:
    """Block until a serial number enumerates, or raise.

    Used after a flash or reboot: the device vanishes from USB and may return on a
    different port name. Waiting on the serial rather than the port is the whole point.
    """
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return resolve_port(serial_number)
        except NodeNotFound as exc:
            last = exc
            time.sleep(poll)
    raise NodeNotFound(
        f"serial {serial_number!r} did not enumerate within {timeout:.0f}s"
    ) from last


def wait_for_absence(serial_number: str, timeout: float = 30.0, poll: float = 0.5) -> bool:
    """Block until a serial number disappears. True if it went, False on timeout.

    A DFU touch is only confirmed by the device leaving; polling for its return without
    first seeing it go can match the pre-touch enumeration and report a false success.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if try_resolve_port(serial_number) is None:
            return True
        time.sleep(poll)
    return False


def snapshot_ports() -> dict[str, tuple[int | None, int | None]]:
    """Map of port to (vid, pid), for before/after DFU comparison."""
    return {d.port: (d.vid, d.pid) for d in enumerate_devices(all_devices=True)}


def looks_like_dfu(
    before: dict[str, tuple[int | None, int | None]],
    after: dict[str, tuple[int | None, int | None]] | None = None,
) -> str | None:
    """Port that genuinely re-enumerated into a bootloader, or None.

    Requires an observed transition - a port that did not exist before, or one whose PID
    changed - rather than trusting a bootloader-shaped PID on its own. Some application
    builds enumerate with a PID that collides with a bootloader's, and treating that as
    DFU spends the touch on a node in app mode and then fails with "Target is not in DFU
    mode".
    """
    after = snapshot_ports() if after is None else after
    for port, (_vid, pid) in after.items():
        if port not in before:
            return port  # brand-new port: a genuine re-enumeration
        old_pid = before[port][1]
        if old_pid is not None and pid is not None and old_pid != pid:
            return port
    return None


@dataclass
class BenchNode:
    """One physical node and the policy attached to its role.

    never_command is load-bearing rather than advisory. The stock observer is the only
    instrument that sees the air with no client attached, and it keeps that property
    only while nothing ever opens a protobuf session against it - one stray device_info
    silences its plain-text logging until reboot and destroys its value for the rest of
    the session. The flag is enforced in the observer and the provisioner, not left to
    the operator to remember.
    """

    name: str
    serial_number: str
    role: str  # dut | peer | observer | exciter
    board: str | None = None
    never_command: bool = False
    never_flash: bool = False
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.role == "observer":
            # An observer that is commanded or reflashed is not an observer.
            self.never_command = True
            self.never_flash = True

    def resolve(self) -> str:
        return resolve_port(self.serial_number)

    def present(self) -> bool:
        return try_resolve_port(self.serial_number) is not None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "serial_number": self.serial_number,
            "role": self.role,
            "board": self.board,
            "never_command": self.never_command,
            "never_flash": self.never_flash,
            "port": try_resolve_port(self.serial_number),
            "notes": list(self.notes),
        }


def assert_commandable(node: BenchNode) -> None:
    if node.never_command:
        raise CommandRefused(
            f"{node.name} ({node.role}) is marked never_command: opening a protobuf "
            "session would silence its plain-text logging until reboot and void its "
            "value as an unperturbed witness"
        )


def assert_flashable(node: BenchNode) -> None:
    if node.never_flash:
        raise CommandRefused(
            f"{node.name} ({node.role}) is marked never_flash: it must stay on stock "
            "firmware to be a valid naive-client instrument"
        )


def describe(nodes: Iterable[BenchNode]) -> list[dict]:
    return [n.to_dict() for n in nodes]
