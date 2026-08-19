"""Tests for the scheduled run's digest and the rolling page built from it.

The reports here are hand-built rather than produced by a campaign run: a collator checked against
the simulator's own output only proves the two agree, and the cases that matter are the ones a real
run produces once a month - a silent loss, a block whose job never wrote a file, an arm that moved
nothing. Each is built directly.

The inert check has its own regression: the first version compared only the metrics the digest
displays and called `E-signed` inert, when that arm moves `advert_bytes` by 43% and nothing else.
`test_inert_sees_a_metric_it_does_not_display` is that case.

Run from `sim/`:  python3 -m unittest sfpp.test_collate -v
"""

import contextlib
import io
import json
import os
import tempfile
import unittest

from . import collate as C
from . import explorer as E


def report(block="B-arm", arm="arm", value=1, seed=7, held=0.8, text=0.7, **overrides):
    """One cell report, shaped like the campaign's but only as deep as these tests read."""
    r = {
        "block": block,
        "arm": arm,
        "value": value,
        "seed": seed,
        "grid": [],
        "transport": "abc1234",
        "wall_seconds": 68.6,
        "opts": {"nodes": 60, "scenario": None, "signed": bool(value)},
        "mesh": {"nodes": 60, "mean_degree": 9.5, "connected": True},
        "baseline": {
            "text_reception_mean": text,
            "text_reception_min": text - 0.2,
            "reach_ceiling_mean": 0.94,
        },
        "traffic": {
            "transmissions": 10000,
            "queue_drops": 100,
            "channel_utilisation": 1.7,
            "bytes_on_air": 500000,
        },
        "sfpp": {
            "held_fraction_mean": held,
            "held_fraction_min": held - 0.1,
            "union_fraction": held + 0.02,
            "sr_airtime_share": 0.026,
            "objects_moved": 229,
            "adverts": 81,
            "advert_bytes": 12555,
            "sr_bytes": 36212,
            "escalations": 0,
            "decode_failures": 0,
            "misdecodes": 0,
            "silent_losses": 0,
            "held_per_server": [0.79, 0.80, 0.81],
        },
    }
    for path, v in overrides.items():
        section, _, field = path.partition("__")
        if field:
            r[section][field] = v
        else:
            r[section] = v
    return r


def quietly(main, argv):
    """Run a module's main() without its progress lines landing in the test output."""
    with contextlib.redirect_stdout(io.StringIO()):
        return main(argv)


def write_run(directory, blocks):
    """{block name: [reports]} on disk, the way sweep.py leaves a run."""
    os.makedirs(directory, exist_ok=True)
    for name, reports in blocks.items():
        with open(os.path.join(directory, f"{name}.json"), "w") as f:
            json.dump(reports, f)
    return directory


class NumericLeaves(unittest.TestCase):
    def test_walks_dicts_and_lists(self):
        leaves = C.numeric_leaves({"a": {"b": 1}, "c": [2, {"d": 3.5}]})
        self.assertEqual(leaves["/a/b"], 1)
        self.assertEqual(leaves["/c[0]"], 2)
        self.assertEqual(leaves["/c[1]/d"], 3.5)

    def test_bools_are_not_measurements(self):
        # `connected` flipping is a different mesh, not a different measurement of one, and a bool
        # compared as 0/1 would put every fragmented run's block on the leaderboard.
        self.assertEqual(
            C.numeric_leaves({"connected": True, "nodes": 60}), {"/nodes": 60}
        )


class Inert(unittest.TestCase):
    def test_identical_cells_are_inert(self):
        grouped = {"False": [report(value=False)], "True": [report(value=True)]}
        self.assertTrue(C._inert(grouped))

    def test_inert_sees_a_metric_it_does_not_display(self):
        # E-signed: held, text, airtime and every displayed metric identical; only the byte counters
        # move. The arm is live and must not be reported as inert.
        grouped = {
            "False": [report(value=False)],
            "True": [report(value=True, sfpp__advert_bytes=17901)],
        }
        self.assertFalse(C._inert(grouped))

    def test_a_difference_inside_a_list_counts(self):
        grouped = {
            "a": [report(value="a")],
            "b": [report(value="b", sfpp__held_per_server=[0.79, 0.80, 0.99])],
        }
        self.assertFalse(C._inert(grouped))

    def test_timing_and_seed_are_not_evidence(self):
        # Two identical cells always differ in wall_seconds, and the seed is the draw rather than a
        # measurement of it. Neither may rescue an inert arm.
        grouped = {
            "False": [report(value=False, wall_seconds=68.6, seed=1)],
            "True": [report(value=True, wall_seconds=71.2, seed=1)],
        }
        self.assertTrue(C._inert(grouped))

    def test_a_single_value_is_never_inert(self):
        self.assertFalse(C._inert({"only": [report()]}))


