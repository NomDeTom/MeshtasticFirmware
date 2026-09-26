"""Stage 3: put a node into a known state, and prove it is in that state.

The ordering below is load-bearing and every rule in it was learned by having it break.
It is not a checklist of good practice; each step is where it is because putting it
anywhere else produced a node that looked configured and was not.

  1. Wait for the node after flashing. A DFU flash reboots into new firmware and the
     first command must not race it.
  2. Factory reset. userPrefs are FACTORY DEFAULTS - a node with saved config ignores
     them entirely, so a bake's settings never take effect without this.
  3. Region and modem preset. A reset leaves region UNSET, which DISABLES LoRa TX. A
     node in this state beacons nothing and looks like a firmware fault.
  4. Diagnostic flags. A reset wipes security.debug_log_api_enabled; without it capture
     falls back to raw text and cannot be combined with commanding.
  5. Reboot, to commit config to NVS.
  6. Channels LAST. Written before a reboot they are acknowledged and then lost.
  7. Verify on device. Poll, re-apply once, then fail.

Two general rules fall out of that list. Wait for readiness after EVERY step, not just
the obvious restarts - a security-config write reboots the node, and the next command
then reports success while its write is silently discarded. And the write is
acknowledged BEFORE it reaches NVS, so an immediate read-back can miss it: poll, then
re-apply, then fail.

The settled-state block this produces is emitted into the capture as a preamble on every
node, so a log is self-describing and a hollow pass - a node beaconing happily with
pskLen 1 and an empty channel name - cannot be mistaken for a real one.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable

from . import devices

# Generous: a node re-enumerating after a config reboot can take well over a
# minute on this host, and a tight bound turns slow hardware into a failed row.
READY_TIMEOUT_S = 180.0

# Worst case for one node's full prep: reset, several writes and two reboots,
# each of which can wait out a readiness budget. Quoted by the run schedule.
# The phases, named once. The run schedule plans against this list and the provisioner
# reports against it, so neither can drift into calling the same work something else.
RESET, REGION, LORA, ROLE, FLAGS, REBOOT, CHANNELS, VERIFY = (
    "factory reset",
    "region + preset",
    "other lora fields",
    "device role",
    "diagnostic flags",
    "reboot to commit",
    "channels",
    "read back + verify",
)
PHASES = (
    (RESET, 120.0),
    (REGION, 45.0),
    (LORA, 45.0),
    (ROLE, 45.0),
    (FLAGS, 45.0),
    (REBOOT, 60.0),
    (CHANNELS, 30.0),
    (VERIFY, 30.0),
)

PROVISION_BUDGET_S = 420.0
SETTLE_AFTER_WRITE_S = 2.0
VERIFY_POLL_S = 2.0
VERIFY_ATTEMPTS = 6


class ProvisionError(RuntimeError):
    """The node could not be put into, or proved to be in, the required state."""


@dataclass
class NodeSpec:
    """What a scenario wants a node configured to.

    Deliberately small. Anything a scenario needs beyond this belongs in its own setup
    step, so that this block stays the thing every row can be checked against.
    """

    region: str | None = None
    modem_preset: str | None = None
    role: str | None = None
    channel_url: str | None = None
    long_name: str | None = None
    short_name: str | None = None
    debug_log_api: bool = True
    extra_config: dict[str, Any] = field(
        default_factory=dict
    )  # "lora.tx_enabled" -> value

    def to_dict(self) -> dict:
        return {
            "region": self.region,
            "modem_preset": self.modem_preset,
            "role": self.role,
            "channel_url": "<set>" if self.channel_url else None,
            "long_name": self.long_name,
            "short_name": self.short_name,
            "debug_log_api": self.debug_log_api,
            "extra_config": dict(self.extra_config),
        }


@dataclass
class SettledState:
    """What the device says it is, read back from the device itself."""

    node: str
    serial_number: str
    port: str | None
    node_id: str | None
    node_num: int | None
    firmware_version: str | None
    build_tag: str | None
    region: str | None
    modem_preset: str | None
    role: str | None
    channels: list[dict] = field(default_factory=list)
    tx_enabled: bool | None = None
    # Observed values for every spec.extra_config key, read from the device.
    extra_config: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @property
    def ok(self) -> bool:
        return not self.errors


class _Phase:
    """Reports which planned phase a node's provisioning is in, and how it ended.

    A context manager so a phase cannot be left open by an exception: the schedule
    showing work as still running when it failed is the same lie as showing it as never
    started. Phases the spec does not ask for are simply never entered, and the plan
    reports them skipped rather than pretending they ran.
    """

    def __init__(self, provisioner: "Provisioner", node: str) -> None:
        self._p = provisioner
        self._node = node
        self._open: str | None = None

    def _say(self, name: str, status: str) -> None:
        self._p._emit("provision_phase", node=self._node, phase=name, status=status)

    def enter(self, name: str) -> None:
        """Open a phase that has no natural end - the last one, which the caller ends."""
        self.close()
        self._open = name
        self._say(name, "running")

    def close(self, status: str = "done") -> None:
        if self._open:
            self._say(self._open, status)
            self._open = None

    @contextmanager
    def __call__(self, name: str):
        self.enter(name)
        try:
            yield
        except BaseException:
            self.close("failed")
            raise
        else:
            self.close("done")


class Provisioner:
    def __init__(
        self,
        observer: Any,
        on_event: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.observer = observer
        self.on_event = on_event
        # node name -> the extra_config a spec asked for, so the read-back knows
        # which fields to go and look at on the device.
        self._wanted_extra: dict[str, dict] = {}

    def _emit(self, kind: str, **data) -> None:
        if self.on_event:
            self.on_event(kind, data)

    # -- the ordered sequence --------------------------------------------------

    def provision(self, node: devices.BenchNode, spec: NodeSpec) -> SettledState:
        """Run the seven steps in order, then assert the result on the device."""
        devices.assert_commandable(node)
        self._wanted_extra[node.name] = dict(spec.extra_config)
        self._emit("provision_start", node=node.name, spec=spec.to_dict())
        phase = _Phase(self, node.name)

        # 1. Wait for the node. A flash has just rebooted it into new firmware.
        self._wait_ready(node)

        # 2. Factory reset, so the bake's userPrefs are actually applied.
        with phase(RESET):
            self._step(
                node,
                "factory_reset",
                lambda n: n.localNode.factoryReset(),
                reboots=True,
            )
            self._wait_ready(node, reconnect=True)

        # 3. Region and preset, in a write of their own.
        #
        # Without a region LoRa TX is disabled outright, so this has to happen early. But
        # it must NOT carry anything else: a region transition out of UNSET discards the
        # rest of the same message. Measured on this bench - after a factory reset,
        # writing region=EU_868 together with tx_enabled=false applied the region and
        # left tx_enabled true, with no error anywhere. A node then looks configured, is
        # not, and any negative control built on it silently measures nothing.
        region_writes = {}
        if spec.region:
            region_writes["region"] = spec.region
        if spec.modem_preset:
            region_writes["modem_preset"] = spec.modem_preset
        if region_writes:
            with phase(REGION):
                self._write_config(node, "lora", region_writes)

        # Other lora fields go in a second write, once the region is established.
        other_lora = {
            key.split(".", 1)[1]: value
            for key, value in spec.extra_config.items()
            if key.startswith("lora.")
        }
        if other_lora:
            with phase(LORA):
                self._write_config(node, "lora", other_lora)

        if spec.role:
            with phase(ROLE):
                self._write_config(node, "device", {"role": spec.role})

        # 4. Diagnostic flags. A reset wiped this, and capture depends on it.
        if spec.debug_log_api:
            with phase(FLAGS):
                self._write_config(node, "security", {"debug_log_api_enabled": True})

        # Any other sections the scenario asked for.
        for key, value in spec.extra_config.items():
            if "." not in key or key.startswith("lora."):
                continue
            section, leaf = key.split(".", 1)
            self._write_config(node, section, {leaf: value})

        if spec.long_name or spec.short_name:
            self._step(
                node,
                "set_owner",
                lambda n: n.localNode.setOwner(
                    long_name=spec.long_name, short_name=spec.short_name
                ),
            )

        # 5. Reboot, committing config to NVS.
        with phase(REBOOT):
            self._reboot(node)

        # 6. Channels LAST - before a reboot they are acknowledged and then lost.
        if spec.channel_url:
            with phase(CHANNELS):
                self._step(
                    node,
                    "set_channel_url",
                    lambda n: n.localNode.setURL(spec.channel_url),
                )
                self._wait_ready(node, reconnect=True)

        # 7. Verify on device, with one re-apply before failing.
        phase.enter(VERIFY)
        self.refresh(node)
        state = self.read_settled_state(node)
        problems = self._compare(state, spec)
        if problems:
            self._emit("provision_reapply", node=node.name, problems=problems)
            self._reapply(node, spec, problems)
            self.refresh(node)
            state = self.read_settled_state(node)
            problems = self._compare(state, spec)

        state.errors = problems
        self._emit("provision_done", node=node.name, ok=state.ok, state=state.to_dict())
        if problems:
            raise ProvisionError(f"{node.name} did not settle: {'; '.join(problems)}")
        return state

    def reboot(self, node: devices.BenchNode) -> None:
        """Reboot a node and wait for it to answer again. Nothing else."""
        devices.assert_commandable(node)
        self._wait_ready(node)
        self._reboot(node)

    def verify(
        self, node: devices.BenchNode, spec: NodeSpec
    ) -> tuple[SettledState, list[str]]:
        """Read the device's state and say how it differs from the spec.

        The read-only half of provisioning, so a caller can ask "is it already right?"
        without reapplying a factory reset and two reboots. Returns the state and the
        problems rather than raising, because "not yet" is an answer here, not a fault.
        """
        self._wanted_extra[node.name] = dict(spec.extra_config)
        self.refresh(node)
        state = self.read_settled_state(node)
        return state, self._compare(state, spec)

    # -- primitives ------------------------------------------------------------

    def _iface(self, node: devices.BenchNode) -> Any:
        return self.observer.interface(node.name)

    def _step(
        self,
        node: devices.BenchNode,
        name: str,
        action: Callable[[Any], Any],
        reboots: bool = False,
    ) -> None:
        """Run one provisioning action against the held interface.

        `reboots` forces the observer to drop and re-open afterwards. A node that reboots
        mid-call leaves a live-looking but dead interface behind: pubsub does not always
        deliver connection.lost before the next command, so without this the next step
        writes into a closed session and reports success. That is the mechanism behind
        "the write was acknowledged and then discarded" - the acknowledgement came from
        an interface that no longer had a node on the other end.
        """
        self._emit("provision_step", node=node.name, step=name)
        try:
            action(self._iface(node))
        except Exception as exc:  # noqa: BLE001
            # Several of these reboot the node mid-call, so the library raises on the way
            # out of an operation that worked. The read-back at step 7 is the arbiter.
            self._emit(
                "provision_step_raised", node=node.name, step=name, error=str(exc)
            )
        time.sleep(SETTLE_AFTER_WRITE_S)
        if reboots:
            # The node is rebooting. expect_reboot abandons the handle rather than
            # closing it: a close blocks on a device that is already leaving and keeps
            # the port against the reconnect immediately after it.
            self.observer.owner_for(node.name).expect_reboot(f"provision:{name}")

    def _write_config(
        self, node: devices.BenchNode, section: str, values: dict
    ) -> None:
        """Write one config section and commit it.

        A write is acknowledged before it reaches NVS, so this never trusts its own
        return - verification happens at step 7 and nowhere else.
        """
        self._emit(
            "provision_config", node=node.name, section=section, values=_safe(values)
        )

        def _apply(iface: Any) -> None:
            local = iface.localNode
            config = getattr(local.localConfig, section, None)
            if config is None:
                config = getattr(local.moduleConfig, section, None)
            if config is None:
                raise ProvisionError(f"unknown config section {section!r}")
            for key, value in values.items():
                _set_field(config, key, value)
            local.writeConfig(section)

        # A security-config write reboots the node, so the interface must be recycled or
        # the next command reports success into a dead session.
        self._step(node, f"write_{section}", _apply, reboots=(section == "security"))
        self._wait_ready(node, reconnect=True)

    def _reboot(self, node: devices.BenchNode) -> None:
        self._step(node, "reboot", lambda n: n.localNode.reboot(), reboots=True)
        time.sleep(5.0)
        self._wait_ready(node, reconnect=True)

    def _wait_ready(
        self,
        node: devices.BenchNode,
        timeout: float = READY_TIMEOUT_S,
        reconnect: bool = True,
    ) -> None:
        """Block until the node is answering again, or fail with a named outcome.

        Delegated to the port owner, which is the only thing that opens the device. It
        retries with spacing and reports each attempt, so a node that is merely busy
        rebooting stays distinguishable from one that is genuinely gone.
        """
        owner = self.observer.owner_for(node.name)
        result = owner.wait_answering(budget_s=timeout)
        self._emit(
            "node_ready" if result.ok else "node_not_ready",
            node=node.name,
            outcome=result.outcome,
            waited_s=round(result.elapsed_s, 1),
            budget_s=result.budget_s,
            detail=result.detail,
        )
        if not result.ok:
            raise ProvisionError(
                f"{node.name} did not become ready: {result.outcome} after "
                f"{result.elapsed_s:.0f}s of {result.budget_s:.0f}s - {result.detail}"
            )

    # -- read-back -------------------------------------------------------------

    def refresh(self, node: devices.BenchNode) -> None:
        """Drop and re-open the node's interface so its config comes from the device.

        localConfig is the CLIENT's cached copy, fetched once when the interface
        connected. Reading it back after a write returns whatever the client already had,
        so a write that stuck and a write that did not look identical - which is the exact
        failure the read-back step exists to catch, performed against a cache.

        Several config writes also reboot the node, staling that copy a second way.
        Reconnecting settles both.
        """
        self._emit("provision_refresh", node=node.name)
        # A clean release, NOT expect_reboot: the node is staying put, so its handle must
        # actually be closed. Abandoning one on a device that never leaves keeps the port
        # owned by this process and every later open is denied.
        self.observer.owner_for(node.name).drop_cached_connection("config_readback")
        self._wait_ready(node, reconnect=True)

    def read_settled_state(self, node: devices.BenchNode) -> SettledState:
        """Everything read FROM the device, never from what we intended to write."""
        iface = self._iface(node)
        state = SettledState(
            node=node.name,
            serial_number=node.serial_number,
            port=devices.try_resolve_port(node.serial_number),
            node_id=None,
            node_num=None,
            firmware_version=None,
            build_tag=None,
            region=None,
            modem_preset=None,
            role=None,
        )
        try:
            info = iface.getMyNodeInfo() or {}
            state.node_num = info.get("num")
            user = info.get("user") or {}
            state.node_id = user.get("id")
        except Exception as exc:  # noqa: BLE001
            state.errors.append(f"could not read node info: {exc}")

        try:
            meta = getattr(iface, "metadata", None)
            if meta is not None:
                state.firmware_version = getattr(meta, "firmware_version", None) or None
        except Exception:  # noqa: BLE001
            pass

        try:
            lora = iface.localNode.localConfig.lora
            state.region = _enum_name(lora, "region")
            state.modem_preset = _enum_name(lora, "modem_preset")
            state.tx_enabled = bool(getattr(lora, "tx_enabled", False))
        except Exception as exc:  # noqa: BLE001
            state.errors.append(f"could not read lora config: {exc}")

        try:
            state.role = _enum_name(iface.localNode.localConfig.device, "role")
        except Exception:  # noqa: BLE001
            pass

        state.channels = self._read_channels(iface)
        state.extra_config = self._read_extra(iface, node)
        return state

    def _read_extra(self, iface: Any, node: devices.BenchNode) -> dict:
        """Observed values for the keys this node's spec asked for."""
        wanted = self._wanted_extra.get(node.name, {})
        out: dict[str, Any] = {}
        for path in wanted:
            out[path] = read_config_value(iface, path)
        return out

    def _read_channels(self, iface: Any) -> list[dict]:
        """Channel names, roles and PSK LENGTHS. Never the key material.

        The length is the point: a node with pskLen 1 and an empty name is
        unprovisioned, and it beacons perfectly happily while producing green rows.
        """
        out: list[dict] = []
        try:
            channels = getattr(iface.localNode, "channels", None) or []
        except Exception:  # noqa: BLE001
            return out
        for index, channel in enumerate(channels):
            try:
                role = _enum_name(channel, "role")
                if role == "DISABLED":
                    continue
                settings = getattr(channel, "settings", None)
                psk = getattr(settings, "psk", b"") if settings else b""
                out.append(
                    {
                        "index": index,
                        "name": getattr(settings, "name", "") if settings else "",
                        "role": role,
                        "psk_len": len(psk or b""),
                    }
                )
            except Exception:  # noqa: BLE001
                continue
        return out

    # -- verification ----------------------------------------------------------

    def _compare(self, state: SettledState, spec: NodeSpec) -> list[str]:
        problems = list(state.errors)
        if spec.region and _norm(state.region) != _norm(spec.region):
            problems.append(f"region is {state.region!r}, expected {spec.region!r}")
        if spec.modem_preset and _norm(state.modem_preset) != _norm(spec.modem_preset):
            problems.append(
                f"modem_preset is {state.modem_preset!r}, expected {spec.modem_preset!r}"
            )
        if spec.role and _norm(state.role) != _norm(spec.role):
            problems.append(f"device role is {state.role!r}, expected {spec.role!r}")
        if state.region is not None and _norm(state.region) == "UNSET":
            problems.append("region is UNSET, which disables LoRa TX")

        # Every value the spec asked for, checked against what the device reports. The
        # previous version hardcoded "tx_enabled false is a problem", which is backwards
        # for a scenario that deliberately disables TX - and, worse, meant a spec value
        # that silently failed to apply was never noticed at all. A negative control whose
        # precondition quietly did not take is exactly the hollow pass this bench exists
        # to prevent.
        for path, expected in spec.extra_config.items():
            actual = state.extra_config.get(path, _MISSING)
            if actual is _MISSING:
                problems.append(f"{path} could not be read back from the device")
            elif not _values_match(actual, expected):
                problems.append(f"{path} is {actual!r}, expected {expected!r}")

        # The hollow-pass check. A node with a default-length key and no channel name is
        # unprovisioned, and nothing else in the run would notice.
        if spec.channel_url:
            primary = next((c for c in state.channels if c["index"] == 0), None)
            if primary is None:
                problems.append("no primary channel after applying the channel URL")
            elif primary["psk_len"] <= 1:
                problems.append(
                    f"primary channel psk_len is {primary['psk_len']} - the node is "
                    "unprovisioned and any pass it produces is hollow"
                )
        return problems

    def _reapply(
        self, node: devices.BenchNode, spec: NodeSpec, problems: list[str]
    ) -> None:
        """One re-apply. The write may simply not have reached NVS yet."""
        joined = " ".join(problems)
        if "region" in joined or "modem_preset" in joined or "tx_enabled" in joined:
            writes = {}
            if spec.region:
                writes["region"] = spec.region
            if spec.modem_preset:
                writes["modem_preset"] = spec.modem_preset
            if writes:
                self._write_config(node, "lora", writes)
        if "device role" in joined and spec.role:
            self._write_config(node, "device", {"role": spec.role})
        if "channel" in joined and spec.channel_url:
            self._step(
                node, "set_channel_url", lambda n: n.localNode.setURL(spec.channel_url)
            )
            self._wait_ready(node, reconnect=True)


