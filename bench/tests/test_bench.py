"""Unit tests for the bench, run with stdlib unittest so they need no new dependency.

    python -m unittest discover -s bench/tests -t .

Everything here runs without hardware. The properties under test are the ones whose
failure produces a confident wrong answer rather than an error - honest last-byte
resolution, paths kept distinct rather than averaged, the INVALID/NOT OBSERVED
distinction, and preprocessor-faithful capability derivation.
"""

from __future__ import annotations

import json
import tempfile
import time
import threading
import types
import unittest
from pathlib import Path

from bench import builder, ledger, manifest, packets, scenario, server, streams
from bench.devices import BenchNode, CommandRefused, assert_commandable, looks_like_dfu
from bench.observer import _RawSerialReader


class TestPackets(unittest.TestCase):
    def test_records_fields_a_summary_would_drop(self):
        row = packets.summarize(
            {
                "id": 1,
                "hopLimit": 2,
                "hopStart": 3,
                "viaMqtt": False,
                "publicKey": b"k" * 32,
                "pkiEncrypted": True,
                "txAfter": 500,
                "priority": "BACKGROUND",
                "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": b"hi"},
            },
            observer="dut",
        )
        for field in ("hop_start", "via_mqtt", "transport_mechanism", "tx_after",
                      "priority", "pki_encrypted", "xeddsa_signed", "rx_time"):
            self.assertIn(field, row)
        self.assertEqual(row["hops_taken"], 1)

    def test_never_stores_key_material(self):
        row = packets.summarize({"id": 1, "publicKey": b"secret-key-bytes" * 2}, observer="o")
        self.assertEqual(row["public_key_len"], 32)
        self.assertNotIn("public_key", row)
        self.assertNotIn("secret", json.dumps(row))

    def test_last_byte_resolution_is_honest(self):
        unique = packets.resolve_last_byte(0xDC, [0x77E4F0DC, 0x1234ABCD])
        self.assertEqual(unique.status, packets.UNIQUE)
        self.assertEqual(unique.node_num, 0x77E4F0DC)

        # Two candidates share the byte: the firmware refuses to tie-break, so do we.
        ambiguous = packets.resolve_last_byte(0xDC, [0x11AA11DC, 0x2222DCDC])
        self.assertEqual(ambiguous.status, packets.AMBIGUOUS)
        self.assertIsNone(ambiguous.node_num)
        self.assertIn("ambiguous", ambiguous.render())

        self.assertEqual(packets.resolve_last_byte(0xDC, []).status, packets.NONE)
        # 0 is the NO_RELAY sentinel, not a node whose last byte happens to be zero.
        self.assertEqual(packets.resolve_last_byte(0, [0x100]).status, packets.NOT_SET)

    def test_ambiguous_render_never_fabricates_a_node(self):
        rendered = packets.resolve_last_byte(0xDC, [0x11AA11DC, 0x2222DCDC]).render()
        self.assertNotIn("!", rendered)

    def test_decrypt_failure_is_derived_from_signal_without_portnum(self):
        failed = packets.summarize({"id": 2, "encrypted": b"\xde\xad", "rxRssi": -64}, observer="o")
        self.assertEqual(failed["status"], packets.ST_DECRYPT_FAIL)
        ok = packets.summarize({"id": 3, "decoded": {"portnum": "X", "payload": b"a"}}, observer="o")
        self.assertEqual(ok["status"], packets.ST_OK)


class TestLedger(unittest.TestCase):
    def rows(self):
        base = {"observer": "obs", "dir": "SEEN", "portnum": "TEXT_MESSAGE_APP",
                "from_node": "!aaa", "to_node": "^all", "status": "OK", "payload_size": 10}
        return [
            {**base, "id": 1, "ts": 1.0, "rx_rssi": -38, "rx_snr": 6.0, "hops_taken": 0,
             "relay_node": {"raw": None, "status": "not_set"}},
            # Same packet id, different path: a relayed copy at a very different level.
            {**base, "id": 1, "ts": 1.5, "rx_rssi": -73, "rx_snr": 2.0, "hops_taken": 1,
             "relay_node": {"raw": 0xDC, "status": "unique", "node_num": 0x77E4F0DC}},
            {**base, "id": 2, "ts": 2.0, "rx_rssi": -60, "rx_snr": 5.0, "hops_taken": 0,
             "relay_node": {"raw": None, "status": "not_set"}},
        ]

    def test_deduplicates_by_id_but_keeps_distinct_paths(self):
        lane = ledger.PacketLane(self.rows())
        self.assertEqual(lane.count(), 2)
        first = [s for s in lane.sightings() if s.packet_id == 1][0]
        self.assertEqual(first.rebroadcast_count, 2)
        # The whole point: -38 and -73 are two real paths, not one -55 dBm path.
        self.assertEqual(len(first.paths), 2)
        self.assertIn(-38, first.rssis)
        self.assertIn(-73, first.rssis)

    def test_rf_stats_report_spread_not_just_centre(self):
        stats = ledger.PacketLane(self.rows()).rf_stats("obs")
        self.assertIn("median", stats["rssi"])
        self.assertIn("stdev", stats["rssi"])
        self.assertGreater(stats["rssi"]["max"] - stats["rssi"]["min"], 30)

    def test_rf_only_excludes_traffic_that_never_crossed_the_air(self):
        rows = self.rows() + [
            {"id": 9, "ts": 3.0, "observer": "obs", "dir": "SEEN", "status": "OK",
             "via_mqtt": True, "rx_rssi": None, "rx_snr": None, "relay_node": {}},
        ]
        lane = ledger.PacketLane(rows)
        self.assertEqual(lane.count(), 3)
        self.assertEqual(lane.count(rf_only=True), 2)

    def test_decrypt_failures_grouped_by_source(self):
        lane = ledger.PacketLane([
            {"id": 5, "ts": 1.0, "observer": "o", "from_node": "!bad", "status": "DECRYPT_FAIL",
             "relay_node": {}},
            {"id": 6, "ts": 1.0, "observer": "o", "from_node": "!bad", "status": "DECRYPT_FAIL",
             "relay_node": {}},
        ])
        self.assertEqual(lane.decrypt_failures_by_source(), {"!bad": 2})

    def test_log_lane_accepts_alternative_wordings(self):
        lane = ledger.LogLane([
            {"node": "dut", "line": "Received text msg from=0x1"},
            {"node": "dut", "line": "phone downloaded packet (id=0x2 fr=0x1)"},
        ])
        # One wording finds one line; the pair finds both. This is the failure that
        # scored a demonstrably working link as zero received, twice.
        self.assertEqual(lane.count([r"Received text msg"]), 1)
        self.assertEqual(lane.count([r"Received text msg", r"phone downloaded packet"]), 2)


