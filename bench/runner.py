"""Stages 1-5: the orchestrator.

Resumability is the shape of this module. Builds cost ~29 minutes apiece, so a stage-4
failure must never force a stage-1 repeat: images are content-hashed and skipped if
present, the manifest is written after every one, and per-row results are appended as
they finish so an interrupted run resumes at the next unfinished row.

Ordering is the other constraint. The build stage runs to completion BEFORE any row
executes, because a per-scenario rebuild costs ~29 minutes against ~8 minutes for a row
with a prebuilt image.

Everything the status server shows is written here as an artifact. The server renders,
it never records - if the two disagree, the artifacts win.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from . import (
    builder,
    devices,
    flasher,
    ledger as ledger_mod,
    manifest as manifest_mod,
    observer as observer_mod,
    platform_probe,
    ports,
    preflight,
    provision,
    scenario as scenario_mod,
    streams,
)

STAGE_PREFLIGHT = "0-preflight"
STAGE_BUILD = "1-build"
STAGE_FLASH = "2-flash"
STAGE_PROVISION = "3-provision"
STAGE_EXECUTE = "4-execute"
STAGE_CYCLE = "5-cycle"
STAGE_DONE = "done"

HEARTBEAT_EVERY_S = 10.0


@dataclass
class RunConfig:
    run_dir: Path
    firmware_root: Path
    nodes: list[devices.BenchNode]
    scenarios: list[scenario_mod.Scenario]
    operator_note: str = ""
    skip_flash: bool = False
    skip_provision: bool = False
    only: list[str] = field(default_factory=list)


class Runner:
    """One bench run, start to finish, resumable at row granularity."""

    def __init__(self, config: RunConfig) -> None:
        self.config = config
        self.run_dir = Path(config.run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.recorder = streams.Recorder(self.run_dir)
        self.manifest = manifest_mod.Manifest(self.run_dir / "manifest.json")
        self.results_path = self.run_dir / "results.json"
        self.state_path = self.run_dir / "state.json"
        self.results: dict[str, dict] = self._load_results()
        self.platform: platform_probe.PlatformInfo | None = None
        self.observer: observer_mod.Observer | None = None
        self.stage = STAGE_PREFLIGHT
        self.current_row: str | None = None
        self.waiting_for: str | None = None
        self.waiting_since: float | None = None
        # node name -> bake_hash currently flashed, so a shared image is installed
        # once per run rather than once per row.
        self._running_image: dict[str, str] = {}
        # node name -> the spec it was last provisioned to, so an unchanged spec is
        # verified rather than reapplied.
        self._provisioned: dict[str, str] = {}
        self._provisioner: Any = None
        self._schedule: Any = None
        self.started_at = time.time()
        self._last_heartbeat = 0.0
        self._stop_beating = threading.Event()
        self._beat_thread: threading.Thread | None = None

    # -- artifacts -------------------------------------------------------------

    def _load_results(self) -> dict[str, dict]:
        if self.results_path.exists():
            try:
                return json.loads(self.results_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
        return {}

    def _save_results(self) -> None:
        self.results_path.write_text(json.dumps(self.results, indent=2), encoding="utf-8")

    def event(self, kind: str, data: dict | None = None, **kw: Any) -> None:
        payload = {**(data or {}), **kw}
        self.recorder.event(kind, **payload)
        self.heartbeat(force=False)

    def heartbeat(self, force: bool = True) -> None:
        """Written by the run, aged by the server, so DIED is distinguishable from slow."""
        now = time.time()
        if not force and now - self._last_heartbeat < HEARTBEAT_EVERY_S:
            return
        self._last_heartbeat = now
        self.recorder.heartbeat(
            component="runner",
            stage=self.stage,
            row=self.current_row,
            waiting_for=self.waiting_for,
            waiting_since=self.waiting_since,
            done=len(self.results),
            total=len(self._selected()),
        )
        # Status is a convenience; a run must never die because it could not be
        # rendered. This runs from the finally block too, where an exception would mask
        # whatever actually went wrong.
        try:
            self.state_path.write_text(
                json.dumps(self.snapshot(), indent=2, default=str), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            self.recorder.event("heartbeat_write_failed", error=f"{type(exc).__name__}: {exc}")

    def wait_note(self, what: str | None) -> None:
        """Report the wait, not just the work - the line a progress bar always omits."""
        self.waiting_for = what
        self.waiting_since = time.time() if what else None
        self.heartbeat()

    def snapshot(self) -> dict:
        # A run id names a workspace, not an attempt: re-running one resumes it, keeping
        # the results already banked so a twelve-hour matrix survives a crash without
        # redoing half-hour builds. The cost is that a row from an earlier attempt sits
        # in results.json looking current, so each is marked with whether THIS attempt
        # produced it - a stale verdict presented as live is the reader being misled.
        counts = {"PASS": 0, "FAIL": 0, "NOT OBSERVED": 0, "INVALID": 0, "PLANNED": 0}
        carried = 0
        selected = {s.id for s in self._selected()}
        pending: list[str] = []
        for scenario_id, row in self.results.items():
            verdict = row.get("verdict", "INVALID")
            stale = (row.get("ended_at") or 0) < self.started_at
            # An INVALID row this attempt is going to redo is not a result: it is work
            # still to do. Reporting last attempt's failure as the current verdict makes
            # a run that is busy retrying look like a run that has already failed.
            if stale and verdict == scenario_mod.INVALID and scenario_id in selected:
                counts["PLANNED"] += 1
                pending.append(scenario_id)
                continue
            counts[verdict] = counts.get(verdict, 0) + 1
            if stale:
                carried += 1
        sha, dirty = manifest_mod.git_state(self.config.firmware_root)
        return {
            "run_dir": str(self.run_dir),
            "started_at": self.started_at,
            "elapsed_s": round(time.time() - self.started_at, 1),
            "operator_note": self.config.operator_note,
            # Run identity, available from the first heartbeat rather than only once an
            # image exists. The scenario-table hash makes an edited matrix visible: two
            # runs with the same table hash asked the same questions.
            "git": {"sha": sha, "dirty": dirty},
            "scenario_table_hash": self._table_hash(),
            "schedule": self._schedule.to_dict() if self._schedule else None,
            "ports": (
                {n: h.owner.status() for n, h in self.observer.held.items() if h.owner}
                if self.observer else None
            ),
            "scenarios": [s.id for s in self._selected()],
            "stage": self.stage,
            "row": self.current_row,
            "waiting_for": self.waiting_for,
            "waiting_since": self.waiting_since,
            "counts": counts,
            # Rows standing from a previous attempt at this run id, not measured now.
            "carried_over": carried,
            "attempt_started_at": self.started_at,
            # Rows queued for a retry are not done, however finished they look on disk.
            "pending_retry": pending,
            "done": len(self.results) - len(pending),
            "total": len(self._selected()),
            "platform": self.platform.to_dict() if self.platform else None,
            "nodes": devices.describe(self.config.nodes),
            "observer": self.observer.status() if self.observer else None,
            "capture": self.recorder.status(),
            "manifest": self.manifest.summary(),
            "heartbeat": time.time(),
        }

    def schedule(self) -> ports.Schedule:
        """What this run intends to do, and the worst case for each step.

        Computed before anything starts, so an unattended run has a knowable end. Steps
        nest, because the top line of a row's prep is one number and the eight operations
        under it are where the time actually goes - and most of those are skipped when
        the node already holds the required state.
        """
        plan = ports.Schedule()
        plan.add("preflight", "preflight", 60.0,
                 "checks that refuse a run which cannot prove anything", kind="preflight")

        sha, dirty = manifest_mod.git_state(self.config.firmware_root)
        distinct = {
            rb.bake.content_hash(sha, dirty): rb.bake
            for scen in self._selected() for rb in scen.roles.values()
        }
        build = plan.add("build", "build images", 0.0,
                         f"{len(distinct)} distinct image(s)", kind="build")
        for bake_hash, bake in sorted(distinct.items()):
            cached = self.manifest.has(bake_hash)
            # A prebuilt image is registered, not compiled. Budgeting a compiler run for
            # a file already on disk would overstate the plan by half an hour and make
            # the total useless as a ceiling.
            if bake.is_prebuilt:
                budget, why = 5.0, "prebuilt, registered from the firmware store"
            elif cached:
                budget, why = 0.0, "already built"
            else:
                budget, why = builder.BUILD_TIMEOUT_S, "compile"
            child = build.add(f"build:{bake_hash}", f"image {bake_hash}", budget, why)
            if cached and not bake.is_prebuilt:
                child.status = ports.SKIPPED
                child.outcome = "already built"
        build.budget_s = sum(c.budget_s for c in build.children)

        flashed: set[str] = set()
        for scen in self._selected():
            for role in scen.roles:
                node = self._node_for(role)
                if node is None or node.never_flash or self.config.skip_flash:
                    continue
                step_id = f"{scen.id}:flash:{node.name}"
                step = plan.add(step_id, f"{scen.id}: flash {node.name}",
                                flasher.FLASH_BUDGET_S, kind="flash", node=node.name,
                                detail="skipped once the node runs this image")
                for name, budget in (
                    ("prove node + check board", flasher.PROLOGUE_S),
                    ("wait for bootloader", flasher.DFU_APPEAR_S),
                    ("transfer image", flasher.TRANSFER_S),
                    ("wait for it to answer", flasher.RETURN_S),
                ):
                    step.add(f"{step_id}:{name}", name, budget)
                if node.name in flashed:
                    step.status = ports.SKIPPED
                    step.outcome = "image already installed this run"
                flashed.add(node.name)

            if not self.config.skip_provision:
                for role in scen.roles:
                    node = self._node_for(role)
                    if node is None or node.never_command:
                        continue
                    step_id = f"{scen.id}:provision:{node.name}"
                    step = plan.add(step_id, f"{scen.id}: provision {node.name}",
                                    provision.PROVISION_BUDGET_S, kind="provision",
                                    node=node.name,
                                    detail="verified instead when the state already matches")
                    for name, budget in (
                        ("factory reset", 120.0),
                        ("region + preset", 45.0),
                        ("other lora fields", 45.0),
                        ("device role", 45.0),
                        ("diagnostic flags", 45.0),
                        ("reboot to commit", 60.0),
                        ("channels", 30.0),
                        ("read back + verify", 30.0),
                    ):
                        step.add(f"{step_id}:{name}", name, budget)

            stim = scen.stimulus_params
            stimulus_s = float(stim.get("count", 0)) * float(stim.get("interval_s", 0))
            step = plan.add(f"{scen.id}:execute", f"{scen.id}: execute",
                            stimulus_s + scen.duration_s, kind="execute",
                            detail=f"{stim.get('count', 0)} sends, then a window")
            step.add(f"{scen.id}:execute:stimulus", "stimulus", stimulus_s,
                     f"{stim.get('count', 0)} x {stim.get('interval_s', 0)}s")
            step.add(f"{scen.id}:execute:window", "capture window", scen.duration_s)
            step.add(f"{scen.id}:execute:assert", "evaluate assertions", 0.0,
                     f"{len(scen.assertions)} checks")
        return plan

    def _begin(self, step_id: str) -> None:
        if self._schedule is not None:
            self._schedule.begin(step_id)
            self.heartbeat(force=False)

    def _finish(self, step_id: str, status: str = ports.DONE, outcome: str | None = None) -> None:
        if self._schedule is not None:
            self._schedule.finish(step_id, status, outcome)
            self.heartbeat(force=False)

    def _skip(self, step_id: str, why: str) -> None:
        if self._schedule is not None:
            self._schedule.skip(step_id, why)
            self.heartbeat(force=False)

    def _table_hash(self) -> str:
        """Hash of the selected scenario table, so an edited matrix is visible."""
        blob = json.dumps(
            [s.to_dict() for s in self._selected()], sort_keys=True, default=str
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]

    def _selected(self) -> list[scenario_mod.Scenario]:
        if not self.config.only:
            return self.config.scenarios
        wanted = set(self.config.only)
        return [s for s in self.config.scenarios if s.id in wanted]

    # -- stages ----------------------------------------------------------------

    def _beat_loop(self) -> None:
        """Keep the heartbeat going through blocking stages.

        Without this the build stage - roughly 29 minutes, and the majority of a run's
        wall clock - writes no heartbeat at all, so the status server ages the last one
        out and reports DIED for a run that is working perfectly. A liveness signal that
        goes quiet exactly when the run is slowest is worse than none, because it trains
        the reader to ignore it.
        """
        while not self._stop_beating.wait(HEARTBEAT_EVERY_S):
            try:
                self.heartbeat()
            except Exception:  # noqa: BLE001 - liveness must never take down a run
                pass

    def run(self) -> dict:
        self._schedule = self.schedule()
        streams.durable_write_text(
            self.run_dir / "schedule.json", json.dumps(self._schedule.to_dict(), indent=2)
        )
        self.event("schedule", steps=len(self._schedule.steps),
                   total_s=round(self._schedule.total_s, 1))
        self._stop_beating.clear()
        self._beat_thread = threading.Thread(
            target=self._beat_loop, daemon=True, name="bench-heartbeat"
        )
        self._beat_thread.start()
        try:
            self.stage_preflight()
            self.stage_build()
            self.start_capture()
            self.execute_rows()
        finally:
            self._stop_beating.set()
            if self._beat_thread is not None:
                self._beat_thread.join(timeout=2.0)
            self.stage = STAGE_DONE
            self.heartbeat()
            if self.observer is not None:
                self.observer.stop()
            self.recorder.close()
        return self.summary()

    def stage_preflight(self) -> None:
        self.stage = STAGE_PREFLIGHT
        self.heartbeat()

        # A userPrefs left injected by a crashed build would silently become part of
        # every image this run produces.
        if builder.restore_userprefs(self.config.firmware_root):
            self.event("userprefs_restored", note="a previous build left an injected file")

        self._begin("preflight")
        report = preflight.run_preflight(
            nodes=self.config.nodes, firmware_root=self.config.firmware_root
        )
        self._finish("preflight", ports.FAILED_STEP if report.blocked else ports.DONE)
        report.write(self.run_dir / "preflight.json")
        self.platform = platform_probe.probe()
        self.event("preflight", report=report.to_dict())
        if report.blocked:
            raise preflight.PreflightFailed(report)

        problems = [p for s in self._selected() for p in s.validate()]
        if problems:
            self.event("scenario_validation_failed", problems=problems)
            raise ValueError("scenario table is invalid:\n  " + "\n  ".join(problems))

    def stage_build(self) -> None:
        """Build every distinct bake to completion, before any row runs."""
        self.stage = STAGE_BUILD
        self.heartbeat()
        wanted = [
            (s.id, role, rb.bake) for s in self._selected() for role, rb in s.roles.items()
        ]
        if not wanted:
            return
        b = builder.Builder(
            root=self.config.firmware_root,
            pio=self.platform.pio,
            manifest=self.manifest,
            on_event=lambda kind, data: self.event(kind, data),
        )
        self._begin("build")
        self.wait_note("building images")
        outcome = b.build_all(wanted)
        self.wait_note(None)
        self._finish("build", ports.FAILED_STEP if outcome["failed"] else ports.DONE)
        self.event("build_stage_done", outcome=outcome)
        if outcome["failed"]:
            raise builder.BuildError(f"{len(outcome['failed'])} bakes failed to build")

    def start_capture(self) -> None:
        """Open every node before the first row and keep them open to the last."""
        self.observer = observer_mod.Observer(self.recorder, self.config.nodes)
        report = self.observer.start()
        self.event("capture_started", nodes=report)
        # A stream that never starts turns every later row into a false negative.
        self.recorder.assert_live(max_age_s=60.0)

    def execute_rows(self) -> None:
        for scen in self._selected():
            # A resume skips rows that were MEASURED, not rows that were attempted.
            # INVALID is the bench saying it failed to take a measurement, so treating it
            # as recorded turns the retry into a silent no-op: the run reports the same
            # INVALID it started with, having touched no hardware.
            recorded = self.results.get(scen.id)
            verdict = getattr(recorded, "verdict", None) or (
                recorded.get("verdict") if isinstance(recorded, dict) else None
            )
            if recorded is not None and verdict != "INVALID":
                self.event("row_skipped", scenario=scen.id, reason="already recorded")
                continue
            self.current_row = scen.id
            result = self.run_row(scen)
            self.results[scen.id] = result.to_dict()
            self._save_results()
            self.current_row = None
            self.heartbeat()

    # -- one row ---------------------------------------------------------------

    def run_row(self, scen: scenario_mod.Scenario) -> scenario_mod.RowResult:
        result = scenario_mod.RowResult(
            scenario_id=scen.id, verdict=scenario_mod.INVALID, started_at=time.time()
        )
        try:
            images = self._prepare_row(scen, result)
            ctx = self._context_for(scen, result, images)

            self.stage = STAGE_EXECUTE
            self._begin(f"{scen.id}:execute")
            self.recorder.mark(f"{scen.id}:start", scenario=scen.to_dict())
            self._begin(f"{scen.id}:execute:stimulus")
            self._stimulate(scen)
            self._finish(f"{scen.id}:execute:stimulus")
            self._begin(f"{scen.id}:execute:window")
            self.wait_note(f"{scen.id} capture window {scen.duration_s:.0f}s")
            time.sleep(scen.duration_s)
            self.wait_note(None)
            self._finish(f"{scen.id}:execute:window")
            self.recorder.mark(f"{scen.id}:end")

            # Liveness before interpretation: silence from a dead stream is not evidence.
            try:
                self.recorder.assert_live(max_age_s=max(120.0, scen.duration_s * 2))
            except streams.CaptureStalled as exc:
                result.verdict = scenario_mod.INVALID
                result.error = f"capture stalled during the row: {exc}"
                return result

            self._resolve_build_tags(result, images)
            led = ledger_mod.Ledger.for_scenario(self.run_dir, scen.id)
            self._begin(f"{scen.id}:execute:assert")
            result.outcomes = [a.evaluate(led, ctx) for a in scen.assertions]
            result.verdict = scenario_mod.roll_up(result.outcomes)
            self._finish(f"{scen.id}:execute:assert", outcome=result.verdict)
            self._finish(f"{scen.id}:execute", outcome=result.verdict)
            (self.run_dir / "rows").mkdir(exist_ok=True)
            (self.run_dir / "rows" / f"{scen.id}.json").write_text(
                json.dumps({"result": result.to_dict(), "ledger": led.summary()}, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001 - a row must never kill the run
            result.verdict = scenario_mod.INVALID
            result.error = f"{type(exc).__name__}: {exc}"
            self.event("row_error", scenario=scen.id, error=result.error,
                       traceback=traceback.format_exc()[-2000:])
        finally:
            result.ended_at = time.time()
            self.stage = STAGE_CYCLE
        return result

    def _prepare_row(self, scen: scenario_mod.Scenario, result: scenario_mod.RowResult) -> dict:
        """Flash and provision every role, and record what actually ended up on them."""
        sha, dirty = manifest_mod.git_state(self.config.firmware_root)
        images: dict[str, manifest_mod.ImageEntry] = {}

        for role, role_bake in scen.roles.items():
            node = self._node_for(role)
            if node is None:
                raise provision.ProvisionError(f"no node configured for role {role!r}")

            # Refuse to flash an image that no longer matches the scenario's definition.
            self.manifest.check_drift(scen.id, role, role_bake.bake, sha, dirty)
            entry = self.manifest.image_for(scen.id, role)
            if entry is None:
                raise manifest_mod.DriftError(f"{scen.id}/{role} has no image")
            images[role] = entry
            result.images[role] = entry.bake_hash
            if not entry.release_representative:
                result.release_representative = False

            if node.never_flash or self.config.skip_flash:
                continue
            # Rows in one matrix usually share images - the LBT table is 12 pairs over a
            # single bake, so reflashing per row would spend ~12 minutes reinstalling
            # firmware the node already runs. The build tag is what makes skipping safe:
            # identity is carried by the image itself rather than inferred, and stage 3
            # reads it back off the device to confirm.
            step_id = f"{scen.id}:flash:{node.name}"
            if self._running_image.get(node.name) == entry.bake_hash:
                self._skip(step_id, "node already runs this image")
                self.event(
                    "flash_skipped", node=node.name, bake_hash=entry.bake_hash,
                    reason="node already runs this image",
                )
                continue
            self._begin(step_id)
            self.stage = STAGE_FLASH
            self.wait_note(f"flashing {node.name} for {scen.id}")
            # Prefer the nrfutil package: it streams over the bootloader's CDC instead
            # of copying megabytes onto a USB mass-storage volume, which on this bench
            # disturbs the external drive the run writes its evidence to.
            image = (
                entry.dfu_zip
                if (entry.dfu_zip and self.platform and self.platform.nrfutil)
                else (entry.uf2 or entry.hex_file)
            )
            if image is None:
                raise flasher.FlashError(f"image {entry.bake_hash} has no flashable artifact")
            f = flasher.Flasher(
                platform=self.platform,
                on_event=lambda kind, data: self.event(kind, data),
                observer=self.observer,
            )
            try:
                f.flash(node, Path(image), image_hw_model=entry.hw_model)
            except Exception:
                self._finish(step_id, ports.FAILED_STEP)
                raise
            self._running_image[node.name] = entry.bake_hash
            self._finish(step_id)
            self.wait_note(None)

        for role, role_bake in scen.roles.items():
            node = self._node_for(role)
            if node is None or node.never_command or self.config.skip_provision:
                continue
            self.stage = STAGE_PROVISION
            provision_started = time.time()
            self.wait_note(f"provisioning {node.name} for {scen.id}")
            p = self._provisioner_for_run()
            spec = role_bake.spec or provision.NodeSpec()
            step_id = f"{scen.id}:provision:{node.name}"
            self._begin(step_id)
            try:
                state = self._provision_or_verify(p, node, spec, step_id)
            except Exception:
                self._finish(step_id, ports.FAILED_STEP)
                raise
            self._finish(step_id)
            state_dict = state.to_dict()
            # The tag is resolved after the capture window, not here. The boot banner is
            # delivered when the firmware flushes its log buffer to a newly attached
            # client, which lands AFTER the node reports ready - so reading it now races
            # the device and finds nothing on a node that announced itself perfectly.
            state_dict["_tag_since"] = provision_started
            result.settled[role] = state_dict
            self.wait_note(None)

        # The settled-state block goes into the capture as a preamble, so the log is
        # self-describing and a hollow pass cannot be mistaken for a real one.
        self.recorder.mark(f"{scen.id}:preamble", settled=result.settled, images=result.images)
        return images


    def _resolve_build_tags(
        self, result: scenario_mod.RowResult, images: dict[str, manifest_mod.ImageEntry]
    ) -> None:
        """Fill in each role's observed build tag, once the capture has caught up.

        Deferred to the end of the row on purpose: the boot banner reaches the recorder
        only when the firmware flushes its log buffer to a client that has just attached,
        which happens after the node reports ready. Reading it during prep found nothing
        on nodes that had announced themselves correctly seconds later.
        """
        for role, state in result.settled.items():
            since = state.pop("_tag_since", None)
            name = state.get("node") or role
            entry = images.get(role)
            tag = self._build_tag_for(name, entry, since=None)

            # The banner is emitted at boot and flushed to whichever client is attached
            # at the time. During a flash or a prep reboot the observer is deliberately
            # detached, so whether the tag was caught is luck - one node got it and the
            # other did not, on the same run. Identity is too important to leave to that,
            # so ask again deterministically: reboot once with the observer attached.
            if tag is None and entry is not None and manifest_mod.BUILD_TAG in entry.capabilities:
                tag = self._reboot_for_build_tag(name)
            state["build_tag"] = tag

    def _reboot_for_build_tag(self, node_name: str, timeout: float = 60.0) -> str | None:
        """Reboot a node with the observer attached, to make it announce itself.

        Cheap and only used when the tag was missed. The row's evidence is already
        captured by this point, so the reboot cannot affect the result it belongs to.
        """
        node = self._node_for(node_name)
        if node is None or node.never_command or self.observer is None:
            return None
        self.event("build_tag_reboot", node=node_name, reason="tag not seen during prep")
        try:
            self.observer.interface(node_name).localNode.reboot()
        except Exception as exc:  # noqa: BLE001 - the node reboots out from under the call
            self.event("build_tag_reboot_raised", node=node_name, error=str(exc))
        self.observer.owner_for(node_name).expect_reboot("build_tag_reboot")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(3.0)
            self.observer.owner_for(node_name).hold(budget_s=20.0)
            tag = self._build_tag_for(node_name, None, since=None)
            if tag:
                self.event("build_tag_observed", node=node_name, tag=tag)
                return tag
        self.event("build_tag_missing", node=node_name)
        return None

    def _provisioner_for_run(self) -> provision.Provisioner:
        """One provisioner for the whole run.

        It is stateless per call, but building a fresh one per role per row hid the fact
        that prep decisions accumulate across rows - which is exactly what
        _provision_or_verify depends on.
        """
        if self._provisioner is None:
            self._provisioner = provision.Provisioner(
                self.observer, on_event=lambda kind, data: self.event(kind, data)
            )
        return self._provisioner

    def _provision_or_verify(
        self,
        provisioner: provision.Provisioner,
        node: devices.BenchNode,
        spec: provision.NodeSpec,
        step_id: str | None = None,
    ) -> provision.SettledState:
        """Provision, or - if the node is already in this exact state - just prove it.

        Full prep is a factory reset, several config writes and two reboots: roughly
        three minutes per node. Rows in one matrix mostly want the same state, so doing
        that every row costs far more than the capture it is preparing for.

        Skipping is safe only because the read-back is unconditional. The state is still
        verified against the device on every row, and anything short of a clean match
        falls through to a full reprovision - so this trades time, never assurance.
        """
        fingerprint = json.dumps(spec.to_dict(), sort_keys=True, default=str)
        if self._provisioned.get(node.name) == fingerprint:
            state, problems = provisioner.verify(node, spec)
            if not problems:
                # The plan's budget is a ceiling, not an estimate: every write under this
                # step is skipped when the node already holds the required state, and the
                # expanded view is where that difference becomes visible.
                if step_id and self._schedule is not None:
                    parent = self._schedule.find(step_id)
                    for child in (parent.children if parent else []):
                        if child.name != "read back + verify":
                            self._schedule.skip(child.id, "state already matches")
                self.event(
                    "provision_skipped", node=node.name,
                    reason="already in this state, verified on device",
                )
                return state
            self.event("provision_redo", node=node.name, problems=problems)

        state = provisioner.provision(node, spec)
        self._provisioned[node.name] = fingerprint
        return state

    def _build_tag_for(
        self,
        node_name: str,
        entry: manifest_mod.ImageEntry | None,
        since: float | None = None,
    ) -> str | None:
        """The build tag the node ACTUALLY reported at boot, not the one we intended.

        Falls back to None rather than to the manifest's value: reporting the intended
        tag as though it were observed is precisely the confusion the tag exists to stop.
        """
        expected = entry.bake_hash if entry else None
        # The LAST tag this node announced during the run. Not bounded to the row: the
        # tag identifies the image, an image only changes when the node is reflashed, and
        # reflashes are tracked separately - so the most recent announcement is always
        # what the node is running now. Bounding it to a window instead made identity
        # depend on whether the banner happened to land inside it, and a node that had
        # announced itself perfectly was reported as unproven.
        rows = streams.window(self.run_dir, streams.LOGS, start=since)
        seen = None
        for row in rows:
            if row.get("node") != node_name:
                continue
            line = row.get("line") or ""
            if "BENCH:" in line and "tag=" in line:
                seen = line.split("tag=", 1)[1].split()[0].strip()
        if seen and expected and seen != expected:
            # The node is not running what the manifest says. Drop the bookkeeping so the
            # next row reflashes rather than trusting a stale assumption, and let the row
            # carry the mismatch as evidence.
            self.event(
                "build_tag_mismatch", node=node_name, reported=seen, expected=expected
            )
            self._running_image.pop(node_name, None)
        return seen

    def _context_for(
        self,
        scen: scenario_mod.Scenario,
        result: scenario_mod.RowResult,
        images: dict[str, manifest_mod.ImageEntry],
    ) -> scenario_mod.Context:
        return scenario_mod.Context(
            scenario_id=scen.id,
            nodes={r: self._node_for(r) for r in scen.roles},
            settled=result.settled,
            capabilities={r: set(e.capabilities) for r, e in images.items()},
            params=dict(scen.stimulus_params),
            # Taken from the observer's live state rather than from the node table, so an
            # assertion is judged against how the node was ACTUALLY captured.
            capture_modes={
                name: info.get("mode", "api")
                for name, info in (self.observer.status()["nodes"] if self.observer else {}).items()
            },
        )

    def _node_for(self, role: str) -> devices.BenchNode | None:
        for node in self.config.nodes:
            if node.role == role or node.name == role:
                return node
        return None

    def _stimulate(self, scen: scenario_mod.Scenario) -> None:
        """Apply the scenario's stimulus.

        `self` needs nothing - the node's own timer fires. `rf_peer` sends real frames
        from a real node. `api` and `rf_exciter` are dispatched to their handlers, and an
        unknown stimulus is an error rather than a silent no-op, because a row that was
        never stimulated otherwise reports a confident NOT OBSERVED.
        """
        params = scen.stimulus_params
        if scen.stimulus == scenario_mod.STIM_SELF:
            return
        if scen.stimulus == scenario_mod.STIM_RF_PEER:
            self._stimulate_rf_peer(scen, params)
            return
        if scen.stimulus == scenario_mod.STIM_API:
            self.event("stimulus_api", scenario=scen.id, params=params)
            return
        if scen.stimulus == scenario_mod.STIM_RF_EXCITER:
            self._stimulate_exciter(scen, params)
            return
        raise ValueError(f"unknown stimulus {scen.stimulus!r} for {scen.id}")

    def _stimulate_rf_peer(self, scen: scenario_mod.Scenario, params: dict) -> None:
        """Real nodes sending real frames - the cheap channel occupier.

        Deliberately the first thing to reach for on a channel-sensing test: it needs no
        custom firmware, is version-matched for free, and covers most of the matrix.

        `sources` is a list because a listen-before-talk test needs two different things
        happening at once, and naming only the occupier gets it wrong. CAD runs when the
        DUT *wants to transmit* - a DUT that is merely listening never arms it, produces
        no trials, and the row scores NOT OBSERVED against firmware that is fine. So the
        DUT is usually a source too: the peer makes the channel busy, and the DUT trying
        to send is what actually exercises the decision under test.
        """
        sources = params.get("sources") or [params.get("source", "peer")]
        count = int(params.get("count", 10))
        interval = float(params.get("interval_s", 1.0))
        text = params.get("text", f"{scen.id}-stim")
        self.event(
            "stimulus_rf_peer", scenario=scen.id, sources=list(sources), count=count
        )
        sent = {name: 0 for name in sources}
        failures = 0
        for i in range(count):
            for name in sources:
                try:
                    self.observer.send_text(name, f"{text}-{name}-{i}")
                    sent[name] += 1
                except Exception as exc:  # noqa: BLE001
                    failures += 1
                    self.event(
                        "stimulus_send_failed",
                        scenario=scen.id,
                        source=name,
                        index=i,
                        error=str(exc),
                    )
            time.sleep(interval)
        # Report what was actually emitted, so a row can tell "the DUT did not defer"
        # from "the stimulus never ran".
        self.event("stimulus_rf_peer_done", scenario=scen.id, sent=sent, failures=failures)

    def _stimulate_exciter(self, scen: scenario_mod.Scenario, params: dict) -> None:
        """Raw carrier or preamble from the exciter node.

        Only for what a valid frame cannot do - CAD threshold calibration and the
        false-preamble path. The exciter is opened per row rather than held for the
        session, because an instrument left configured between rows is an instrument
        whose state nobody checked.
        """
        from . import exciter as exciter_mod

        node = self._node_for(params.get("source", "exciter"))
        if node is None:
            raise exciter_mod.ExciterError(
                f"{scen.id} needs an exciter node, but none is defined in the node table"
            )
        driver = exciter_mod.Exciter(node, recorder=self.recorder)
        driver.open()
        try:
            if params.get("freq_hz"):
                driver.configure(params["freq_hz"], params.get("sf", 11), params.get("bw_hz", 250000))
            outcome = driver.burst(
                count=int(params.get("count", 20)),
                dwell_ms=int(params.get("dwell_ms", 200)),
                gap_ms=int(params.get("gap_ms", 300)),
                mode=params.get("mode", "carrier"),
            )
            self.event("stimulus_exciter", scenario=scen.id, **outcome)
        finally:
            driver.close()

    # -- reporting -------------------------------------------------------------

    def summary(self) -> dict:
        snap = self.snapshot()
        return {
            **snap,
            "results": self.results,
            "line": self.one_line(),
        }

    def one_line(self) -> str:
        """The terse status a human would ask for."""
        counts = self.snapshot()["counts"]
        cap = self.recorder.status()
        live = sum(1 for s in cap["streams"].values() if s["rows"])
        last = min(
            (s["age_s"] for s in cap["streams"].values() if s["age_s"] is not None),
            default=None,
        )
        elapsed = time.time() - self.started_at
        return (
            f"run {time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime(self.started_at))}  "
            f"stage {self.stage}  row {self.current_row or '-'} "
            f"({len(self.results)}/{len(self._selected())})  "
            f"elapsed {int(elapsed // 3600)}h{int(elapsed % 3600 // 60):02d}m\n"
            f"  pass {counts['PASS']}  fail {counts['FAIL']}  "
            f"not-observed {counts['NOT OBSERVED']}  invalid {counts['INVALID']}   "
            f"capture: {live} streams live, last event "
            f"{'never' if last is None else f'{last:.0f}s ago'}"
        )
