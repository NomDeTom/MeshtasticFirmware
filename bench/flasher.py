"""Stage 2: getting an image onto a node without losing the node.

Entry to DFU is the fragile part, not the transfer. The recovery ladder is ordered by
how much it can hurt, safest first, and the bench climbs it only as far as it must:

  1. Protocol DFU. enterDFUMode() over the API puts this hardware into its UF2
     mass-storage bootloader, then the image is copied to the volume. No baud-rate
     trick, no reconnecting to a port name that may have changed, and it works on a node
     that is ALREADY in DFU - the exact state where a touch cannot help.
  2. 1200-baud touch plus serial DFU. Needed where the protocol path is unavailable.
     Racy: it lands in app mode if the port is still settling, and reports "Target is
     not in DFU mode" after the touch is already spent.
  3. USB power cycle. A wedged node that answers nothing.

The one hard rule: never touch a node that is not answering. A node already in its
bootloader cannot respond, and repeatedly touching it is the most likely way to lose it
for good - which is how 43C2192F2DFEE099 was lost.

PlatformIO's exit code is not evidence. adafruit-nrfutil can fail to program and still
let pio report success, so every upload is checked for the failure strings and, above
all, for the node coming back.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import devices, hardware, platform_probe, proc

# nrfutil prints one of these when a serial-DFU upload fails to program. pio does not
# treat them as errors, so a silent failure otherwise reads as a successful flash.
DFU_FAILURE_MARKERS = (
    "Target is not in DFU mode",
    "No ping response after",
    "Failed to upgrade target",
    "Timeout waiting for acknowledgement",
    "Serial port could not be opened",
)

UF2_SETTLE_S = 90.0
RETURN_TIMEOUT_S = 120.0


class FlashError(RuntimeError):
    pass


class NodeNotAnswering(FlashError):
    """Refused to act because the node could not be confirmed alive first."""


@dataclass
class FlashResult:
    node: str
    method: str
    ok: bool
    detail: str
    duration_s: float

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class Flasher:
    """Flashes one node at a time, climbing the recovery ladder only as needed."""

    def __init__(
        self,
        platform: platform_probe.PlatformInfo,
        on_event: Callable[[str, dict], None] | None = None,
        observer: Any = None,
    ) -> None:
        self.platform = platform
        self.on_event = on_event
        # The observer holds the interfaces. Flashing must tell it to let go, so the
        # disconnection is attributed as a deliberate gap rather than a capture hole.
        self.observer = observer
        # Which board the pending image targets; set by flash() and read by the locked
        # body, so the compatibility check stays next to the code that acts on it.
        self._image_model: str | None = None
        # A live interface handed over by the observer for the duration of a flash.
        self._handed: Any = None

    def _emit(self, kind: str, **data) -> None:
        if self.on_event:
            self.on_event(kind, data)

    # -- the entry point -------------------------------------------------------

    def flash(
        self,
        node: devices.BenchNode,
        image: Path,
        image_hw_model: str | None = None,
    ) -> FlashResult:
        """Put `image` on `node`. Raises FlashError rather than returning a bad state."""
        devices.assert_flashable(node)
        image = Path(image)
        if not image.exists():
            raise FlashError(f"image does not exist: {image}")

        started = time.time()
        self._image_model = image_hw_model
        self._emit("flash_start", node=node.name, image=str(image), serial=node.serial_number)

        # Hold the port for the whole flash. The observer reconnects on a timer, and a
        # reconnect landing mid-DFU takes the port back, leaves the node in app mode and
        # produces "no UF2 volume appeared" - a failure that looks like hardware and is
        # not. Released in the finally below, whatever happens.
        if self.observer is not None:
            self._handed = self.observer.detach(node.name, reason="flash")
        try:
            return self._flash_locked(node, image, started)
        finally:
            if self.observer is not None:
                self.observer.resume(node.name)

    def _flash_locked(self, node, image: Path, started: float) -> FlashResult:
        # One connection for the whole prologue. Liveness, the board check and the DFU
        # command each used to open their own, and each open that times out leaves a
        # thread behind still holding an exclusive port - so the next open contends with
        # the last one and the flash stalls with no error anywhere. Measured: three
        # connect/lose cycles and then nothing for the rest of the run.
        port = devices.try_resolve_port(node.serial_number)
        if port is None:
            raise NodeNotAnswering(
                f"{node.name} ({node.serial_number}) is not enumerated; refusing to act"
            )

        # Prefer the connection the observer just handed over: it is already open to
        # this node, so no port is released and reacquired and nothing can race for it.
        iface = self._handed
        self._handed = None
        if iface is None:
            iface = self._open_with_retry(node, port)
        if iface is None:
            raise NodeNotAnswering(
                f"{node.name} is enumerated on {port} but not answering. It may already "
                "be in its bootloader - touching it again is how nodes are lost. "
                "Power-cycle it and retry."
            )
        try:
            device_model = hardware.model_from_interface(iface)
            self._emit(
                "hw_model_check", node=node.name, device=device_model, image=self._image_model
            )
            hardware.assert_compatible(node.name, device_model, self._image_model)

            before = devices.snapshot_ports()
            self._emit("enter_dfu", node=node.name, port=port)
            # Bounded. The node reboots into its bootloader partway through this call, so
            # the library can be left waiting on a device that is no longer there - and an
            # unbounded call here hangs the whole run with no error and no timeout. The
            # command is fire-and-forget anyway: whether it worked is decided by a
            # bootloader appearing, never by this returning.
            self._call_bounded(
                lambda: iface.localNode.enterDFUMode(),
                timeout=20.0,
                label="enterDFUMode",
                node=node.name,
            )
        finally:
            _close_quietly(iface)

        return self._finish_dfu(node, image, before, started)

    def _call_bounded(self, fn, timeout: float, label: str, node: str) -> bool:
        """Run a call that may never return, and carry on when it does not.

        Anything issued to a node that is about to reboot can block forever inside the
        library. The thread is abandoned rather than joined, which is safe here only
        because the device it is stuck on is going away.
        """
        import threading

        done: dict = {}

        def _go() -> None:
            try:
                fn()
                done["ok"] = True
            except Exception as exc:  # noqa: BLE001 - the node vanishes mid-call
                done["error"] = str(exc)[:140]

        t = threading.Thread(target=_go, daemon=True, name=f"bench-{label}")
        t.start()
        t.join(timeout)
        if "error" in done:
            self._emit(f"{label}_raised", node=node, error=done["error"])
        elif "ok" not in done:
            self._emit(f"{label}_timeout", node=node, after_s=timeout)
        return bool(done.get("ok"))

    def _open_with_retry(self, node, port: str, total: float = 90.0):
        """Open the node, allowing for one that is still coming back up.

        A single bounded open is not liveness. Flashing and provisioning both reboot the
        node, and an open attempted while it is still enumerating times out and reports
        "not answering" about hardware that is merely busy - which then refuses the flash
        and, on a matrix, every row after it. The port is re-resolved each attempt because
        a rebooting node can return on a different one.
        """
        deadline = time.monotonic() + total
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            current = devices.try_resolve_port(node.serial_number) or port
            iface = self._open_once(current, timeout=20.0)
            if iface is not None:
                if attempt > 1:
                    self._emit("node_answered", node=node.name, port=current, attempt=attempt)
                return iface
            self._emit("node_not_ready", node=node.name, port=current, attempt=attempt)
            time.sleep(4.0)
        return None

    def _open_once(self, port: str, timeout: float = 25.0):
        """Open one bounded connection, or None. The thread is abandoned on timeout."""
        import threading

        import meshtastic.serial_interface as si

        out: dict = {}

        def _go() -> None:
            try:
                out["iface"] = si.SerialInterface(devPath=port)
            except Exception as exc:  # noqa: BLE001
                out["error"] = exc

        t = threading.Thread(target=_go, daemon=True, name=f"bench-flash-open-{port}")
        t.start()
        t.join(timeout)
        return out.get("iface")

    def _finish_dfu(self, node, image: Path, before: dict, started: float) -> FlashResult:
        """Wait for whichever DFU interface appears, then transfer and verify."""
        dfu_port = None
        volume = None
        deadline = time.monotonic() + 60.0
        while dfu_port is None and volume is None and time.monotonic() < deadline:
            time.sleep(1.0)
            dfu_port = devices.looks_like_dfu(before)
            volume = platform_probe.find_uf2_volume()

        if dfu_port is not None and image.suffix.lower() == ".zip":
            result = self._serial_dfu_upload(node, image, dfu_port)
        else:
            # Mass storage only, which is what this hardware offers. The node is already
            # in DFU, so finish through the volume rather than abandoning it there.
            uf2 = image if image.suffix.lower() == ".uf2" else _sibling_uf2(image)
            if uf2 is None:
                result = FlashResult(
                    node.name, "dfu", False, "no .uf2 available for a volume flash", 0.0
                )
            else:
                self._emit("dfu_serial_unavailable", node=node.name, falling_back="uf2_volume")
                result = self._copy_uf2_to_volume(node, uf2)

        result.duration_s = round(time.time() - started, 1)
        self._emit("flash_done", **result.to_dict())
        if not result.ok:
            raise FlashError(f"{node.name}: {result.detail}")
        return result

    def _serial_dfu_upload(self, node, image: Path, dfu_port: str) -> FlashResult:
        if self.platform.nrfutil is None:
            return FlashResult(node.name, "serial_dfu", False, "adafruit-nrfutil absent", 0.0)
        argv = [
            *self.platform.nrfutil.argv, "dfu", "serial",
            "--package", str(image), "-p", dfu_port, "-b", "115200", "--singlebank",
        ]
        result = proc.run(argv, env=dict(self.platform.nrfutil.env), timeout=300.0)
        failure = next((m for m in DFU_FAILURE_MARKERS if m in result.output), None)
        if failure or not result.ok:
            return FlashResult(
                node.name, "serial_dfu", False,
                f"nrfutil failed ({failure or result.returncode}): {result.tail(10)}", 0.0)
        if not self._wait_for_return(node):
            return FlashResult(
                node.name, "serial_dfu", False, "flashed but the node did not re-appear", 0.0)
        return FlashResult(node.name, "serial_dfu", True, f"serial DFU on {dfu_port}", 0.0)

    def _unused_flash_locked_tail(self, node, image: Path, started: float) -> FlashResult:

        if image.suffix.lower() == ".zip":
            # nrfutil package: enter DFU over the protocol - no 1200-baud touch, which is
            # the fragile part - then stream the image to the bootloader's CDC instead of
            # writing it to a mass-storage volume mounted over the same USB bus the run
            # is recording to.
            result = self._flash_protocol_then_serial(node, image, port)
        elif image.suffix.lower() == ".uf2":
            result = self._flash_uf2(node, image, port)
        else:
            result = self._flash_serial_dfu(node, image, port)

        result.duration_s = round(time.time() - started, 1)
        self._emit("flash_done", **result.to_dict())
        if not result.ok:
            raise FlashError(f"{node.name}: {result.detail}")
        return result

    def _hw_model(self, node: devices.BenchNode, port: str) -> str | None:
        """The node's hardware model, preferring an interface already held.

        The serial port is exclusive: while the observer holds a node, opening a second
        connection to ask what it is simply fails, the model comes back unknown, and the
        flash is refused for the wrong reason. Ask the held interface instead, and only
        open one when nothing else has it.
        """
        if self.observer is not None:
            held = self.observer.held.get(node.name)
            if held is not None and held.iface is not None:
                model = hardware.model_from_interface(held.iface)
                if model:
                    return model
        return hardware.read_hw_model(port)

    # -- liveness --------------------------------------------------------------

    def _require_alive(self, node: devices.BenchNode) -> str:
        """The node's port, having proved it is enumerated and answering.

        Enumeration alone is not proof: a node sitting in its bootloader enumerates
        perfectly well and cannot be commanded. Where the observer holds an interface we
        take its liveness; otherwise we probe.
        """
        port = devices.try_resolve_port(node.serial_number)
        if port is None:
            raise NodeNotAnswering(
                f"{node.name} ({node.serial_number}) is not enumerated; refusing to act"
            )
        if self.observer is not None:
            held = self.observer.held.get(node.name)
            if held is not None and held.connected:
                return port
        if not self._probe(port):
            raise NodeNotAnswering(
                f"{node.name} is enumerated on {port} but not answering. It may already "
                "be in its bootloader - touching it again is how nodes are lost. "
                "Power-cycle it and retry."
            )
        return port

    def _probe(self, port: str, timeout: float = 25.0) -> bool:
        """One bounded connect, to prove the node speaks the protocol."""
        import threading

        import meshtastic.serial_interface as si

        outcome: dict[str, Any] = {}

        def _try() -> None:
            iface = None
            try:
                iface = si.SerialInterface(devPath=port)
                outcome["ok"] = True
            except Exception as exc:  # noqa: BLE001
                outcome["error"] = exc
            finally:
                if iface is not None:
                    try:
                        iface.close()
                    except Exception:  # noqa: BLE001
                        pass

        t = threading.Thread(target=_try, daemon=True)
        t.start()
        t.join(timeout)
        return bool(outcome.get("ok"))

    def _flash_protocol_then_serial(
        self, node: devices.BenchNode, image: Path, port: str
    ) -> FlashResult:
        """Protocol DFU entry, then an nrfutil serial upload.

        Keeps the safe half of the UF2 path - enterDFUMode() over the API, so no
        1200-baud touch and nothing that can strand a node - while avoiding the
        mass-storage write. The Adafruit nRF52 bootloader serves both interfaces at once,
        so once the node is in DFU either upload works.
        """
        if self.platform.nrfutil is None:
            return FlashResult(node.name, "protocol_serial", False, "adafruit-nrfutil absent", 0.0)
        before = devices.snapshot_ports()
        self._enter_dfu(port)

        # Wait for the bootloader's own CDC to appear, confirmed by an observed
        # transition rather than by a bootloader-shaped PID that may always have been there.
        # Wait for whichever the bootloader offers FIRST - a DFU serial port, or the
        # mass-storage volume. Waiting out the serial timeout before even looking for the
        # volume spent a minute per flash on hardware that was never going to present one.
        dfu_port = None
        volume = None
        deadline = time.monotonic() + 60.0
        while dfu_port is None and volume is None and time.monotonic() < deadline:
            time.sleep(1.0)
            dfu_port = devices.looks_like_dfu(before)
            volume = platform_probe.find_uf2_volume()
        if dfu_port is None:
            # This bootloader offers mass storage only - measured on nice!nano /
            # nRF52840, whose UF2 bootloader presents no DFU CDC at all and re-enumerates
            # with the SAME pid as the application, so there is no transition to observe.
            #
            # The node is already in DFU at this point, so the volume is sitting right
            # there: finish the job rather than returning a failure that leaves it
            # stranded in its bootloader for the next row to refuse to touch.
            self._emit("dfu_serial_unavailable", node=node.name, falling_back="uf2_volume")
            uf2 = _sibling_uf2(image)
            if uf2 is None:
                return FlashResult(
                    node.name, "protocol_serial", False,
                    "no DFU serial port appeared and no .uf2 sits beside the package", 0.0)
            return self._copy_uf2_to_volume(node, uf2)
        self._emit("dfu_confirmed", node=node.name, port=dfu_port)

        argv = [
            *self.platform.nrfutil.argv,
            "dfu", "serial",
            "--package", str(image),
            "-p", dfu_port,
            "-b", "115200",
            "--singlebank",
        ]
        result = proc.run(argv, env=dict(self.platform.nrfutil.env), timeout=300.0)
        failure = next((m for m in DFU_FAILURE_MARKERS if m in result.output), None)
        if failure or not result.ok:
            return FlashResult(
                node.name, "protocol_serial", False,
                f"nrfutil failed ({failure or result.returncode}): {result.tail(10)}", 0.0)
        if not self._wait_for_return(node):
            return FlashResult(
                node.name, "protocol_serial", False,
                "flashed but the node did not re-appear", 0.0)
        return FlashResult(
            node.name, "protocol_serial", True, f"serial DFU on {dfu_port}, no volume write", 0.0)

    # -- path 1: protocol DFU + UF2 volume -------------------------------------

    def _flash_uf2(self, node: devices.BenchNode, image: Path, port: str) -> FlashResult:
        """Preferred path. Commands DFU over the protocol, then copies the .uf2."""
        entered = self._enter_dfu(port)
        if not entered:
            return FlashResult(node.name, "uf2", False, "enterDFUMode did not take", 0.0)

        return self._copy_uf2_to_volume(node, image)

    def _copy_uf2_to_volume(self, node: devices.BenchNode, image: Path) -> FlashResult:
        """Copy a .uf2 onto the bootloader volume, for a node already in DFU."""
        volume = self._wait_for_volume()
        if volume is None:
            return FlashResult(
                node.name,
                "uf2",
                False,
                f"no UF2 volume appeared within {UF2_SETTLE_S:.0f}s of entering DFU",
                0.0,
            )
        self._emit("uf2_volume", node=node.name, volume=str(volume))

        try:
            # Read once, write once. shutil.copy2 streams in small chunks, and when the
            # source is an external USB drive and the destination is a USB bootloader
            # volume on the same bus, the two contend: measured at 177 seconds for 1.5 MB.
            # Buffering the whole image first makes it a single read and a single write -
            # and the image is small enough that holding it in memory costs nothing.
            payload = image.read_bytes()
            target = volume / image.name
            with target.open("wb") as fh:
                fh.write(payload)
                fh.flush()
        except OSError as exc:
            # The bootloader reboots the instant it has the image, so the copy can report
            # a write error on a volume that has already gone. Treat the node coming back
            # as the real evidence, not the copy's return.
            self._emit("uf2_copy_warning", node=node.name, error=str(exc))

        back = self._wait_for_return(node)
        if not back:
            return FlashResult(
                node.name, "uf2", False, "flashed but the node did not re-appear", 0.0
            )
        return FlashResult(node.name, "uf2", True, f"flashed via UF2 volume {volume}", 0.0)

    def _enter_dfu(self, port: str) -> bool:
        import threading

        import meshtastic.serial_interface as si

        done: dict[str, Any] = {}

        def _go() -> None:
            iface = None
            try:
                iface = si.SerialInterface(devPath=port)
                iface.localNode.enterDFUMode()
                done["ok"] = True
            except Exception as exc:  # noqa: BLE001
                # The node reboots into the bootloader mid-call, so the library often
                # raises on the way out of a call that actually worked. The volume
                # appearing is the evidence, not this return.
                done["error"] = exc
            finally:
                if iface is not None:
                    try:
                        iface.close()
                    except Exception:  # noqa: BLE001
                        pass

        t = threading.Thread(target=_go, daemon=True)
        t.start()
        t.join(30.0)
        return True  # confirmed by _wait_for_volume, never by this call's return

    def _wait_for_volume(self, timeout: float = UF2_SETTLE_S) -> Path | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            volume = platform_probe.find_uf2_volume()
            if volume is not None:
                return volume
            time.sleep(2.0)
        return None

    def _wait_for_return(self, node: devices.BenchNode, timeout: float = RETURN_TIMEOUT_S) -> bool:
        """The node re-enumerating and answering is the only proof a flash worked.

        Answering is asked of whoever already holds the port. The observer reconnects on
        its own as soon as the node returns, and the serial port is exclusive - so
        opening a competing connection here fails with a permission error and reports
        "the node did not re-appear" about a node that demonstrably did. Only probe
        directly when nothing else is holding it.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            port = devices.try_resolve_port(node.serial_number)
            if port is not None:
                # While the flash holds the port the observer is suspended and will not
                # reconnect, so asking it whether the node is back would wait forever.
                # We own the port here, so probe it directly; only defer to the observer
                # when it is the one holding the connection.
                owns_port = self.observer is None or self.observer.is_suspended(node.name)
                if owns_port:
                    if self._probe(port, timeout=20.0):
                        self._emit("flash_node_returned", node=node.name, port=port)
                        return True
                else:
                    self.observer.health_tick()
                    held = self.observer.held.get(node.name)
                    if held is not None and held.connected:
                        self._emit("flash_node_returned", node=node.name, port=held.port)
                        return True
            time.sleep(3.0)
        return False

    # -- path 2: 1200-baud touch + serial DFU ----------------------------------

    def _flash_serial_dfu(self, node: devices.BenchNode, image: Path, port: str) -> FlashResult:
        if self.platform.nrfutil is None:
            return FlashResult(
                node.name, "serial_dfu", False, "adafruit-nrfutil is not available", 0.0
            )
        dfu_port = self.touch_1200bps(node, port)
        if dfu_port is None:
            return FlashResult(
                node.name, "serial_dfu", False, "node did not re-enumerate into DFU", 0.0
            )

        argv = [
            *self.platform.nrfutil.argv,
            "--verbose",
            "dfu",
            "serial",
            "--package",
            str(image),
            "-p",
            dfu_port,
            "-b",
            "115200",
            "--singlebank",
        ]
        result = proc.run(argv, env=dict(self.platform.nrfutil.env), timeout=300.0)
        failure = next((m for m in DFU_FAILURE_MARKERS if m in result.output), None)
        if failure or not result.ok:
            return FlashResult(
                node.name,
                "serial_dfu",
                False,
                f"nrfutil failed ({failure or result.returncode}): {result.tail(10)}",
                0.0,
            )
        if not self._wait_for_return(node):
            return FlashResult(
                node.name, "serial_dfu", False, "flashed but the node did not re-appear", 0.0
            )
        return FlashResult(node.name, "serial_dfu", True, f"flashed via serial DFU on {dfu_port}", 0.0)

    def touch_1200bps(self, node: devices.BenchNode, port: str, settle_ms: int = 250) -> str | None:
        """Bounce a node into its bootloader, and confirm it genuinely went.

        Returns the port that appeared AFTER the reset, which is not reliably the one we
        touched. Confirmation requires an observed transition - a new port, or a changed
        PID - because a bootloader-shaped PID on an unchanged port means the node never
        left app mode, and flashing it then fails after the touch is already spent.
        """
        import serial

        before = devices.snapshot_ports()
        self._emit("touch_1200bps", node=node.name, port=port)
        try:
            with serial.Serial(port, 1200) as handle:
                handle.dtr = False
                time.sleep(settle_ms / 1000.0)
        except serial.SerialException:
            pass  # the port vanishing mid-open is the reset happening

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            time.sleep(0.5)
            found = devices.looks_like_dfu(before)
            if found:
                self._emit("dfu_confirmed", node=node.name, port=found)
                return found
        self._emit("dfu_not_confirmed", node=node.name)
        return None

    # -- path 3: power cycle ---------------------------------------------------

    def power_cycle(self, location: str, port_number: int, delay_s: float = 3.0) -> bool:
        """Hard USB power cycle via uhubctl. The rung below a touch.

        Recovers a node that answers nothing without anyone walking to the bench, and
        without the repeated touching that loses nodes.
        """
        if not self.platform.uhubctl:
            self._emit("power_cycle_unavailable", location=location)
            return False
        result = proc.run(
            [
                self.platform.uhubctl,
                "-l",
                location,
                "-p",
                str(port_number),
                "-a",
                "cycle",
                "-d",
                str(delay_s),
            ],
            timeout=60.0,
        )
        self._emit(
            "power_cycle", location=location, port=port_number, ok=result.ok, tail=result.tail(5)
        )
        return result.ok

def _sibling_uf2(package: Path) -> Path | None:
    """The .uf2 built alongside an nrfutil package, if there is one."""
    candidate = package.with_suffix(".uf2")
    return candidate if candidate.exists() else None


def _close_quietly(iface) -> None:
    """Close on a daemon thread and abandon it - close() can block on a rebooting node."""
    import threading

    t = threading.Thread(target=_safe_close_iface, args=(iface,), daemon=True)
    t.start()
    t.join(5.0)


def _safe_close_iface(iface) -> None:
    try:
        iface.close()
    except Exception:  # noqa: BLE001
        pass