# -- helpers --------------------------------------------------------------------


_MISSING = object()


def read_config_value(iface: Any, path: str) -> Any:
    """Read a "section.field" from the device's live config.

    Looks in localConfig then moduleConfig, and renders enums by name so a comparison
    reads as words rather than integers.
    """
    if "." not in path:
        return None
    section, field_name = path.split(".", 1)
    local = iface.localNode
    config = getattr(local.localConfig, section, None)
    if config is None:
        config = getattr(local.moduleConfig, section, None)
    if config is None:
        return None
    descriptor = config.DESCRIPTOR.fields_by_name.get(field_name)
    if descriptor is not None and descriptor.enum_type is not None:
        return _enum_name(config, field_name)
    return getattr(config, field_name, None)


def _values_match(actual: Any, expected: Any) -> bool:
    """Compare a device value to a spec value without tripping over types.

    Booleans are compared as booleans, enums by name, everything else by string - the
    device answers with protobuf types and a scenario is written in plain Python.
    """
    if isinstance(expected, bool) or isinstance(actual, bool):
        return bool(actual) == bool(expected)
    if isinstance(expected, str) or isinstance(actual, str):
        return _norm(actual) == _norm(expected)
    return actual == expected


def _set_field(config: Any, key: str, value: Any) -> None:
    """Assign a protobuf field, resolving enum names to their numbers."""
    current = getattr(config, key, None)
    if isinstance(value, str) and isinstance(current, int):
        descriptor = config.DESCRIPTOR.fields_by_name.get(key)
        if descriptor is not None and descriptor.enum_type is not None:
            entry = descriptor.enum_type.values_by_name.get(value.upper())
            if entry is None:
                raise ProvisionError(f"{value!r} is not a valid value for {key}")
            setattr(config, key, entry.number)
            return
    setattr(config, key, value)


