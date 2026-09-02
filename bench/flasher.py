"""Stage 2: getting an image onto a node without losing the node.

Entry to DFU is the fragile part, not the transfer. The ladder is ordered by how much
each rung can hurt, and the bench climbs only as far as it must:

  1. Protocol DFU. enterDFUMode() over the API, then the image goes in over whichever
     interface the bootloader offers - a DFU serial port where one exists, the UF2
     mass-storage volume otherwise. No baud-rate trick, and it works on a node that is
     ALREADY in DFU, which is the exact state where a touch cannot help.
  2. 1200-baud touch plus serial DFU, for boards without the protocol path. Racy: it
     lands in app mode if the port is still settling, and reports "Target is not in DFU
     mode" after the touch is already spent.
  3. USB power cycle, for a node that answers nothing.

Two rules this module exists to keep:

  Never touch a node that is not answering. A node in its bootloader cannot respond, and
  repeatedly touching it is the most likely way to lose it for good.

  Whatever puts a node into DFU is responsible for getting it out again. A flash that
  gives up partway leaves hardware stranded, and every later row then correctly refuses
  to touch it - so one bad flash costs the whole matrix.

Every port comes from the node's PortOwner as an exclusive lease, and nothing here opens
one. An open that times out abandons a thread still holding the port, so two openers on
one device is a race with no winner.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import devices, hardware, platform_probe, ports, proc

# nrfutil prints one of these when a serial-DFU upload fails to program. pio does not
# treat them as errors, so a silent failure otherwise reads as a successful flash.
DFU_FAILURE_MARKERS = (
    "Target is not in DFU mode",
    "No ping response after",
    "Failed to upgrade target",
    "Timeout waiting for acknowledgement",
    "Serial port could not be opened",
)

# Budgets. Each bounds one phase, so a flash has a knowable worst case rather than an
# open-ended one, and a phase that overruns names itself.
PROLOGUE_S = 90.0  # prove the node, check the board, command DFU
DFU_APPEAR_S = 60.0  # wait for a bootloader interface to show up
TRANSFER_S = 300.0  # the image transfer itself
RETURN_S = 180.0  # the node coming back and answering

FLASH_BUDGET_S = PROLOGUE_S + DFU_APPEAR_S + TRANSFER_S + RETURN_S


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
    outcome: str = ports.OK

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class Flasher:
    """Flashes one node at a time, holding an exclusive lease for the whole operation."""

    def __init__(
        self,
        platform: platform_probe.PlatformInfo,
        on_event: Callable[[str, dict], None] | None = None,
        observer: Any = None,
    ) -> None:
        self.platform = platform
        self.on_event = on_event
        self.observer = observer

    def _emit(self, kind: str, **data) -> None:
        if self.on_event:
            self.on_event(kind, data)

    def _owner(self, node: devices.BenchNode) -> ports.PortOwner:
        if self.observer is None:
            raise FlashError("flashing needs an observer to own the device's port")
        return self.observer.owner_for(node.name)

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
        owner = self._owner(node)
        # A node already sitting in its bootloader cannot be commanded into it, and must
        # not be touched to try: that is the state where a touch strands hardware. Finish
        # the flash it is already halfway through instead.
        standing = platform_probe.find_uf2_volume()
        if standing is not None and not _answers_as_application(owner):
            self._emit(
                "flash_start", node=node.name, image=str(image),
                serial=node.serial_number, budget_s=TRANSFER_S + RETURN_S,
                already_in_dfu=str(standing),
            )
            owner.expect_reboot("already in DFU", window_s=TRANSFER_S + RETURN_S)
            uf2 = image if image.suffix.lower() == ".uf2" else _sibling_uf2(image)
            if uf2 is None:
                raise FlashError(f"{node.name} is in DFU and no .uf2 was built")
            result = self._copy_uf2_to_volume(node, owner, uf2, standing)
            result.duration_s = round(time.time() - started, 1)
            self._emit("flash_done", **result.to_dict())
            if not result.ok:
                raise FlashError(f"{node.name}: {result.detail}")
            return result

        self._emit(
            "flash_start", node=node.name, image=str(image),
            serial=node.serial_number, budget_s=FLASH_BUDGET_S,
        )

        before = self._prologue(node, owner, image_hw_model)
        result = self._transfer(node, owner, image, before)
        result.duration_s = round(time.time() - started, 1)
        self._emit("flash_done", **result.to_dict())
        if not result.ok:
            raise FlashError(f"{node.name}: {result.detail}")
        return result

    def _prologue(
        self, node: devices.BenchNode, owner: ports.PortOwner, image_hw_model: str | None
    ) -> dict:
        """Prove the node, check the board and command DFU, all on one leased interface.

        `reboots=True`: the node is going into its bootloader, so the handle is abandoned
        rather than closed on the way out. Closing there blocks on a device that is
        already leaving and keeps the port against everything after it.
        """
        try:
            # The claim has to outlive the lease: the lease ends the moment DFU is
            # commanded, and everything fragile happens after that.
            with owner.lease(
                "flash", budget_s=PROLOGUE_S, reboots=True,
                reboot_window_s=DFU_APPEAR_S + TRANSFER_S + RETURN_S,
            ) as iface:
                device_model = hardware.model_from_interface(iface)
                self._emit(
                    "hw_model_check", node=node.name,
                    device=device_model, image=image_hw_model,
                )
                # Guards the instrument rather than the answer, so it blocks, never warns.
                hardware.assert_compatible(node.name, device_model, image_hw_model)

                before = devices.snapshot_ports()
                before["uf2_volume"] = platform_probe.find_uf2_volume()
                self._emit("enter_dfu", node=node.name, port=owner.port)
                # Bounded: the node reboots partway through this call, so the library can
                # be left waiting on a device that is no longer there.
                outcome, detail = _call_bounded(lambda: iface.localNode.enterDFUMode(), 20.0)
                # Not fatal on its own: the node reboots partway through this call, so a
                # raise is as consistent with success as with failure. But it is the only
                # account of why no bootloader turned up, and discarding it turned a
                # refused DFU request into a silent sixty-second wait.
                self._emit(
                    "enter_dfu_result", node=node.name, outcome=outcome, detail=detail
                )
                return before
        except ports.PortBusy as exc:
            raise NodeNotAnswering(
                f"{node.name} could not be taken for flashing: {exc}. It may already be "
                "in its bootloader - touching it again is how nodes are lost."
            ) from exc

    def _transfer(
        self, node: devices.BenchNode, owner: ports.PortOwner, image: Path, before: dict
    ) -> FlashResult:
        """Wait for whichever DFU interface appears, then put the image through it."""
        budget = ports.Budget(DFU_APPEAR_S)
        # A volume that was already mounted when the flash began belongs to some other
        # device - a node stranded in its bootloader from an earlier row, most likely.
        # Writing this image there would flash the wrong board, so only a volume that
        # APPEARS in response to the command just issued counts as this node's.
        standing = before.get("uf2_volume")
        dfu_port = None
        volume = None
        while dfu_port is None and volume is None and not budget.spent:
            time.sleep(1.0)
            dfu_port = devices.looks_like_dfu(before)
            found = platform_probe.find_uf2_volume()
            volume = found if found is not None and found != standing else None

        if dfu_port is not None and image.suffix.lower() == ".zip":
            return self._serial_dfu_upload(node, owner, image, dfu_port)

        # Mass storage only - which is what nice!nano / nRF52840 offers: no DFU CDC, and
        # it re-enumerates with the same PID as the application, so there is not even a
        # transition to observe. The node is already in DFU here, so finish through the
        # volume rather than abandoning it there.
        uf2 = image if image.suffix.lower() == ".uf2" else _sibling_uf2(image)
        if volume is None:
            return FlashResult(
                node.name, "dfu", False,
                f"no bootloader interface appeared within {DFU_APPEAR_S:.0f}s",
                0.0, ports.TIMED_OUT,
            )
        if uf2 is None:
            return FlashResult(
                node.name, "dfu", False,
                "bootloader offers mass storage only and no .uf2 was built",
                0.0, ports.FAILED,
            )
        self._emit("dfu_via", node=node.name, interface="uf2_volume", volume=str(volume))
        return self._copy_uf2_to_volume(node, owner, uf2, volume)

    # -- transports ------------------------------------------------------------

    def _copy_uf2_to_volume(
        self, node: devices.BenchNode, owner: ports.PortOwner, image: Path, volume: Path
    ) -> FlashResult:
        try:
            # Read once, write once. shutil.copy2 streams in small chunks, and when the
            # source sits on an external USB drive and the destination is a bootloader
            # volume on the same bus the two contend - measured at 177s for 1.5 MB.
            payload = image.read_bytes()
            with (volume / image.name).open("wb") as fh:
                fh.write(payload)
                fh.flush()
            self._emit("uf2_written", node=node.name, bytes=len(payload))
        except OSError as exc:
            # The bootloader reboots the instant it has the image, so the write can fail
            # on a volume that has already gone. The node coming back is the evidence.
            self._emit("uf2_copy_warning", node=node.name, error=str(exc)[:120])

        back = owner.wait_answering(budget_s=RETURN_S)
        if not back.ok:
            return FlashResult(
                node.name, "uf2", False,
                f"flashed but the node did not answer within {RETURN_S:.0f}s",
                0.0, back.outcome,
            )
        return FlashResult(node.name, "uf2", True, f"flashed via {volume}", 0.0, ports.OK)

    def _serial_dfu_upload(
        self, node: devices.BenchNode, owner: ports.PortOwner, image: Path, dfu_port: str
    ) -> FlashResult:
        if self.platform.nrfutil is None:
            return FlashResult(
                node.name, "serial_dfu", False, "adafruit-nrfutil absent", 0.0, ports.FAILED
            )
        self._emit("dfu_via", node=node.name, interface="serial", port=dfu_port)
        argv = [
            *self.platform.nrfutil.argv, "dfu", "serial",
            "--package", str(image), "-p", dfu_port, "-b", "115200", "--singlebank",
        ]
        result = proc.run(argv, env=dict(self.platform.nrfutil.env), timeout=TRANSFER_S)
        failure = next((m for m in DFU_FAILURE_MARKERS if m in result.output), None)
        if failure or not result.ok:
            return FlashResult(
                node.name, "serial_dfu", False,
                f"nrfutil failed ({failure or result.returncode}): {result.tail(10)}",
                0.0, ports.FAILED,
            )
        back = owner.wait_answering(budget_s=RETURN_S)
        if not back.ok:
            return FlashResult(
                node.name, "serial_dfu", False,
                "flashed but the node did not answer", 0.0, back.outcome,
            )
        return FlashResult(
            node.name, "serial_dfu", True, f"serial DFU on {dfu_port}", 0.0, ports.OK
        )

    # -- lower rungs -----------------------------------------------------------

    def touch_1200bps(
        self, node: devices.BenchNode, port: str, settle_ms: int = 250
    ) -> str | None:
        """Bounce a node into its bootloader, and confirm it genuinely went.

        Returns the port that appeared AFTER the reset, which is not reliably the one
        touched. Confirmation needs an observed transition - a new port, or a changed PID
        - because a bootloader-shaped PID on an unchanged port means the node never left
        app mode, and flashing then fails with the touch already spent.
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

        budget = ports.Budget(10.0)
        while not budget.spent:
            time.sleep(0.5)
            found = devices.looks_like_dfu(before)
            if found:
                self._emit("dfu_confirmed", node=node.name, port=found)
                return found
        self._emit("dfu_not_confirmed", node=node.name)
        return None

    def power_cycle(self, location: str, port_number: int, delay_s: float = 3.0) -> bool:
        """Hard USB power cycle via uhubctl - the rung below a touch.

        Recovers a node that answers nothing, without anyone walking to the bench and
        without the repeated touching that loses nodes.
        """
        if not self.platform.uhubctl:
            self._emit("power_cycle_unavailable", location=location)
            return False
        result = proc.run(
            [self.platform.uhubctl, "-l", location, "-p", str(port_number),
             "-a", "cycle", "-d", str(delay_s)],
            timeout=60.0,
        )
        self._emit(
            "power_cycle", location=location, port=port_number,
            ok=result.ok, tail=result.tail(5),
        )
        return result.ok