class Cells(unittest.TestCase):
    def test_seeds_are_averaged_and_spread_recorded(self):
        cells = C.cells_of([report(seed=1, held=0.7), report(seed=2, held=0.9)])
        self.assertEqual(len(cells), 1)
        self.assertAlmostEqual(cells[0]["metrics"]["held"], 0.8)
        self.assertEqual(cells[0]["seeds"], [1, 2])
        self.assertIn("held", cells[0]["sd"])

    def test_one_seed_records_no_spread(self):
        # A nightly run draws one seed per cell. Writing 0.0 as its spread would let the explorer
        # average a fiction into the runs that do have several.
        cells = C.cells_of([report(seed=1)])
        self.assertEqual(cells[0]["sd"], {})

    def test_values_keep_the_order_the_block_declares(self):
        reports = [report(value=v) for v in ("bucket", "interval", "aimd")]
        self.assertEqual(
            [c["value"] for c in C.cells_of(reports)], ["bucket", "interval", "aimd"]
        )


class Gate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_a_silent_loss_fails_the_run(self):
        run = write_run(
            os.path.join(self.tmp.name, "r1"),
            {
                "B-arm": [
                    report(value=1),
                    report(value=2, sfpp__silent_losses=3, held=0.9),
                ]
            },
        )
        summary = C.collate(run)
        self.assertFalse(summary["gate"]["ok"])
        self.assertIn("SILENT LOSSES", summary["gate"]["failures"][0])

    def test_queue_drops_are_a_warning_not_a_failure(self):
        run = write_run(
            os.path.join(self.tmp.name, "r2"),
            {
                "B-arm": [
                    report(value=1),
                    report(value=2, traffic__queue_drops=9000, held=0.9),
                ]
            },
        )
        summary = C.collate(run)
        self.assertTrue(summary["gate"]["ok"])
        self.assertTrue(any("queue drops" in w for w in summary["gate"]["warnings"]))

    def test_a_block_that_never_ran_is_named(self):
        run = write_run(os.path.join(self.tmp.name, "r3"), {"B-arm": [report()]})
        summary = C.collate(run, expected={"B-arm", "B-missing"})
        self.assertEqual(summary["missing_blocks"], ["B-missing"])
        self.assertEqual(summary["gate"]["blocks_missing"], 1)

    def test_its_own_digest_is_not_read_back_as_a_block(self):
        run = write_run(os.path.join(self.tmp.name, "r4"), {"B-arm": [report()]})
        quietly(C.main, ["--runs", run])
        again = C.collate(run)
        self.assertEqual([b["block"] for b in again["blocks"]], ["B-arm"])


class Trend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_blocks_rank_by_how_far_held_travels(self):
        run = write_run(
            os.path.join(self.tmp.name, "r"),
            {
                "A-small": [
                    report(block="A-small", value=1, held=0.80),
                    report(block="A-small", value=2, held=0.81),
                ],
                "B-large": [
                    report(block="B-large", value=1, held=0.50),
                    report(block="B-large", value=2, held=0.90),
                ],
            },
        )
        md = C.markdown(C.collate(run))
        self.assertLess(md.index("`B-large`"), md.index("`A-small`"))

    def test_the_report_names_the_run_and_its_ground(self):
        run = write_run(os.path.join(self.tmp.name, "r"), {"B-arm": [report()]})
        md = C.markdown(
            C.collate(run, run_id="2026-08-19-4711", seed_base="4711", scenario="ridge")
        )
        self.assertIn("2026-08-19-4711", md)
        self.assertIn("ridge", md)
        self.assertIn("4711", md)

    def test_main_writes_both_artefacts(self):
        run = write_run(os.path.join(self.tmp.name, "r"), {"B-arm": [report()]})
        self.assertEqual(quietly(C.main, ["--runs", run, "--run-id", "x"]), 0)
        self.assertTrue(os.path.exists(os.path.join(run, "summary.json")))
        self.assertTrue(os.path.exists(os.path.join(run, "trend.md")))

    def test_fail_on_gate_is_the_only_non_zero_exit(self):
        run = write_run(
            os.path.join(self.tmp.name, "r"),
            {
                "B-arm": [
                    report(value=1),
                    report(value=2, sfpp__silent_losses=1, held=0.9),
                ]
            },
        )
        self.assertEqual(quietly(C.main, ["--runs", run]), 0)
        self.assertEqual(quietly(C.main, ["--runs", run, "--fail-on-gate"]), 1)


