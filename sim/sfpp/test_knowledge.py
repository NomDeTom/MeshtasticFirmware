"""Tests for breaking the mesh and for the instrument that says what was lost.

The graph algorithms are checked against hand-drawn graphs with unambiguous answers rather than
against the simulator's own output, for the same reason as test_mesh: a finder that agrees with
itself proves nothing. The cases cover an asymmetric graph, where `partition` must cut inbound-only
links too, and coverage after a split, which must not exceed 100%.

Run from `sim/`:  python3 -m unittest sfpp.test_knowledge -v
"""

import random
import unittest

from . import knowledge as K
from . import mesh as M


def small_mesh(nodes=12, seed=11, **kwargs):
    rng = random.Random(seed)
    conf = M.make_config()
    return M.build(conf, nodes, 4000.0, rng, hop_limit=3, **kwargs)


def graph(mesh, adjacency):
    """Replace the mesh's link graph with a hand-drawn one, symmetric by construction."""
    mesh.neighbours = [list(peers) for peers in adjacency]
    for a, peers in enumerate(adjacency):
        for b in peers:
            mesh.rssi[a][b] = -100.0
    return mesh


class Articulation(unittest.TestCase):
    """Ground-truth graphs. Every answer here is checkable by eye."""

    def setUp(self):
        self.mesh = small_mesh(nodes=6)

    def test_path_graph_every_interior_node(self):
        graph(self.mesh, [[1], [0, 2], [1, 3], [2, 4], [3, 5], [4]])
        self.assertEqual(self.mesh.articulation_nodes(), [1, 2, 3, 4])

    def test_bowtie_is_the_two_joining_nodes(self):
        graph(self.mesh, [[1, 2], [0, 2], [0, 1, 3], [2, 4, 5], [3, 5], [3, 4]])
        self.assertEqual(self.mesh.articulation_nodes(), [2, 3])

    def test_a_ring_has_none(self):
        graph(self.mesh, [[1, 5], [0, 2], [1, 3], [2, 4], [3, 5], [4, 0]])
        self.assertEqual(self.mesh.articulation_nodes(), [])

    def test_a_star_centre(self):
        graph(self.mesh, [[1, 2, 3, 4, 5], [0], [0], [0], [0], [0]])
        self.assertEqual(self.mesh.articulation_nodes(), [0])

    def test_a_disconnected_graph_does_not_confuse_it(self):
        graph(self.mesh, [[1], [0], [3, 4], [2, 4], [2, 3], []])
        self.assertEqual(self.mesh.articulation_nodes(), [])


class Partitioning(unittest.TestCase):
    def test_a_split_severs_links_in_both_directions(self):
        """The bug this exists for: links are not reciprocal, so scanning one way is not enough."""
        mesh = small_mesh(nodes=6)
        graph(mesh, [[1], [0, 2], [1, 3], [2, 4], [3, 5], [4]])
        # Make one crossing link inbound-only, which is what an asymmetry draw produces.
        mesh.neighbours[3].remove(2)
        cut = mesh.partition({0, 1, 2})
        self.assertGreater(cut, 0)
        self.assertNotIn(3, mesh.neighbours[2])
        self.assertNotIn(2, mesh.neighbours[3])
        self.assertEqual([len(c) for c in K.partitions(mesh)], [3, 3])

    def test_partitions_sees_two_components(self):
        mesh = small_mesh(nodes=6)
        graph(mesh, [[1, 2], [0, 2], [0, 1], [4, 5], [3, 5], [3, 4]])
        self.assertEqual([len(c) for c in K.partitions(mesh)], [3, 3])

    def test_an_offline_node_leaves_the_component(self):
        mesh = small_mesh(nodes=6)
        graph(mesh, [[1], [0, 2], [1, 3], [2, 4], [3, 5], [4]])
        mesh.take_down(3)
        self.assertEqual([len(c) for c in K.partitions(mesh)], [3, 2])

    def test_split_on_a_real_mesh_actually_splits_it(self):
        mesh = small_mesh(nodes=30, seed=5)
        self.assertEqual(len(K.partitions(mesh)), 1)
        report = mesh.break_mesh("split")
        self.assertGreater(report["links_cut"], 0)
        self.assertEqual(len(K.partitions(mesh)), 2)


