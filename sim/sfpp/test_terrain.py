"""Pins the ground: the index against the vendored answer, and the wiring against the flat world.

The parity tests here are the important ones. `IndexedTerrainGrid` exists only because the vendored
`TerrainGrid.elevation_at` sorts every sample on every call, so it must answer the identical
question - a faster grid that quietly interpolates differently would move every result that rests
on it, and nothing downstream would say so.

Run from `sim/`:  python3 -m unittest sfpp.test_terrain -v
"""

import random
import unittest

from . import mesh as M
from . import terrain as T


class IndexMatchesVendored(unittest.TestCase):
    """The index is an optimisation, not a second model. It answers what the vendored grid answers."""

    def _assert_parity(self, rows, queries, reach=6000.0, seed=9):
        from lib.terrain import TerrainGrid

        vendored = TerrainGrid.from_rows(rows)
        indexed = T.IndexedTerrainGrid.from_rows(rows)
        rng = random.Random(seed)
        for _ in range(queries):
            x, y = rng.uniform(-reach, reach), rng.uniform(-reach, reach)
            self.assertAlmostEqual(
                vendored.elevation_at(x, y), indexed.elevation_at(x, y), places=6
            )

    def test_terrain_index_matches_vendored(self):
        """The real shape of the input: a regular grid, which is what SRTM and the presets are."""
        self._assert_parity(T.synthetic_terrain_rows("alpine", 8000.0, 3), queries=250)

    def test_terrain_index_matches_vendored_on_a_real_preset(self):
        self._assert_parity(T.load("batumi").terrain_rows, queries=150, reach=16000.0)

    def test_terrain_index_matches_vendored_on_scattered_samples(self):
        """Irregular scatter is the case that broke it: the ring search has to keep widening until
        it has actually covered every occupied bucket, and counting cells against the bucket count
        does not prove that. It once exited with no candidate at all and divided by zero."""
        rng = random.Random(4)
        rows = [
            (rng.uniform(-5000, 5000), rng.uniform(-5000, 5000), rng.uniform(0, 900))
            for _ in range(60)
        ]
        self._assert_parity(rows, queries=120)

    def test_exact_sample_hits_return_that_sample(self):
        rows = [(0.0, 0.0, 10.0), (1000.0, 0.0, 200.0), (0.0, 1000.0, 300.0)]
        grid = T.IndexedTerrainGrid.from_rows(rows)
        for x, y, z in rows:
            self.assertAlmostEqual(grid.elevation_at(x, y), z, places=6)


class FlatIsUntouched(unittest.TestCase):
    """No scenario means the world this simulator had before the ground existed, exactly."""

    def test_no_scenario_leaves_nodes_at_sea_level(self):
        mesh = _mesh(scenario=None)
        self.assertIsNone(mesh.terrain)
        self.assertTrue(all(n.ground_m == 0.0 for n in mesh.nodes))
        self.assertTrue(all(n.altitude == 0.0 for n in mesh.nodes))

    def test_no_scenario_charges_no_obstruction(self):
        mesh = _mesh(scenario=None)
        self.assertEqual(mesh.loss_terms["terrain_db"], 0.0)
        self.assertEqual(mesh.loss_terms["clutter_db"], 0.0)

    def test_terrain_never_improves_a_link(self):
        """Obstruction is loss. A ridge cannot make a pair hear each other better."""
        flat = _mesh(scenario=None, seed=3)
        hilly = _mesh(scenario="alpine", seed=3)
        n = len(flat.nodes)
        self.assertEqual(n, len(hilly.nodes))
        for i in range(n):
            for j in range(n):
                if i != j:
                    self.assertLessEqual(hilly.rssi[i][j], flat.rssi[i][j] + 1e-9)


class GroundIsLoaded(unittest.TestCase):
    def test_landform_puts_nodes_at_different_heights(self):
        mesh = _mesh(scenario="alpine")
        heights = {round(n.ground_m) for n in mesh.nodes}
        self.assertGreater(len(heights), 1, "alpine ground left every node at one height")
        self.assertGreater(mesh.loss_terms["terrain_db"], 0.0)

    def test_antenna_height_is_above_ground_not_above_the_sea(self):
        """The path-loss models take a height term; handing them altitude makes every node a peak."""
        mesh = _mesh(scenario="alpine")
        for node in mesh.nodes:
            self.assertLess(node.antenna_height_m, 100.0)
            self.assertAlmostEqual(
                node.altitude, node.ground_m + node.antenna_height_m, places=6
            )

    def test_flattening_the_ground_keeps_the_geometry(self):
        """--no-terrain is the paired run that prices terrain alone, so the nodes must not move."""
        with_ground = _mesh(scenario="alpine", use_terrain=True)
        without = _mesh(scenario="alpine", use_terrain=False)
        self.assertEqual(
            [(n.x, n.y) for n in with_ground.nodes], [(n.x, n.y) for n in without.nodes]
        )
        self.assertEqual(without.loss_terms["terrain_db"], 0.0)
        self.assertGreater(with_ground.loss_terms["terrain_db"], 0.0)


class RealGeometryDecides(unittest.TestCase):
    def test_a_real_scenario_sets_its_own_node_count(self):
        scenario = T.load("batumi")
        self.assertTrue(scenario.fixed_geometry)
        mesh = _mesh(scenario="batumi", node_count=7, area=40000.0)
        self.assertEqual(len(mesh.nodes), scenario.node_count)

    def test_stretch_is_refused_rather_than_ignored(self):
        """Moving a real mesh's nodes apart makes it somewhere else under the same label."""
        with self.assertRaises(ValueError):
            _mesh(scenario="batumi", area=40000.0, stretch=1.5)

    def test_a_generated_mesh_still_stretches(self):
        mesh = _mesh(scenario="alpine", stretch=1.5)
        self.assertEqual(mesh.stretch, 1.5)


def _mesh(scenario, node_count=25, area=8000.0, seed=5, use_terrain=True, **kwargs):
    conf = M.make_config()
    scn = T.load(scenario, area=area, seed=seed) if scenario is not None else None
    grid = T.apply(conf, scn, terrain=use_terrain) if scn is not None else None
    return M.build(
        conf,
        node_count,
        area,
        random.Random(seed),
        scenario=scn,
        terrain=grid,
        **kwargs,
    )


if __name__ == "__main__":
    unittest.main()