class Explorer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.archive = os.path.join(self.tmp.name, "runs")
        for run_id, held in (("2026-08-19-1", 0.80), ("2026-08-20-2", 0.84)):
            run = write_run(
                os.path.join(self.archive, run_id),
                {
                    "B-arm": [
                        report(value=1, held=held),
                        report(value=2, held=held + 0.05),
                    ]
                },
            )
            quietly(
                C.main,
                [
                    "--runs",
                    run,
                    "--run-id",
                    run_id,
                    "--seed-base",
                    run_id.split("-")[-1],
                    "--scenario",
                    "ridge",
                ],
            )

    def test_runs_load_oldest_first(self):
        runs = E.load_archive(self.archive)
        self.assertEqual([r["run_id"] for r in runs], ["2026-08-19-1", "2026-08-20-2"])

    def test_window_keeps_the_most_recent(self):
        runs = E.load_archive(self.archive, window=1)
        self.assertEqual([r["run_id"] for r in runs], ["2026-08-20-2"])

    def test_an_unreadable_digest_does_not_stop_the_rollup(self):
        broken = os.path.join(self.archive, "2026-08-21-3")
        os.makedirs(broken)
        with open(os.path.join(broken, "summary.json"), "w") as f:
            f.write("{ this is not json")
        self.assertEqual(len(E.load_archive(self.archive)), 2)

    def test_a_block_is_indexed_across_every_run_that_has_it(self):
        blocks = E.index_by_block(E.load_archive(self.archive))
        self.assertEqual(len(blocks["B-arm"]["runs"]), 2)

    def test_leaderboard_averages_rather_than_pools(self):
        # Both runs spread held by 0.05, so the average spread is 0.05 however many runs there are.
        board = E.leaderboard(E.index_by_block(E.load_archive(self.archive)))
        self.assertAlmostEqual(board[0]["spread"], 0.05, places=6)
        self.assertEqual(board[0]["runs"], 2)

    def test_a_gap_breaks_the_sparkline_rather_than_bridging_it(self):
        # A block missing from one run must not draw a straight line through the run it missed.
        svg = E.sparkline([0.1, None, 0.9, 0.95])
        self.assertEqual(svg.count("<polyline"), 1)
        self.assertIn("polyline", E.sparkline([0.1, 0.2, None, 0.8, 0.9]))

    def test_series_holds_a_place_for_a_value_a_run_lacks(self):
        blocks = E.index_by_block(E.load_archive(self.archive))
        blocks["B-arm"]["runs"][0]["cells"].pop("2")
        self.assertEqual(E.series(blocks["B-arm"], "held")["2"][0], None)

    def test_the_page_is_self_contained_and_names_every_run(self):
        runs = E.load_archive(self.archive)
        for r in runs:
            r["_href"] = os.path.join("runs", r["_name"])
        page = E.render_html(
            runs, E.index_by_block(runs), E.leaderboard(E.index_by_block(runs))
        )
        self.assertIn("2026-08-19-1", page)
        self.assertIn("2026-08-20-2", page)
        # No CDN, no remote font, no fetch: the branch serves this file directly and it must also
        # open from disk.
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)

    def test_a_block_name_cannot_inject_markup(self):
        runs = E.load_archive(self.archive)
        runs[0]["blocks"][0]["block"] = "<script>alert(1)</script>"
        for r in runs:
            r["_href"] = "runs/" + r["_name"]
        page = E.render_html(runs, E.index_by_block(runs), [])
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_main_writes_a_page_and_a_digest(self):
        out = os.path.join(self.tmp.name, "site")
        self.assertEqual(quietly(E.main, ["--archive", self.archive, "--out", out]), 0)
        self.assertTrue(os.path.exists(os.path.join(out, "index.html")))
        with open(os.path.join(out, "INDEX.md")) as f:
            self.assertIn("2026-08-20-2", f.read())

    def test_links_are_relative_to_the_page_not_the_archive(self):
        out = os.path.join(self.tmp.name, "site2")
        quietly(E.main, ["--archive", self.archive, "--out", out])
        with open(os.path.join(out, "index.html")) as f:
            page = f.read()
        # The page sits beside `runs/`, so a run's report is one directory down from it.
        self.assertIn("runs/2026-08-20-2/trend.md", page.replace("../", ""))

    def test_an_empty_archive_says_so_rather_than_writing_a_blank_page(self):
        self.assertEqual(
            quietly(
                E.main,
                [
                    "--archive",
                    os.path.join(self.tmp.name, "nothing"),
                    "--out",
                    self.tmp.name,
                ],
            ),
            1,
        )


if __name__ == "__main__":
    unittest.main()


class ScenarioTruth(unittest.TestCase):
    """A landform the run asked for but no block recorded must be visible as such."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _page(self, observed):
        archive = os.path.join(self.tmp.name, "runs")
        run = write_run(
            os.path.join(archive, "2026-08-19-1"),
            {
                "B-arm": [
                    report(value=1, opts={"scenario": observed}),
                    report(value=2, held=0.9, opts={"scenario": observed}),
                ]
            },
        )
        quietly(
            C.main, ["--runs", run, "--run-id", "2026-08-19-1", "--scenario", "ridge"]
        )
        runs = E.load_archive(archive)
        for r in runs:
            r["_href"] = "runs/" + r["_name"]
        return E.render_html(runs, E.index_by_block(runs), [])

    def test_ground_that_reached_the_blocks_is_not_flagged(self):
        self.assertNotIn("not applied", self._page("ridge"))

    def test_ground_that_never_reached_the_blocks_is_flagged(self):
        self.assertIn("not applied", self._page(None))