def _enum_name(message: Any, field_name: str) -> str | None:
    """The NAME of an enum field, so a settled-state block reads as words not integers."""
    try:
        value = getattr(message, field_name)
        descriptor = message.DESCRIPTOR.fields_by_name.get(field_name)
        if descriptor is not None and descriptor.enum_type is not None:
            return descriptor.enum_type.values_by_number[value].name
        return str(value)
    except Exception:  # noqa: BLE001
        return None


def _norm(value: Any) -> str:
    return str(value or "").strip().upper()


def _safe(values: dict) -> dict:
    """Config values for the log, with anything key-shaped reduced to its length."""
    out = {}
    for key, value in values.items():
        if "psk" in key.lower() or "key" in key.lower():
            out[key] = f"<{len(value) if hasattr(value, '__len__') else '?'} bytes>"
        else:
            out[key] = value
    return out


# -- isolation ------------------------------------------------------------------
#
# Every operation that reboots a node, and the wait for it to come back, runs in a
# short-lived child process (bench/provision_child.py). A reboot is exactly when this
# process used to lose track of a handle: an open that overran against a node that was
# leaving or silent, a reader thread outliving the interface it served, a close racing
# the library's own. Each of those contested the port for the rest of the operation -
# opens alternating "Access is denied" and 25 s timeouts - and one run ended in a
# segfault. When a child exits the OS closes every handle it had, so none of that can
# outlive the operation. The parent only ever closes a node that is staying put, and
# reopens it once the child is gone.