class TakingNodesDown(unittest.TestCase):
    def test_an_offline_node_neither_sends_nor_hears(self):
        mesh = small_mesh(nodes=8)
        mesh.take_down(0)
        self.assertIsNone(mesh.originate(0, 70, 40))
        self.assertEqual(mesh.stats["sends_while_offline"], 1)

        peer = next(i for i in range(1, 8) if 0 in mesh.neighbours[i])
        mesh.originate(peer, 70, 40)
        mesh.run(30000.0)
        self.assertEqual(mesh.nodes[0].nodedb, {}, "a node that is off learns nothing")

    def test_the_rest_of_the_mesh_keeps_believing_in_it(self):
        """The point of take_down: failure is not broadcast, so knowledge goes stale silently."""
        mesh = small_mesh(nodes=8)
        mesh.originate(0, 70, 40)
        mesh.run(30000.0)
        believers = [n for n in mesh.nodes if n.index != 0 and 0 in n.nodedb]
        self.assertTrue(believers, "someone should have heard node 0")
        mesh.take_down(0)
        self.assertTrue(
            all(0 in n.nodedb for n in believers),
            "taking a node down must not reach into anyone else's NodeDB",
        )

    def test_bringing_it_back_keeps_what_it_knew(self):
        mesh = small_mesh(nodes=8)
        mesh.note_heard(0, 3)
        mesh.take_down(0)
        mesh.bring_up(0)
        self.assertIn(3, mesh.nodes[0].nodedb)

    def test_wiping_it_does_not(self):
        mesh = small_mesh(nodes=8)
        mesh.note_heard(0, 3)
        mesh.wipe(0)
        self.assertEqual(mesh.nodes[0].nodedb, {})

    def test_break_modes_do_what_they_say(self):
        mesh = small_mesh(nodes=30, seed=5, router_fraction=0.2)
        report = mesh.break_mesh("routers", count=2)
        self.assertEqual(len(report["taken_down"]), 2)
        for index in report["taken_down"]:
            self.assertTrue(mesh.nodes[index].is_router_like())
            self.assertFalse(mesh.nodes[index].online)

    def test_bridge_mode_says_when_it_fell_back(self):
        """A mesh with no articulation points has no bridges to cut, and must admit it."""
        mesh = small_mesh(nodes=6)
        graph(mesh, [[1, 5], [0, 2], [1, 3], [2, 4], [3, 5], [4, 0]])  # a ring
        report = mesh.break_mesh("bridge", count=1)
        self.assertTrue(report["fell_back_to_degree"])

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaises(ValueError):
            small_mesh(nodes=6).break_mesh("smash")


class Coverage(unittest.TestCase):
    def test_coverage_is_never_more_than_everything(self):
        """The other bug: after a split, records for the far side outlived the reachable set."""
        mesh = small_mesh(nodes=30, seed=5)
        for step in range(40):
            mesh.originate(step % 30, 70, 40)
            mesh.run(mesh.now + 4000)
        mesh.break_mesh("split")
        for index in range(30):
            self.assertLessEqual(K.node_knowledge(mesh, index)["coverage"], 1.0)

    def test_a_split_turns_knowledge_into_stale_records(self):
        mesh = small_mesh(nodes=30, seed=5)
        for step in range(40):
            mesh.originate(step % 30, 70, 40)
            mesh.run(mesh.now + 4000)
        self.assertEqual(K.snapshot(mesh)["stale_records"], 0)
        mesh.break_mesh("split")
        self.assertGreater(K.snapshot(mesh)["stale_records"], 0)

    def test_a_small_store_caps_coverage(self):
        """A 10-slot store on a 30-node mesh cannot hold more than a third of it, by arithmetic."""
        mesh = small_mesh(nodes=30, seed=5, platform_mix="stm32wl")
        for step in range(60):
            mesh.originate(step % 30, 70, 40)
            mesh.run(mesh.now + 4000)
        for index in range(30):
            record = K.node_knowledge(mesh, index)
            self.assertLessEqual(record["known"], 10)
        self.assertGreater(mesh.stats["nodedb_evictions"], 0)

    def test_stale_routes_predict_wasted_directed_deliveries(self):
        mesh = small_mesh(nodes=12)
        peer = mesh.neighbours[0][0]
        dest = next(i for i in range(len(mesh.nodes)) if i not in (0, peer))
        mesh.note_heard(0, peer, hops_away=0)
        mesh.note_heard(0, dest, hops_away=2)
        mesh.nodes[0].nodedb[dest].next_hop = mesh.nodes[peer].relay_byte
        self.assertEqual(K.stale_beliefs(mesh)["stale_routes"], 0)
        mesh.take_down(peer)
        self.assertEqual(K.stale_beliefs(mesh)["stale_routes"], 1)
        self.assertEqual(K.stale_beliefs(mesh)["nodes_holding_one"], 1)