class TestStreams(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.rec = streams.Recorder(self.dir)

    def tearDown(self):
        self.rec.close()

    def test_marks_land_in_both_events_and_logs(self):
        self.rec.mark("S1:start")
        self.rec.close()
        self.assertEqual(len(streams.marks(self.dir)), 1)
        logs = list(streams.read_stream(self.dir, streams.LOGS))
        self.assertTrue(any(r.get("level") == "MARK" for r in logs))

    def test_scenario_slice_is_bounded_by_markers(self):
        self.rec.log(node="dut", line="before")
        self.rec.mark("S1:start")
        self.rec.log(node="dut", line="inside")
        self.rec.mark("S1:end")
        self.rec.log(node="dut", line="after")
        self.rec.close()
        lines = [r.get("line") for r in
                 streams.between_marks(self.dir, "S1:start", "S1:end", streams.LOGS)]
        self.assertIn("inside", lines)
        self.assertNotIn("before", lines)
        self.assertNotIn("after", lines)

    def test_assert_live_raises_on_a_stalled_stream(self):
        self.rec.event("started")
        self.rec.assert_live(max_age_s=60)
        # Age the stream past the bound: a silent stop must not read as a real negative.
        self.rec._streams[streams.EVENTS].last_ts = time.time() - 500
        with self.assertRaises(streams.CaptureStalled):
            self.rec.assert_live(max_age_s=60)

    def test_truncated_final_line_is_survivable(self):
        self.rec.log(node="dut", line="good")
        self.rec.close()
        with (self.dir / "logs.jsonl").open("a", encoding="utf-8") as fh:
            fh.write('{"ts": 1, "line": "trunca')
        self.assertEqual(len(list(streams.read_stream(self.dir, streams.LOGS))), 1)


class TestManifestAndBuilder(unittest.TestCase):
    def test_identical_bakes_share_one_image(self):
        a = manifest.Bake("env", {"P": "1"}, {"F": 1})
        b = manifest.Bake("env", {"P": "1"}, {"F": 1})
        c = manifest.Bake("env", {"P": "2"}, {"F": 1})
        self.assertEqual(a.content_hash("sha", False), b.content_hash("sha", False))
        self.assertNotEqual(a.content_hash("sha", False), c.content_hash("sha", False))

    def test_same_flags_against_different_source_are_different_images(self):
        bake = manifest.Bake("env")
        self.assertNotEqual(bake.content_hash("aaa", False), bake.content_hash("bbb", False))
        self.assertNotEqual(bake.content_hash("aaa", False), bake.content_hash("aaa", True))

    def test_capabilities_follow_the_preprocessor_not_python_truthiness(self):
        # -DFOO=0 defines the macro and disables the feature.
        self.assertNotIn(manifest.LOG_TRACE,
                         manifest.Bake("e", build_flags={"MESHTASTIC_TRACE_LOGGING": 0}).capabilities())
        self.assertIn(manifest.LOG_TRACE,
                      manifest.Bake("e", build_flags={"MESHTASTIC_TRACE_LOGGING": 1}).capabilities())

    def test_segger_moves_the_log_sink_out_of_reach(self):
        caps = manifest.Bake("e", build_flags={"USE_SEGGER": 1}).capabilities()
        self.assertIn(manifest.LOG_SINK_RTT, caps)
        self.assertNotIn(manifest.LOG_SINK_API, caps)

    def test_bench_only_flags_break_release_representativeness(self):
        self.assertTrue(manifest.Bake("e", build_flags={"DEBUG_HEAP": 1}).release_representative())
        self.assertFalse(
            manifest.Bake("e", build_flags={"MESHTASTIC_TRACE_LOGGING": 1}).release_representative()
        )

    def test_the_build_tag_does_not_disqualify_an_image_but_must_not_ship(self):
        # Every bench image carries the tag by construction, so counting it as
        # disqualifying would mark every row non-representative and mean nothing.
        tagged = manifest.Bake("e").with_build_tag("f820151a8eed")
        self.assertTrue(tagged.release_representative())
        self.assertEqual(tagged.must_not_ship(), ["BENCH_BUILD_TAG"])

        # A flag that does change behaviour still disqualifies, tag or no tag.
        traced = manifest.Bake("e", build_flags={"MESHTASTIC_TRACE_LOGGING": 1}).with_build_tag("x")
        self.assertFalse(traced.release_representative())

    def test_drift_guard_refuses_a_stale_image(self):
        mf = manifest.Manifest(Path(tempfile.mkdtemp()) / "m.json")
        original = manifest.Bake("env", {"P": "1"})
        mf.assign("S1", "dut", original.content_hash("sha", False))
        mf.check_drift("S1", "dut", original, "sha", False)  # unchanged: fine
        edited = manifest.Bake("env", {"P": "2"})
        with self.assertRaises(manifest.DriftError):
            mf.check_drift("S1", "dut", edited, "sha", False)

    def test_userprefs_are_restored_byte_for_byte(self):
        root = Path(tempfile.mkdtemp())
        prefs = root / "userPrefs.jsonc"
        prefs.write_text('{\n  // comment\n  "USERPREFS_A": "1"\n}\n', encoding="utf-8")
        before = prefs.read_bytes()
        with builder.temporary_userprefs(root, {"USERPREFS_B": "2"}):
            self.assertIn("USERPREFS_B", prefs.read_text(encoding="utf-8"))
        self.assertEqual(prefs.read_bytes(), before)

    def test_malformed_injection_is_caught_before_compiling(self):
        root = Path(tempfile.mkdtemp())
        (root / "userPrefs.jsonc").write_text('{"A": "1"}', encoding="utf-8")
        with self.assertRaises(builder.BuildError):
            with builder.temporary_userprefs(root, {'BAD"KEY': "x"}):
                pass

    def test_flag_translation_matches_the_preprocessor(self):
        env = builder.build_flags_env({"BARE": True, "VAL": 7, "OFF": False, "NONE": None})
        self.assertEqual(env["PLATFORMIO_BUILD_FLAGS"], "-DBARE -DVAL=7")

    def test_build_lock_is_exclusive(self):
        root = Path(tempfile.mkdtemp())
        with builder.build_lock(root):
            with self.assertRaises(builder.BuildError):
                with builder.build_lock(root, timeout=0.5):
                    pass


class TestScenario(unittest.TestCase):
    def empty_ledger(self, log_rows=()):
        return ledger.Ledger(packets=ledger.PacketLane([]), logs=ledger.LogLane(log_rows))

    def test_missing_capability_is_invalid_not_not_observed(self):
        check = scenario.LogCount("needs_trace", ["x"], at_least=1, requires=["log.TRACE"])
        outcome = check.evaluate(self.empty_ledger(), scenario.Context("S", capabilities={"dut": set()}))
        self.assertEqual(outcome.verdict, scenario.INVALID)

    def test_absent_evidence_with_capability_present_is_not_observed(self):
        check = scenario.LogCount("wanted", ["never-appears"], at_least=1)
        ctx = scenario.Context("S", capabilities={"dut": {"log.DEBUG"}})
        self.assertEqual(check.evaluate(self.empty_ledger(), ctx).verdict, scenario.NOT_OBSERVED)

    def test_too_few_trials_is_not_observed_not_fail(self):
        rows = [{"node": "dut", "line": "CAD arm"}, {"node": "dut", "line": "CAD arm"}]
        check = scenario.RateAssertion("r", ["CAD busy"], ["CAD arm"], node="dut", min_trials=10)
        outcome = check.evaluate(self.empty_ledger(rows), scenario.Context("S", capabilities={"dut": set()}))
        self.assertEqual(outcome.verdict, scenario.NOT_OBSERVED)

    def test_rate_below_threshold_with_enough_trials_fails(self):
        rows = [{"node": "dut", "line": "CAD arm"} for _ in range(10)]
        check = scenario.RateAssertion("r", ["CAD busy"], ["CAD arm"], node="dut",
                                       min_rate=0.5, min_trials=5)
        outcome = check.evaluate(self.empty_ledger(rows), scenario.Context("S", capabilities={"dut": set()}))
        self.assertEqual(outcome.verdict, scenario.FAIL)

    def test_precondition_yields_not_observed_rather_than_failing_good_firmware(self):
        check = scenario.LogCount("restored", ["restore"], at_least=1,
                                  precondition=lambda ctx: ctx.params.get("target_rf_differs", False),
                                  precondition_reason="target uses home RF, no switch to restore")
        outcome = check.evaluate(self.empty_ledger(), scenario.Context("S", capabilities={"dut": set()}))
        self.assertEqual(outcome.verdict, scenario.NOT_OBSERVED)
        self.assertIn("home RF", outcome.evidence)

    def test_from_role_resolves_to_a_node_id_and_never_passes_vacuously(self):
        led = ledger.Ledger(
            packets=ledger.PacketLane([{"id": 1, "ts": 1.0, "observer": "observer",
                                        "from_node": "!abc", "status": "OK",
                                        "rx_rssi": -50, "relay_node": {}}]),
            logs=ledger.LogLane([]))
        check = scenario.PacketCount("silent", observer="observer", from_role="dut", at_most=0)

        # No settled state: the check cannot address its subject, so an at_most bound
        # must not pass by matching nothing.
        unprovisioned = scenario.Context("S", capabilities={"dut": set()})
        self.assertEqual(check.evaluate(led, unprovisioned).verdict, scenario.INVALID)

        silent = scenario.Context("S", capabilities={"dut": set()},
                                  settled={"dut": {"node_id": "!dead"}})
        self.assertEqual(check.evaluate(led, silent).verdict, scenario.PASS)

        talking = scenario.Context("S", capabilities={"dut": set()},
                                   settled={"dut": {"node_id": "!abc"}})
        self.assertEqual(check.evaluate(led, talking).verdict, scenario.FAIL)

    def test_packet_assertion_against_a_raw_captured_node_is_invalid(self):
        # The passive observer has no packet lane, so an at_most bound over it would be a
        # control that cannot fail - the most dangerous shape a check can take.
        check = scenario.PacketCount("silent", observer="observer", at_most=0)
        ctx = scenario.Context("S", capture_modes={"observer": "raw"})
        outcome = check.evaluate(self.empty_ledger(), ctx)
        self.assertEqual(outcome.verdict, scenario.INVALID)
        self.assertIn("raw serial", outcome.evidence)

    def test_observer_silence_needs_the_observer_to_have_been_listening(self):
        heard = [{"node": "observer", "line": "Received text msg from=0x77e4f0dc"},
                 {"node": "observer", "line": "unrelated"}]
        check = scenario.ObserverSilence("sil", observer_node="observer", from_role="dut")
        dut = {"dut": {"node_id": "!77e4f0dc", "node_num": 0x77E4F0DC}}
        other = {"dut": {"node_id": "!deadbeef", "node_num": 0xDEADBEEF}}

        self.assertEqual(
            check.evaluate(self.empty_ledger(heard), scenario.Context("S", settled=dut)).verdict,
            scenario.FAIL)
        self.assertEqual(
            check.evaluate(self.empty_ledger(heard), scenario.Context("S", settled=other)).verdict,
            scenario.PASS)
        # An observer that logged nothing was not listening; that is not evidence of
        # silence on the air.
        self.assertEqual(
            check.evaluate(self.empty_ledger([]), scenario.Context("S", settled=other)).verdict,
            scenario.NOT_OBSERVED)

    def test_an_image_claiming_a_build_tag_must_have_echoed_one(self):
        from bench.manifest import BUILD_TAG

        base = {"node_id": "!abc", "region": "EU_868", "modem_preset": "LONG_FAST",
                "channels": [{"index": 0}], "errors": []}
        check = scenario.SettledStateAssertion()

        # The -D silently failing to reach the compiler leaves every row asserting
        # against firmware nobody can identify.
        claimed = scenario.Context("S", settled={"dut": {**base, "build_tag": None}},
                                   capabilities={"dut": {BUILD_TAG}})
        self.assertEqual(check.evaluate(self.empty_ledger(), claimed).verdict, scenario.INVALID)

        echoed = scenario.Context("S", settled={"dut": {**base, "build_tag": "f4060bbce604"}},
                                  capabilities={"dut": {BUILD_TAG}})
        self.assertEqual(check.evaluate(self.empty_ledger(), echoed).verdict, scenario.PASS)

        # An image that never claimed the capability is not penalised for lacking it.
        untagged = scenario.Context("S", settled={"dut": {**base, "build_tag": None}},
                                    capabilities={"dut": set()})
        self.assertEqual(check.evaluate(self.empty_ledger(), untagged).verdict, scenario.PASS)

    def test_rollup_precedence(self):
        O = scenario.Outcome
        self.assertEqual(scenario.roll_up([O("a", scenario.PASS, "")]), scenario.PASS)
        self.assertEqual(
            scenario.roll_up([O("a", scenario.PASS, ""), O("b", scenario.NOT_OBSERVED, "")]),
            scenario.NOT_OBSERVED)
        self.assertEqual(
            scenario.roll_up([O("a", scenario.FAIL, ""), O("b", scenario.NOT_OBSERVED, "")]),
            scenario.FAIL)
        # INVALID dominates: preconditions were never established, so the row says nothing.
        self.assertEqual(
            scenario.roll_up([O("a", scenario.FAIL, ""), O("b", scenario.INVALID, "")]),
            scenario.INVALID)
        self.assertEqual(scenario.roll_up([]), scenario.INVALID)

    def test_channel_sensing_row_cannot_use_api_injection(self):
        row = scenario.Scenario(
            id="X", description="",
            roles={"dut": scenario.RoleBake("dut", manifest.Bake("e"))},
            stimulus=scenario.STIM_API, senses_channel=True,
            assertions=[scenario.LogCount("a", ["x"], at_least=1)])
        problems = row.validate()
        self.assertTrue(any("puts no energy on the air" in p for p in problems))

    def test_assertion_against_an_undefined_role_is_caught(self):
        row = scenario.Scenario(
            id="X", description="",
            roles={"dut": scenario.RoleBake("dut", manifest.Bake("e"))},
            assertions=[scenario.LogCount("a", ["x"], at_least=1, role="ghost")])
        self.assertTrue(any("ghost" in p for p in row.validate()))


class TestDevicesAndObserver(unittest.TestCase):
    def test_observer_role_is_locked_down_on_construction(self):
        node = BenchNode("obs", "SERIAL", "observer")
        self.assertTrue(node.never_command)
        self.assertTrue(node.never_flash)
        with self.assertRaises(CommandRefused):
            assert_commandable(node)

    def test_dfu_needs_an_observed_transition(self):
        before = {"COM3": (0x239A, 0x00B3)}
        # A bootloader-shaped PID that was always there is not evidence of DFU.
        self.assertIsNone(looks_like_dfu(before, {"COM3": (0x239A, 0x00B3)}))
        self.assertEqual(looks_like_dfu(before, {"COM3": (0x239A, 0x0029)}), "COM3")
        self.assertEqual(looks_like_dfu(before, {"COM3": (0x239A, 0x00B3),
                                                 "COM9": (0x239A, 0x0029)}), "COM9")

    def reader(self):
        r = _RawSerialReader.__new__(_RawSerialReader)
        r._buf = bytearray()
        r._frames_skipped = 0
        return r

    def test_ansi_colour_does_not_hide_the_log_level(self):
        from bench.observer import _parse_log_line

        # The firmware colours its prefix, and the escapes sit between the level and the
        # pipe - so an unstripped line parses as having no level at all. Captured from
        # real hardware; this is the boot banner carrying the build tag.
        esc = chr(27)
        line = f"{esc}[0m{esc}[32mINFO  {esc}[0m| ??:??:?? 3 {esc}[32mBENCH: tag=3c9a7f5f534d"
        parsed = _parse_log_line(line)
        self.assertEqual(parsed["level"], "INFO")
        self.assertEqual(parsed["uptime_s"], 3)
        self.assertNotIn(esc, parsed["msg"])
        self.assertEqual(parsed["line"], line, "the raw line must survive verbatim")

    def test_protobuf_frames_do_not_shred_log_lines(self):
        r = self.reader()
        frame = bytes((0x94, 0xC3, 0x00, 0x05)) + bytes(range(5))
        r._buf.extend(b"INFO  | 00:01 42 [Radio] CAD busy\n" + frame + b"DEBUG | 00:02 43 x\n")
        self.assertEqual(r.drain(),
                         ["INFO  | 00:01 42 [Radio] CAD busy", "DEBUG | 00:02 43 x"])
        self.assertEqual(r._frames_skipped, 1)

    def test_frame_split_across_reads_is_carried_over(self):
        r = self.reader()
        r._buf.extend(b"line one\n" + bytes((0x94, 0xC3, 0x00, 0x08)) + bytes(3))
        self.assertEqual(r.drain(), ["line one"])
        r._buf.extend(bytes(5) + b"line two\n")
        self.assertEqual(r.drain(), ["line two"])
        self.assertEqual(r._frames_skipped, 1)

    def test_text_that_collides_with_a_frame_header_is_not_eaten(self):
        r = self.reader()
        # 0x94c3 followed by an implausible length: text, not a frame.
        r._buf.extend(bytes((0x94, 0xC3, 0xFF, 0xFF)) + b"still readable\n")
        self.assertIn("still readable", " ".join(r.drain()))


class TestSchedulePhases(unittest.TestCase):
    def test_the_plan_and_the_work_use_the_same_phase_names(self):
        """A plan that names work differently from the thing doing it can never mark it.

        Every child step under a flash or a provision is addressed by name. When those
        names lived in two places they drifted, and the sub-steps read "planned" for the
        whole run - work that plainly ran, reported as never started.
        """
        from bench import flasher, provision, runner

        source = Path("bench/runner.py").read_text(encoding="utf-8")
        self.assertIn("flasher.PHASES", source)
        self.assertIn("provision.PHASES", source)

        flash_src = Path("bench/flasher.py").read_text(encoding="utf-8")
        for name, _ in flasher.PHASES:
            self.assertIn(f'"{name}"', flash_src, f"{name} is planned but never reported")
        prov_src = Path("bench/provision.py").read_text(encoding="utf-8")
        for name, _ in provision.PHASES:
            self.assertIn(f'"{name}"', prov_src, f"{name} is planned but never reported")


class TestDfuAttribution(unittest.TestCase):
    def test_the_attribution_check_never_releases_what_it_did_not_open(self):
        """A mounted UF2 volume says some board is in DFU, never which one.

        The check asks whether THIS node still answers as an application. When capture
        already holds it the answer is yes and nothing more need be done - asking again
        closed capture's own connection, and the closing handle held the port against
        the flash's later wait for the node to return.
        """
        from bench import flasher, ports
        from bench.devices import BenchNode

        owner = ports.PortOwner(BenchNode("dut", "SER", "dut"))
        owner.iface = object()
        owner._to(ports.ST_HELD, "capture open")
        owner.release = lambda *a, **k: self.fail("must not release capture's connection")

        self.assertTrue(flasher._answers_as_application(owner))
        self.assertEqual(owner.state, ports.ST_HELD)


class TestReconnectBudget(unittest.TestCase):
    def test_a_refusal_does_not_spend_the_retry_ceiling(self):
        """The ceiling is for a node that will not come back, not for "not now".

        Measured: one flash held a node away for five minutes, capture asked every five
        seconds, and all thirty attempts were spent on refusals - so capture had given
        up before the node returned, and the DUT produced no log lines for the rest of
        the run.
        """
        from bench import observer as observer_mod, ports

        held = types.SimpleNamespace(
            node=types.SimpleNamespace(name="dut"),
            owner=types.SimpleNamespace(state=ports.ST_REBOOTING),
            connected=False, dropped_at=None, last_attempt=0.0,
            attempts=observer_mod.RECONNECT_MAX_ATTEMPTS,
            raw_mode=False, port=None,
        )
        obs = observer_mod.Observer.__new__(observer_mod.Observer)
        obs.held = {"dut": held}
        obs._suspended = set()
        obs._lock = threading.RLock()
        obs.recorder = types.SimpleNamespace(event=lambda *a, **k: None)
        obs._open = lambda h: (_ for _ in ()).throw(AssertionError("must not open"))

        obs.health_tick()
        self.assertEqual(held.attempts, 0, "a refused turn restores the budget")


class TestSilenceIsNotEvidence(unittest.TestCase):
    """A quiet instrument must not read as a quiet subject."""

    def _ledger(self, rows):
        from bench import ledger as ledger_mod

        return ledger_mod.Ledger(
            packets=ledger_mod.PacketLane([]), logs=ledger_mod.LogLane(rows)
        )

    def test_at_most_zero_on_a_silent_node_is_invalid(self):
        from bench import scenario

        check = scenario.LogCount("no_abort", [r"Duty cycle"], node="dut", at_most=0)
        ctx = scenario.Context(scenario_id="T1")

        deaf = check.check(self._ledger([{"node": "peer", "line": "hello"}]), ctx)
        self.assertEqual(deaf.verdict, scenario.INVALID)

        heard = check.check(
            self._ledger([{"node": "dut", "line": "anything at all"}]), ctx
        )
        self.assertEqual(heard.verdict, scenario.PASS)


class TestSettledStateComparison(unittest.TestCase):
    """A precondition that quietly did not apply is the hollow pass, restated."""

    def compare(self, spec_extra, observed_extra):
        from bench import provision

        p = provision.Provisioner.__new__(provision.Provisioner)
        spec = provision.NodeSpec(region="EU_868", extra_config=spec_extra)
        state = provision.SettledState(
            node="dut", serial_number="S", port="COM1", node_id="!a", node_num=1,
            firmware_version="2.8.0", build_tag="t", region="EU_868",
            modem_preset=None, role=None, tx_enabled=True,
            extra_config=observed_extra)
        return p._compare(state, spec)

    def test_a_spec_value_that_did_not_apply_is_caught(self):
        # The real case: L6 asked for tx_enabled false, the device stayed true, and the
        # row passed its settled-state check while its negative control measured nothing.
        problems = self.compare({"lora.tx_enabled": False}, {"lora.tx_enabled": True})
        self.assertTrue(any("tx_enabled" in p for p in problems), problems)

    def test_a_spec_value_that_did_apply_is_accepted(self):
        self.assertEqual(self.compare({"lora.tx_enabled": False}, {"lora.tx_enabled": False}), [])

    def test_disabling_tx_is_not_itself_treated_as_a_fault(self):
        # Scenarios deliberately disable TX; that is the point of the control, not an error.
        self.assertEqual(self.compare({"lora.tx_enabled": False}, {"lora.tx_enabled": False}), [])

    def test_an_unreadable_value_is_a_problem_not_a_pass(self):
        problems = self.compare({"lora.tx_enabled": False}, {})
        self.assertTrue(any("could not be read back" in p for p in problems), problems)


class TestProvisionerReadBack(unittest.TestCase):
    def test_verify_refreshes_before_reading_so_it_cannot_read_a_cache(self):
        from bench import provision

        calls = []

        class StubOwner:
            def expect_reboot(self, reason):
                calls.append(("rebooting", reason))

            def release(self, reason, abandon=False):
                # A read-back is not a reboot: the handle must be closed, not abandoned.
                calls.append(("released", reason, abandon))

            def wait_answering(self, budget_s=180.0):
                from bench import ports

                calls.append(("waited", budget_s))
                return ports.Result(ports.OK, "", 0.0, budget_s)

        class StubObserver:
            held = {}

            def owner_for(self, name):
                return StubOwner()

        p = provision.Provisioner(StubObserver())
        p.read_settled_state = lambda node: calls.append(("read", node.name)) or provision.SettledState(
            node=node.name, serial_number="S", port="C", node_id="!a", node_num=1,
            firmware_version="v", build_tag="t", region="EU_868", modem_preset=None,
            role=None)

        node = BenchNode("dut", "SER", "dut")
        p.verify(node, provision.NodeSpec(region="EU_868"))

        # The reconnect must happen BEFORE the read, or the read returns the client's
        # cached config and a write that never stuck looks identical to one that did.
        kinds = [c[0] for c in calls]
        self.assertLess(kinds.index("released"), kinds.index("read"))
        # And it must be a real close - abandoning a node that stays put leaks the port.
        released = [c for c in calls if c[0] == "released"][0]
        self.assertFalse(released[2], "a read-back must close, not abandon")


class TestPortOwnership(unittest.TestCase):
    """The invariant the whole port refactor exists to hold."""

    def test_only_the_port_owner_opens_a_device(self):
        """Exactly one place in the bench may call SerialInterface().

        A serial port is exclusive and this library's connect() can block indefinitely,
        so every bounded open abandons a thread that still holds the handle. Two openers
        on one device is a race with no winner - it produced four different failures that
        all looked like hardware. Grepping for it keeps the rule from eroding.
        """
        import re

        offenders = []
        for path in Path("bench").glob("*.py"):
            if path.name == "ports.py":
                continue  # the one legitimate owner
            text = path.read_text(encoding="utf-8")
            # Strip comments and docstrings so prose about the rule does not trip it.
            code = re.sub(r'""".*?"""', "", text, flags=re.S)
            code = re.sub(r"^\s*#.*$", "", code, flags=re.M)
            if "SerialInterface(" in code:
                offenders.append(path.name)
        self.assertEqual(offenders, [], "only ports.py may open a device")

    def test_a_rebooting_node_is_off_limits_to_everyone(self):
        """Ownership spans the operation, not the open handle.

        A flash gives up its lease the moment it commands DFU: the node is leaving, and
        closing the handle would block on a device that is already gone. The sixty
        seconds after that are the most fragile in the whole run, and under lease rules
        alone they are unowned - capture's health loop asked to reconnect, was correctly
        told yes, and took the port out from under a node on its way into the
        bootloader. Measured: reconnect at +18.7s, no bootloader ever appeared.
        """
        from bench import ports
        from bench.devices import BenchNode

        owner = ports.PortOwner(BenchNode("dut", "SER", "dut"))
        owner.iface = object()
        with owner.lease("flash", budget_s=5.0, reboots=True):
            pass

        self.assertEqual(owner.state, ports.ST_REBOOTING)
        refused = owner.hold(1.0)
        self.assertEqual(refused.outcome, ports.BUSY)
        self.assertIn("rebooting", refused.detail)

    def test_a_lease_is_exclusive(self):
        from bench import ports

        owner = ports.PortOwner(BenchNode("dut", "SER", "dut"))
        owner.iface = object()  # pretend capture already holds it

        with owner.lease("first", budget_s=5.0):
            self.assertEqual(owner.state, ports.ST_LEASED)
            # A second lease must not be granted while the first is live.
            with self.assertRaises(ports.PortBusy):
                with owner.lease("second", budget_s=1.0):
                    pass

    def test_a_rebooting_lease_abandons_rather_than_closes(self):
        from bench import ports

        closed = []

        class FakeIface:
            def close(self):
                closed.append(True)

        owner = ports.PortOwner(BenchNode("dut", "SER", "dut"))
        owner.iface = FakeIface()
        with owner.lease("flash", budget_s=5.0, reboots=True):
            pass
        # Closing a device that is already leaving blocks and keeps the port against
        # whatever needs it next, so a rebooting lease must never close.
        self.assertEqual(closed, [])
        self.assertIsNone(owner.iface)
        self.assertEqual(owner.state, ports.ST_REBOOTING)

    def test_every_outcome_is_one_of_the_declared_exit_states(self):
        from bench import ports

        budget = ports.Budget(1.0)
        for outcome in (ports.OK, ports.TIMED_OUT, ports.ABSENT,
                        ports.BUSY, ports.REFUSED, ports.FAILED):
            self.assertIn(outcome, ports.TERMINAL)
            self.assertEqual(budget.result(outcome).outcome, outcome)

    def test_a_schedule_sums_its_budgets(self):
        from bench import ports

        plan = ports.Schedule()
        plan.add("f", "flash", 630.0, "one node")
        plan.add("p", "provision", 420.0, "reset and verify")
        self.assertEqual(plan.total_s, 1050.0)
        self.assertIn("TOTAL", plan.summary())

    def test_steps_nest_and_track_their_own_status(self):
        from bench import ports

        plan = ports.Schedule()
        prep = plan.add("p", "provision dut", 420.0, kind="provision", node="dut")
        prep.add("p:reset", "factory reset", 120.0)
        prep.add("p:verify", "read back + verify", 30.0)

        self.assertEqual(plan.counts[ports.PLANNED], 3)
        plan.begin("p")
        # Skipped is distinct from done: a skipped step spends none of its budget, which
        # is what makes the plan's total a ceiling rather than an estimate.
        plan.skip("p:reset", "state already matches")
        plan.finish("p:verify")
        counts = plan.counts
        self.assertEqual(counts[ports.RUNNING], 1)
        self.assertEqual(counts[ports.SKIPPED], 1)
        self.assertEqual(counts[ports.DONE], 1)
        self.assertEqual(plan.find("p:reset").outcome, "state already matches")

    def test_a_step_reports_its_own_overrun(self):
        from bench import ports

        plan = ports.Schedule()
        plan.add("s", "short", 0.01)
        plan.begin("s")
        time.sleep(0.05)
        plan.finish("s")
        self.assertTrue(plan.find("s").overran, "an overrun must name itself")


class TestHardwareGuard(unittest.TestCase):
    """The one check that guards the instrument rather than the answer."""

    def test_env_maps_to_the_board_from_the_variant_ini(self):
        from bench import hardware

        self.assertEqual(
            hardware.hw_model_for_env(Path("."), "nrf52_promicro_diy_tcxo"),
            "NRF52_PROMICRO_DIY")

    def test_a_mismatched_board_is_refused(self):
        from bench import hardware

        hardware.assert_compatible("dut", "NRF52_PROMICRO_DIY", "NRF52_PROMICRO_DIY")
        # The near-miss on this bench: a Heltec named as a promicro peer.
        with self.assertRaises(hardware.HardwareMismatch):
            hardware.assert_compatible("peer", "HELTEC_MESH_POCKET", "NRF52_PROMICRO_DIY")

    def test_unknown_on_either_side_is_refused_not_assumed(self):
        from bench import hardware

        # "I could not tell" is not a licence to write flash: the cost of being wrong is
        # the node, the cost of stopping is one line in the node table.
        with self.assertRaises(hardware.HardwareMismatch):
            hardware.assert_compatible("peer", None, "NRF52_PROMICRO_DIY")
        with self.assertRaises(hardware.HardwareMismatch):
            hardware.assert_compatible("peer", "NRF52_PROMICRO_DIY", None)


class TestStatusServer(unittest.TestCase):
    def test_vanished_run_reports_died_not_a_stale_running(self):
        run = Path(tempfile.mkdtemp())
        rec = streams.Recorder(run)
        rec.heartbeat(component="runner", stage="4-execute")
        rec.close()
        (run / "state.json").write_text(json.dumps({"stage": "4-execute", "started_at": time.time()}))
        self.assertEqual(server.read_state(run)["status"], server.RUNNING)

        raw = (run / "status.jsonl").read_text(encoding="utf-8").splitlines()
        rows = [json.loads(line) for line in raw if line.strip()]
        rows[-1]["ts"] = time.time() - 10_000
        (run / "status.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        self.assertEqual(server.read_state(run)["status"], server.DIED)

    def test_state_is_rebuilt_from_disk_with_no_memory(self):
        run = Path(tempfile.mkdtemp())
        (run / "results.json").write_text(json.dumps({
            "S1": {"verdict": "PASS", "outcomes": [{"name": "a", "verdict": "PASS",
                                                    "evidence": "12 packets"}]}}))
        state = server.read_state(run)
        self.assertEqual(state["rows"][0]["verdict"], "PASS")
        self.assertIn("12 packets", state["rows"][0]["outcomes"][0]["evidence"])



class TestDashboardScript(unittest.TestCase):
    """The page is only useful if its script actually runs."""

    def _script(self) -> str:
        import re

        from bench import server

        match = re.search(r"<script>(.*?)</script>", server.PAGE, re.S)
        self.assertIsNotNone(match, "the page must carry a script block")
        return match.group(1)

    def test_no_string_literal_is_broken_across_a_line(self):
        """PAGE is a non-raw Python string, so an escape meant for the browser needs
        doubling. A single backslash-n is consumed by Python, lands as a real newline
        inside a JS string literal, and takes the entire dashboard down with a syntax
        error - the page renders nothing at all, which is how this shipped twice.
        """
        offenders = [
            line for line in self._script().splitlines()
            if line.rstrip().endswith('("') or line.rstrip().endswith("('")
        ]
        self.assertEqual(offenders, [], "string literal split across a newline")

    def test_the_script_parses(self):
        """Parse it for real where node is available; skip cleanly where it is not."""
        import shutil
        import subprocess
        import tempfile

        node = shutil.which("node")
        if not node:
            self.skipTest("node not available to parse the dashboard script")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
            fh.write(self._script())
            path = fh.name
        try:
            done = subprocess.run([node, "--check", path], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr[:400])
        finally:
            Path(path).unlink(missing_ok=True)



class TestDevicesView(unittest.TestCase):
    """Live facts and remembered facts must never be presented as the same thing."""

    def view(self, run_status, ports=None, nodes=None):
        from bench import server

        state = {
            "nodes": nodes if nodes is not None else [
                {"name": "dut", "serial_number": "NOPE-NOT-PLUGGED-IN",
                 "role": "dut", "board": "NRF52_PROMICRO_DIY"},
            ],
            "ports": ports or {},
        }
        return server.devices_view(state, run_status, beat_age=1300.0)

    def test_a_finished_runs_port_state_is_marked_stale(self):
        from bench import server

        row = self.view(server.FINISHED, {"dut": {"state": "gave_up"}})[0]
        # gave_up was true when the run stopped; presenting it as current is how a
        # healthy bench came to look broken.
        self.assertTrue(row["stale"])
        self.assertEqual(row["recorded_state"], "gave_up")
        self.assertEqual(row["as_of_s"], 1300.0)

    def test_a_live_run_is_not_marked_stale(self):
        from bench import server

        row = self.view(server.RUNNING, {"dut": {"state": "held"}})[0]
        self.assertFalse(row["stale"])
        self.assertIsNone(row["as_of_s"])

    def test_identity_survives_a_state_file_that_predates_it(self):
        from bench import server

        # Older runs wrote a port block without board or role; the node table still has
        # them, so the device is described rather than shown as blanks.
        row = self.view(server.FINISHED, {"dut": {"state": "idle"}})[0]
        self.assertEqual(row["declared_board"], "NRF52_PROMICRO_DIY")
        self.assertEqual(row["role"], "dut")

    def test_presence_is_checked_live_not_taken_from_the_run(self):
        from bench import server

        # This serial is not plugged in, so presence is False however the run remembered
        # it - enumeration opens nothing, so a read-only observer may always ask.
        row = self.view(server.FINISHED, {"dut": {"state": "held", "port": "COM16"}})[0]
        self.assertFalse(row["present"])
        self.assertIsNone(row["port"])
        self.assertEqual(row["recorded_port"], "COM16")



class TestPortLeaks(unittest.TestCase):
    """A port opened and never released is invisible until something else is denied it.

    That has cost this bench whole runs: preflight checked a board through a throwaway
    owner, walked away still holding the handle, and the run's own flash was refused
    "Access is denied" seconds later against healthy hardware.
    """

    def owner(self):
        from bench import ports

        return ports.PortOwner(BenchNode("dut", "SER", "dut"))

    def test_a_lease_gives_the_interface_back(self):
        from bench import ports

        o = self.owner()
        o.iface = object()
        ports._LIVE.add(o)
        with o.lease("work", budget_s=5.0):
            pass
        self.assertIsNotNone(o.iface, "a non-rebooting lease returns the interface")
        self.assertIn("dut", [p["node"] for p in ports.open_ports()])
        o.release("done")
        self.assertNotIn("dut", [p["node"] for p in ports.open_ports()])

    def test_a_lease_that_raises_still_releases(self):
        from bench import ports

        o = self.owner()
        o.iface = object()
        ports._LIVE.add(o)
        with self.assertRaises(RuntimeError):
            with o.lease("work", budget_s=5.0):
                raise RuntimeError("operation blew up")
        # The interface must not be stranded by a failure - that is exactly when a port
        # gets left open, because nobody is around to tidy up.
        o.release("done")
        self.assertEqual(
            [p for p in ports.open_ports() if p["node"] == "dut"], [])

    def test_a_rebooting_lease_leaves_nothing_open(self):
        from bench import ports

        o = self.owner()
        o.iface = object()
        ports._LIVE.add(o)
        with o.lease("flash", budget_s=5.0, reboots=True):
            pass
        self.assertIsNone(o.iface)
        self.assertEqual([p for p in ports.open_ports() if p["node"] == "dut"], [])

    def test_the_context_manager_releases_on_exit(self):
        from bench import ports

        with self.owner() as o:
            o.iface = object()
            ports._LIVE.add(o)
        self.assertIsNone(o.iface, "a short-lived owner must not strand a port")

    def test_every_short_lived_owner_outside_ports_is_released(self):
        """Static check: an owner built inside a function must be closed on every path.

        Catches a new call site that forgets entirely, which the runtime checks above
        cannot - those only cover paths a test actually walks.

        Constructors are exempt on purpose. An owner built in __init__ belongs to the
        object holding it and is released by that object's own teardown, which is the
        observer's arrangement; requiring a release in the constructor would be asking
        for the port to be closed the moment it was opened.
        """
        import ast

        offenders = []
        for path in Path("bench").glob("*.py"):
            if path.name == "ports.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for func in [n for n in ast.walk(tree)
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                if func.name == "__init__":
                    continue
                builds = [
                    n for n in ast.walk(func)
                    if isinstance(n, ast.Call)
                    and getattr(n.func, "attr", getattr(n.func, "id", None)) == "PortOwner"
                ]
                if not builds:
                    continue
                body = ast.dump(func)
                released = (
                    "attr='release'" in body
                    or "attr='expect_reboot'" in body
                    or any(isinstance(n, (ast.With, ast.AsyncWith)) for n in ast.walk(func))
                )
                if not released:
                    offenders.append(f"{path.name}:{func.name}")
        self.assertEqual(
            offenders, [], "PortOwner built without a release, a with-block or a reboot")

    def test_stopping_the_observer_leaves_no_port_open(self):
        """The long-lived case the static check deliberately exempts.

        The observer builds an owner per node in its constructor and holds them for the
        session, so nothing about that construction can be checked statically. What can
        be checked is the promise it makes instead: stopping it releases everything.
        """
        from bench import observer as observer_mod
        from bench import ports, streams

        rec = streams.Recorder(Path(tempfile.mkdtemp()))
        nodes = [BenchNode("leaky", "SER-LEAKY", "dut")]
        obs = observer_mod.Observer(rec, nodes)
        held = obs.held["leaky"]
        held.owner.iface = object()  # pretend capture opened it
        ports._LIVE.add(held.owner)
        self.assertIn("leaky", [p["node"] for p in ports.open_ports()])

        obs.stop()
        rec.close()
        self.assertEqual(
            [p for p in ports.open_ports() if p["node"] == "leaky"], [],
            "observer.stop() must release every port it held")


class TestLbtScenarioTable(unittest.TestCase):
    def test_table_is_valid_and_deduplicates(self):
        from bench.scenarios.lbt import SCENARIOS

        self.assertEqual([p for s in SCENARIOS for p in s.validate()], [])
        pairs = [(s.id, r) for s in SCENARIOS for r in s.roles]
        images = {rb.bake.content_hash("sha", False) for s in SCENARIOS for rb in s.roles.values()}
        self.assertGreater(len(pairs), len(images))

    def test_every_row_has_a_way_to_fail(self):
        from bench.scenarios.lbt import SCENARIOS

        for row in SCENARIOS:
            self.assertTrue(row.assertions, f"{row.id} could never fail")

    def test_every_log_pattern_matches_a_string_that_exists_in_the_firmware(self):
        """A pattern matching nothing is a silent NOT OBSERVED generator.

        The row runs, the capture is fine, the count is zero, and the verdict reads as a
        firmware miss. Cheap to catch here: assert the strings are actually in the tree
        the bench is pointed at.
        """
        import re

        from bench.scenarios.lbt import SCENARIOS

        src_root = Path("src")
        if not src_root.is_dir():  # running outside a firmware checkout
            self.skipTest("no src/ tree to check patterns against")
        source = "".join(
            f.read_text(encoding="utf-8", errors="replace") for f in src_root.rglob("*.cpp")
        )

        missing = []
        for scenario_row in SCENARIOS:
            for assertion in scenario_row.assertions:
                for attr in ("patterns", "event_patterns", "trial_patterns"):
                    for pattern in getattr(assertion, attr, []) or []:
                        if not re.search(pattern, source):
                            missing.append(f"{scenario_row.id}/{assertion.name}: {pattern!r}")
        self.assertEqual(missing, [], "patterns that match nothing in the firmware")

    def test_the_trace_gated_row_declares_its_requirement(self):
        from bench.scenarios.lbt import SCENARIOS

        row = next(s for s in SCENARIOS if s.id.startswith("L5"))
        self.assertIn("log.TRACE", row.required_capabilities()["dut"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