# Hard ceilings on a child, past which it is killed. Deliberately above the budgets the
# work inside plans against: they exist so an unattended run always ends, not to time
# the work.
CHILD_CEILING_S = {
    "provision": PROVISION_BUDGET_S + READY_TIMEOUT_S,
    "verify": READY_TIMEOUT_S + 60.0,
    "reboot": READY_TIMEOUT_S + 90.0,
}
# How long the parent waits to take capture back when the child got no answer either.
# One real attempt: a node wedged after a config reboot stays wedged, and the health
# loop keeps trying at its own spacing after this.
RECLAIM_AFTER_FAILURE_S = 45.0
# Prefix on every protocol line a child writes, so anything else that reaches its stdout
# cannot be mistaken for a message.
CHILD_TAG = "@@bench "


def spec_to_json(spec: NodeSpec) -> dict:
    """The whole spec, secrets included - it travels over a pipe, never argv or a log."""
    return {
        "region": spec.region,
        "modem_preset": spec.modem_preset,
        "role": spec.role,
        "channel_url": spec.channel_url,
        "long_name": spec.long_name,
        "short_name": spec.short_name,
        "debug_log_api": spec.debug_log_api,
        "extra_config": dict(spec.extra_config),
    }


def node_to_json(node: devices.BenchNode) -> dict:
    return {
        "name": node.name,
        "serial_number": node.serial_number,
        "role": node.role,
        "board": node.board,
        "never_command": node.never_command,
        "never_flash": node.never_flash,
    }


