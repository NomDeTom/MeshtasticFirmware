"""End-to-end orchestration, driven with fakes instead of hardware.

The runner's job is sequencing and bookkeeping: build before rows, capture before
interpretation, results appended as they finish, and a resumed run picking up where the
last one stopped. None of that needs a radio to test, and all of it is where an
interrupted 12-hour run either survives or loses its evidence.

The fakes stand in for the three things that touch hardware - the observer, the flasher
and the provisioner - and record what they were asked to do, so the tests assert on the
orchestration rather than on the devices.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bench import manifest as manifest_mod
from bench import provision, runner, scenario, server
from bench.devices import BenchNode


class _FakePlatform:
    """Minimal stand-in: snapshot() only needs to_dict() to produce something JSON-safe."""

    pio = "pio"
    nrfutil = None
    uhubctl = None

    def to_dict(self):
        return {"os": "windows", "wsl": False}


class FakeHeld:
    def __init__(self, name):
        self.node = BenchNode(name, f"SER-{name}", "dut")
        self.connected = True
        self.port = "COM9"


class FakeObserver:
    """Stands in for held interfaces. Records sends so a stimulus can be asserted on."""

    def __init__(self, recorder, nodes):
        self.recorder = recorder
        self.held = {n.name: FakeHeld(n.name) for n in nodes}
        self.sent: list[tuple[str, str]] = []
        self.dropped: list[str] = []
        self.stopped = False

    def start(self):
        return {name: {"opened": True, "mode": "api"} for name in self.held}

    def stop(self):
        self.stopped = True
        return self.status()

    def status(self):
        return {"nodes": {n: {"connected": True, "packets": 0, "log_lines": 0} for n in self.held},
                "dropped": []}

    def health_tick(self):
        return None

    def mark_dropped(self, name, reason):
        self.dropped.append(f"{name}:{reason}")

    def interface(self, name):
        return mock.MagicMock()

    def send_text(self, name, text, channel_index=0, **kw):
        self.sent.append((name, text))


class FakeProvisioner:
    def __init__(self, observer, on_event=None):
        self.observer = observer
        self.provisions = 0
        self.verifies = 0

    def verify(self, node, spec):
        self.verifies += 1
        return self._state(node), []

    def provision(self, node, spec):
        self.provisions += 1
        return self._state(node)

    def _state(self, node):
        return provision.SettledState(
            node=node.name, serial_number=node.serial_number, port="COM9",
            node_id=f"!{node.name}", node_num=1, firmware_version="2.8.0",
            build_tag="deadbeef", region="EU_868", modem_preset="LONG_FAST",
            role="CLIENT", channels=[{"index": 0, "name": "bench", "psk_len": 32}],
            tx_enabled=True)


def make_scenarios(n=3, assertion=None):
    bake = manifest_mod.Bake("env", label="fake")
    out = []
    for i in range(n):
        out.append(scenario.Scenario(
            id=f"S{i}", description="fake",
            roles={"dut": scenario.RoleBake("dut", bake, provision.NodeSpec(region="EU_868"))},
            stimulus=scenario.STIM_SELF,
            duration_s=0.0,
            assertions=[assertion or scenario.SettledStateAssertion()]))
    return out


class RunnerHarness(unittest.TestCase):
    def build_runner(self, scenarios, **kw):
        run_dir = Path(tempfile.mkdtemp())
        nodes = [BenchNode("dut", "SER-dut", "dut")]
        config = runner.RunConfig(
            run_dir=run_dir, firmware_root=Path("."), nodes=nodes,
            scenarios=scenarios, skip_flash=True, **kw)
        r = runner.Runner(config)

        # Every distinct bake is "already built", so stage 1 is a no-op and no compiler
        # or build lock is involved.
        sha, dirty = "sha", False
        for s in scenarios:
            for role, rb in s.roles.items():
                h = rb.bake.content_hash(sha, dirty)
                r.manifest.assign(s.id, role, h)
                r.manifest.add(manifest_mod.ImageEntry(
                    bake_hash=h, bake=rb.bake.fingerprint(sha, dirty), env="env",
                    artifacts=["fake.uf2"], git_sha=sha, dirty=dirty,
                    capabilities=["log.DEBUG", "log.sink.api"], bench_only_flags=[],
                    release_representative=True))
        r.manifest.save()
        return r, run_dir

    def run_with_fakes(self, r):
        with mock.patch.object(runner, "observer_mod") as obs_mod, \
             mock.patch.object(runner.provision, "Provisioner", FakeProvisioner), \
             mock.patch.object(runner.manifest_mod, "git_state", return_value=("sha", False)), \
             mock.patch.object(r, "stage_preflight"), \
             mock.patch.object(r, "stage_build"):
            obs_mod.Observer = FakeObserver
            r.platform = _FakePlatform()
            return r.run()


class TestOrchestration(RunnerHarness):
    def test_every_row_runs_and_is_recorded(self):
        r, run_dir = self.build_runner(make_scenarios(3))
        summary = self.run_with_fakes(r)
        self.assertEqual(summary["counts"]["PASS"], 3)
        saved = json.loads((run_dir / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(saved), ["S0", "S1", "S2"])

    def test_results_are_written_per_row_so_a_kill_keeps_them(self):
        r, run_dir = self.build_runner(make_scenarios(3))
        self.run_with_fakes(r)
        # Each row also leaves its own evidence directory entry.
        for i in range(3):
            self.assertTrue((run_dir / "rows" / f"S{i}.json").exists())

    def test_a_resumed_run_skips_rows_already_recorded(self):
        scenarios = make_scenarios(3)
        r, run_dir = self.build_runner(scenarios)
        self.run_with_fakes(r)

        # A second Runner over the same directory must not redo finished work: builds
        # cost ~29 minutes and a 12-hour run cannot afford to restart from zero.
        r2, _ = self.build_runner(scenarios)
        r2.run_dir = run_dir
        r2.results_path = run_dir / "results.json"
        r2.state_path = run_dir / "state.json"
        r2.results = r2._load_results()
        self.assertEqual(len(r2.results), 3)
        summary = self.run_with_fakes(r2)
        self.assertEqual(summary["done"], 3)

    def test_an_unchanged_spec_is_verified_rather_than_reapplied(self):
        # Full prep is a factory reset, several writes and two reboots - about three
        # minutes a node. Rows in one matrix mostly want the same state, so redoing it
        # every row costs far more than the capture it prepares for.
        r, _ = self.build_runner(make_scenarios(4))
        self.run_with_fakes(r)
        fake = r._provisioner
        self.assertEqual(fake.provisions, 1, "should provision once, then verify")
        self.assertEqual(fake.verifies, 3)

    def test_scenario_markers_bound_each_row(self):
        from bench import streams

        r, run_dir = self.build_runner(make_scenarios(2))
        self.run_with_fakes(r)
        labels = {m["label"] for m in streams.marks(run_dir)}
        for i in range(2):
            self.assertIn(f"S{i}:start", labels)
            self.assertIn(f"S{i}:end", labels)
            # The settled-state preamble makes the log self-describing.
            self.assertIn(f"S{i}:preamble", labels)

    def test_a_failing_row_does_not_stop_the_run(self):
        boom = scenario.LogCount("impossible", ["never"], at_least=1)
        scenarios = make_scenarios(3, assertion=boom)
        r, run_dir = self.build_runner(scenarios)
        summary = self.run_with_fakes(r)
        self.assertEqual(summary["counts"]["NOT OBSERVED"], 3)
        self.assertEqual(summary["done"], 3)

    def test_row_exception_becomes_invalid_rather_than_killing_the_run(self):
        class Exploding(scenario.Assertion):
            def check(self, led, ctx):
                raise RuntimeError("assertion blew up")

        scenarios = make_scenarios(2, assertion=Exploding("boom"))
        r, _ = self.build_runner(scenarios)
        summary = self.run_with_fakes(r)
        self.assertEqual(summary["counts"]["INVALID"], 2)
        self.assertEqual(summary["done"], 2)

    def test_drift_makes_a_row_invalid_instead_of_asserting_against_old_firmware(self):
        scenarios = make_scenarios(1)
        r, _ = self.build_runner(scenarios)
        # Edit the scenario after its image was built.
        scenarios[0].roles["dut"].bake = manifest_mod.Bake("env", {"CHANGED": "1"})
        summary = self.run_with_fakes(r)
        self.assertEqual(summary["counts"]["INVALID"], 1)
        row = summary["results"]["S0"]
        self.assertIn("hashes to", row["error"])

    def test_status_server_reads_the_finished_run_from_disk(self):
        r, run_dir = self.build_runner(make_scenarios(2))
        self.run_with_fakes(r)
        state = server.read_state(run_dir)
        self.assertEqual(state["status"], server.FINISHED)
        self.assertEqual(len(state["rows"]), 2)
        self.assertIn("EU_868", json.dumps(state["rows"]))


class TestStimulus(RunnerHarness):
    def test_rf_peer_stimulus_sends_from_the_named_source(self):
        bake = manifest_mod.Bake("env")
        row = scenario.Scenario(
            id="P0", description="", duration_s=0.0,
            roles={"dut": scenario.RoleBake("dut", bake, provision.NodeSpec(region="EU_868"))},
            stimulus=scenario.STIM_RF_PEER,
            stimulus_params={"source": "dut", "count": 3, "interval_s": 0.0, "text": "occupy"},
            senses_channel=True,
            assertions=[scenario.SettledStateAssertion()])
        r, _ = self.build_runner([row])
        self.run_with_fakes(r)
        self.assertEqual(len(r.observer.sent), 3)
        self.assertTrue(all(t.startswith("occupy") for _, t in r.observer.sent))

    def test_unknown_stimulus_is_an_error_not_a_silent_no_op(self):
        bake = manifest_mod.Bake("env")
        row = scenario.Scenario(
            id="P1", description="", duration_s=0.0,
            roles={"dut": scenario.RoleBake("dut", bake, provision.NodeSpec(region="EU_868"))},
            stimulus="teleportation",
            assertions=[scenario.SettledStateAssertion()])
        r, _ = self.build_runner([row])
        summary = self.run_with_fakes(r)
        # A row that was never stimulated would otherwise report a confident NOT OBSERVED.
        self.assertEqual(summary["counts"]["INVALID"], 1)
        self.assertIn("teleportation", summary["results"]["P1"]["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