class Recording(unittest.TestCase):
    def test_the_recorder_samples_on_its_timer(self):
        mesh = small_mesh(nodes=10)
        recorder = K.Recorder(mesh, every_ms=1000.0)
        mesh.run(5500.0)
        self.assertGreaterEqual(len(recorder.series), 5)
        self.assertTrue(all("mean_coverage" in s for s in recorder.series))

    def test_events_are_marked_and_sampled(self):
        mesh = small_mesh(nodes=10)
        recorder = K.Recorder(mesh, every_ms=1000.0)
        mesh.run(2000.0)
        mesh.break_mesh("degree", count=1)
        recorder.note("killed the hub")
        self.assertEqual(recorder.events[-1][1], "killed the hub")
        self.assertEqual(recorder.series[-1]["label"], "killed the hub")
        self.assertEqual(recorder.series[-1]["offline"], 1)

    def test_the_summary_names_every_way_a_route_is_lost(self):
        mesh = small_mesh(nodes=10)
        recorder = K.Recorder(mesh, every_ms=1000.0)
        recorder.sample()
        summary = recorder.summary()
        for _, label in K.ROUTE_DEATHS:
            self.assertIn(label, summary)
        for _, label in K.RESOLUTION_FAILURES:
            self.assertIn(label, summary)

    def test_rendering_is_optional_not_required(self):
        """A headless run must not fail because it could not draw a picture."""
        mesh = small_mesh(nodes=10)
        recorder = K.Recorder(mesh, every_ms=1000.0)
        self.assertIsNone(
            recorder.render("/tmp/never-written.png"), "no samples, no plot"
        )


class FirmwareMix(unittest.TestCase):
    def test_a_share_of_nodes_can_stay_on_old_firmware(self):
        mesh = small_mesh(nodes=40, seed=3, legacy_fraction=0.25)
        names = [n.profile.name for n in mesh.nodes]
        self.assertEqual(names.count("legacy"), 10)
        self.assertEqual(names.count("2.8"), 30)

    def test_an_old_node_keeps_the_old_contention_window(self):
        """The mix has to reach the MAC, or it is decoration."""
        mesh = small_mesh(nodes=40, seed=3, legacy_fraction=0.5)
        old = next(i for i, n in enumerate(mesh.nodes) if n.profile.name == "legacy")
        new = next(i for i, n in enumerate(mesh.nodes) if n.profile.name == "2.8")
        mesh.nodes[old].role = mesh.nodes[new].role = M.CLIENT
        floor = 2 * M.CW_MAX * mesh.slot_time_ms()
        self.assertGreaterEqual(mesh.tx_delay_weighted(new, 0.0), floor)
        self.assertTrue(
            any(mesh.tx_delay_weighted(old, 0.0) < floor for _ in range(50)),
            "a pre-2.8 node does not wait behind the router offset",
        )

    def test_an_old_node_still_cancels_a_dupe_as_a_router(self):
        mesh = small_mesh(nodes=40, seed=3, legacy_fraction=0.5)
        old = next(i for i, n in enumerate(mesh.nodes) if n.profile.name == "legacy")
        mesh.nodes[old].role = M.ROUTER
        self.assertTrue(mesh.role_allows_canceling_dupe(old, M.Packet(1, 5, 70, 40)))

    def test_a_mixed_mesh_still_floods(self):
        mesh = small_mesh(nodes=30, seed=5, legacy_fraction=0.4)
        for step in range(20):
            mesh.originate(step % 30, 70, 40)
            mesh.run(mesh.now + 5000)
        self.assertGreater(mesh.stats["receptions"], 0)
        self.assertGreater(mesh.stats["rebroadcasts"], 0)


if __name__ == "__main__":
    unittest.main()