def state_from_json(data: dict) -> SettledState:
    known = set(SettledState.__dataclass_fields__)
    return SettledState(**{k: v for k, v in data.items() if k in known})


def child_command() -> list[str]:
    """How a child is started. faulthandler, so a crash in one leaves a traceback."""
    import sys

    return [sys.executable, "-X", "faulthandler", "-m", "bench.provision_child"]


class IsolatedProvisioner:
    """The Provisioner's interface, with every device operation run in a child process.

    provision(), verify() and reboot() behave as Provisioner's do - same events, same
    SettledState, same ProvisionError - because the child runs the Provisioner itself and
    this forwards what it reports. Only where the port is opened differs.

    The parent's side of each call: stop the observer reconnecting to the node, lend the
    port (closing capture's connection while the node is still staying put), run the
    child to completion or to its ceiling, and only then - the child gone and every
    handle it had closed by the OS - take capture back.
    """

    def __init__(
        self,
        observer: Any,
        on_event: Callable[[str, dict], None] | None = None,
        command: list[str] | None = None,
        ceilings: dict[str, float] | None = None,
        ready_timeout_s: float = READY_TIMEOUT_S,
    ) -> None:
        self.observer = observer
        self.on_event = on_event
        self.command = command
        self.ceilings = {**CHILD_CEILING_S, **(ceilings or {})}
        self.ready_timeout_s = ready_timeout_s

    def _emit(self, kind: str, **data: Any) -> None:
        if self.on_event:
            self.on_event(kind, data)

    # -- the same three operations -----------------------------------------------

    def provision(self, node: devices.BenchNode, spec: NodeSpec) -> SettledState:
        reply = self._run("provision", node, spec)
        return state_from_json(reply["state"])

    def verify(
        self, node: devices.BenchNode, spec: NodeSpec
    ) -> tuple[SettledState, list[str]]:
        reply = self._run("verify", node, spec)
        return state_from_json(reply["state"]), list(reply.get("problems") or [])

    def reboot(self, node: devices.BenchNode) -> None:
        self._run("reboot", node, None)

    # -- one child -----------------------------------------------------------------

    def _run(self, op: str, node: devices.BenchNode, spec: NodeSpec | None) -> dict:
        """Run one operation in a child and return its reply, or raise ProvisionError."""
        from . import ports

        devices.assert_commandable(node)
        ceiling = float(self.ceilings[op])
        reason = f"provision:{op}"
        job: dict[str, Any] = {
            "op": op,
            "node": node_to_json(node),
            "spec": spec_to_json(spec) if spec is not None else None,
        }
        run: dict[str, Any] = {
            "outcome": ports.FAILED,
            "detail": "not started",
            "reply": None,
        }
        back = None
        self.observer.suspend(node.name, reason)
        try:
            owner = self.observer.owner_for(node.name)
            with owner.lend(reason, budget_s=ceiling) as port:
                job["port"] = port
                run = self._spawn(node, op, job, ceiling)
        finally:
            # The child is gone by here - _spawn does not return while it lives - so
            # nothing of it can contest the port the parent is about to open.
            reply = run.get("reply") or {}
            back = self.observer.resume(
                node.name,
                budget_s=(
                    self.ready_timeout_s
                    if reply.get("answered")
                    else RECLAIM_AFTER_FAILURE_S
                ),
            )
            if back is not None:
                self._emit(
                    "capture_resumed",
                    node=node.name,
                    outcome=back.outcome,
                    waited_s=round(back.elapsed_s, 1),
                    budget_s=back.budget_s,
                    detail=back.detail,
                )

        reply = run.get("reply")
        if reply is None:
            raise ProvisionError(
                f"{node.name}: {op} child ended {run['outcome']} without a result "
                f"({run.get('detail', '')})"
            )
        if reply.get("error"):
            raise ProvisionError(reply["error"])
        if back is not None and not back.ok:
            raise ProvisionError(
                f"{node.name}: {op} completed in the child but capture could not be taken "
                f"back: {back.outcome} ({back.detail})"
            )
        return reply

    def _spawn(
        self, node: devices.BenchNode, op: str, job: dict, ceiling_s: float
    ) -> dict:
        """Start the child, forward what it reports, and never return while it lives."""
        import collections
        import json
        import os
        import queue
        import subprocess
        import threading
        from pathlib import Path

        from . import ports

        budget = ports.Budget(ceiling_s)
        env = dict(os.environ)
        root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = root + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.Popen(
            self.command or child_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._emit(
            "provision_child_start",
            node=node.name,
            op=op,
            pid=proc.pid,
            port=job.get("port"),
            budget_s=ceiling_s,
        )
        lines: "queue.Queue[str | None]" = queue.Queue()
        stderr_tail: "collections.deque[str]" = collections.deque(maxlen=40)

        def _pump_out() -> None:
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)

        def _pump_err() -> None:
            for line in proc.stderr:
                stderr_tail.append(line.rstrip())

        out_thread = threading.Thread(
            target=_pump_out, daemon=True, name="bench-child-out"
        )
        out_thread.start()
        err_thread = threading.Thread(
            target=_pump_err, daemon=True, name="bench-child-err"
        )
        err_thread.start()

        reply: dict | None = None
        killed = False
        try:
            try:
                proc.stdin.write(json.dumps(job))
                proc.stdin.close()
            except OSError:
                pass  # it died at once; the exit code says why
            while True:
                if budget.spent:
                    killed = True
                    break
                try:
                    line = lines.get(timeout=min(1.0, max(0.05, budget.remaining)))
                except queue.Empty:
                    continue
                if line is None:
                    break
                if not line.startswith(CHILD_TAG):
                    stderr_tail.append(line.rstrip())
                    continue
                try:
                    msg = json.loads(line[len(CHILD_TAG) :])
                except ValueError:
                    stderr_tail.append(line.rstrip())
                    continue
                if msg.get("t") == "event":
                    data = dict(msg.get("data") or {})
                    data["via"] = "child"
                    self._emit(msg.get("kind") or "provision_child_event", **data)
                elif msg.get("t") == "log":
                    self._forward_log(node.name, msg.get("line", ""))
                elif msg.get("t") == "result":
                    reply = msg
        finally:
            if proc.poll() is None:
                if not killed:
                    # Output ended but the process has not: give it a moment to exit.
                    try:
                        proc.wait(timeout=10.0)
                    except subprocess.TimeoutExpired:
                        killed = True
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
            out_thread.join(2.0)
            err_thread.join(2.0)
            for pipe in (proc.stdout, proc.stderr):
                try:
                    pipe.close()
                except Exception:  # noqa: BLE001
                    pass

        code = proc.returncode
        if killed:
            outcome = ports.TIMED_OUT
            detail = f"killed after {budget.elapsed:.0f}s of {ceiling_s:.0f}s"
        elif reply is not None:
            outcome = ports.FAILED if reply.get("error") else ports.OK
            detail = f"exit {code}"
        else:
            outcome, detail = ports.FAILED, f"exit {code} ({_exit_name(code)})"
        self._emit(
            "provision_child_end",
            node=node.name,
            op=op,
            outcome=outcome,
            detail=detail,
            exit_code=code,
            elapsed_s=round(budget.elapsed, 1),
            budget_s=ceiling_s,
            stderr_tail=list(stderr_tail)[-15:] if outcome != ports.OK else [],
        )
        return {"outcome": outcome, "detail": detail, "reply": reply, "exit_code": code}

    def _forward_log(self, name: str, line: str) -> None:
        """A log line the child's connection received, into this run's capture."""
        record = getattr(self.observer, "record_log", None)
        if record is not None:
            record(name, line)


def _exit_name(code: int | None) -> str:
    """Say what an exit code means when it is a crash rather than a return."""
    if code is None:
        return "still running"
    unsigned = code & 0xFFFFFFFF
    if unsigned == 0xC0000005 or code in (-11, 139):
        return "access violation"
    if unsigned >= 0xC0000000:
        return f"NTSTATUS 0x{unsigned:08X}"
    if code < 0:
        return f"signal {-code}"
    return "returned without a result"