def _answers_as_application(owner: ports.PortOwner) -> bool:
    """Is this node running its firmware, rather than sitting in its bootloader?

    A mounted UF2 volume says SOME board is in DFU, never which one, and this board keeps
    the same USB PID in both modes - so presence on the bus settles nothing either. The
    only honest test is whether the node answers as a Meshtastic node. Opening the port
    to ask is a read, not a touch: no reset, nothing that could strand it.
    """
    result = owner.hold(budget_s=20.0)
    if result.ok:
        owner.release("dfu attribution check", abandon=False)
        return True
    return False


def _call_bounded(fn: Callable[[], Any], timeout: float) -> tuple[str, str]:
    """Run a call that may never return, and say what became of it.

    Anything issued to a node that is about to reboot can block forever inside the
    library. Abandoning the thread is safe here only because the device it is stuck on is
    going away.

    Returns (outcome, detail) rather than a bool because all three endings mean different
    things and the caller has to record which one happened. A swallowed raise here cost
    a sixty-second wait for a bootloader that was never asked for.
    """
    done: dict = {}

    def _go() -> None:
        try:
            fn()
            done["outcome"] = "returned"
        except Exception as exc:  # noqa: BLE001 - the node vanishes mid-call
            done["outcome"] = "raised"
            done["detail"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=_go, daemon=True, name="bench-bounded-call")
    thread.start()
    thread.join(timeout)
    return done.get("outcome", "hung"), str(done.get("detail", ""))[:200]


def _sibling_uf2(package: Path) -> Path | None:
    """The .uf2 built alongside an nrfutil package, if there is one."""
    candidate = package.with_suffix(".uf2")
    return candidate if candidate.exists() else None
