"""The walking-time model: Tobler, terrain, side slope, closed ground."""

import tempfile
import unittest
from pathlib import Path

import numpy as np

from routing import model
from routing.cost import CostModel, Rasters, tobler_kmh
from routing.grid import Grid
from routing.tests import terrainaide


def plane(grade_east: float, size: int = 40) -> Rasters:
    """A plane rising *grade_east* metres per metre to the east."""
    grid = Grid(0.0, size * 30.0, 30.0, size, size, terrainaide.EPSG)
    x, _ = grid.cell_xy(*np.mgrid[0:size, 0:size])
    dem = (100 + grade_east * x).astype(np.float32)
    gx = np.full(dem.shape, grade_east, np.float32)
    gy = np.zeros(dem.shape, np.float32)
    terrain = np.full(dem.shape, model.TERRAIN["grass"], np.uint8)
    return Rasters(grid, dem, gx, gy, terrain)


def costs(cm: CostModel, rasters: Rasters, a, b, kind: int):
    xy = np.array([a, b], dtype=np.float64)
    off = np.array([0, 2])
    return cm.edge_costs(rasters, xy, off, np.array([kind], np.uint8), np.array([1.0]))


class TestTobler(unittest.TestCase):
    def testPeaksOnAGentleDescent(self):
        self.assertAlmostEqual(float(tobler_kmh(-0.05)), 6.0)
        self.assertLess(float(tobler_kmh(0.0)), 6.0)

    def testDownhillFasterThanUphill(self):
        self.assertGreater(float(tobler_kmh(-0.2)), float(tobler_kmh(0.2)))


class TestEdgeCosts(unittest.TestCase):
    def setUp(self):
        self.cfg = terrainaide.config(Path(tempfile.mkdtemp()))
        self.cm = CostModel(self.cfg)

    def testUpTheFallLineIsSlowerThanDown(self):
        r = plane(0.3)
        c = costs(self.cm, r, (100, 600), (700, 600), model.THALWEG)
        self.assertAlmostEqual(c.length[0], 600.0, places=3)
        self.assertGreater(c.time_fwd[0], c.time_bwd[0])
        # flat-ground grass would be 600 m at 0.8 * Tobler(0)
        expected_up = 600 / (float(tobler_kmh(0.3)) / 3.6 * self.cfg.terrain_speed["grass"])
        self.assertAlmostEqual(c.time_fwd[0], expected_up, delta=1.0)

    def testContouringASteepSlopeIsSlow(self):
        r = plane(0.5)  # 26.6 degrees, walked across: all of it side slope
        across = costs(self.cm, r, (600, 100), (600, 700), model.LATTICE)
        flat = costs(self.cm, plane(0.0), (600, 100), (600, 700), model.LATTICE)
        self.assertGreater(across.time_fwd[0], 1.5 * flat.time_fwd[0])

    def testTooSteepGroundIsClosedOffTrail(self):
        r = plane(1.0)  # 45 degrees, steeper than max_slope_deg = 40
        c = costs(self.cm, r, (600, 100), (600, 700), model.LATTICE)
        self.assertTrue(np.isinf(c.time_fwd[0]) and np.isinf(c.time_bwd[0]))

    def testTrailIgnoresTheGroundUnderIt(self):
        r = plane(1.0)
        c = costs(self.cm, r, (600, 100), (600, 700), model.OSM)
        self.assertTrue(np.isfinite(c.time_fwd[0]))
        self.assertEqual(c.terrain[0], model.TERRAIN["path"])

    def testSteepTraverseWeighsMoreThanItTakes(self):
        # 0.6 is 31 degrees: 6 above penalty_from_deg, so x1.6 at 1.0 per 10
        c = costs(self.cm, plane(0.6), (600, 100), (600, 700), model.LATTICE)
        self.assertAlmostEqual(c.weight_fwd[0] / c.time_fwd[0], 1 + 0.1 * (np.degrees(np.arctan(0.6)) - 25),
                               places=3)
        self.assertAlmostEqual(c.weight_bwd[0] / c.time_bwd[0], c.weight_fwd[0] / c.time_fwd[0], places=6)

    def testGentleTraverseIsNotPenalised(self):
        c = costs(self.cm, plane(0.3), (600, 100), (600, 700), model.LATTICE)  # 17 degrees
        self.assertAlmostEqual(c.weight_fwd[0], c.time_fwd[0], places=6)

    def testClimbingStraightUpIsNoTraverse(self):
        c = costs(self.cm, plane(0.6), (100, 600), (700, 600), model.LATTICE)
        self.assertAlmostEqual(c.weight_fwd[0], c.time_fwd[0], places=6)

    def testTrailsAndFallLinesAreNeverTraverses(self):
        for kind in (model.OSM, model.THALWEG):
            c = costs(self.cm, plane(0.6), (600, 100), (600, 700), kind)
            self.assertAlmostEqual(c.weight_fwd[0], c.time_fwd[0], places=6)

    def testSteepGlacierIsCrevassed(self):
        r = plane(0.4)  # 21.8 degrees, walked straight up: no traverse
        r.terrain[:] = model.TERRAIN["glacier"]
        c = costs(self.cm, r, (100, 600), (700, 600), model.LATTICE)
        p = self.cfg.crevasse
        share = (np.degrees(np.arctan(0.4)) - p.glacier_from_deg) / (p.glacier_full_deg - p.glacier_from_deg)
        self.assertAlmostEqual(c.weight_fwd[0] / c.time_fwd[0], 1 + p.glacier_penalty * share, places=3)
        gentle = plane(0.1)  # 5.7 degrees: below glacier_from_deg
        gentle.terrain[:] = model.TERRAIN["glacier"]
        c = costs(self.cm, gentle, (100, 600), (700, 600), model.LATTICE)
        self.assertAlmostEqual(c.weight_fwd[0], c.time_fwd[0], places=6)

    def testGlacierTrailIsNotPenalisedButAMappedFieldIs(self):
        r = plane(0.4)
        r.terrain[:] = model.TERRAIN["glacier"]
        c = costs(self.cm, r, (100, 600), (700, 600), model.OSM)
        self.assertAlmostEqual(c.weight_fwd[0], c.time_fwd[0], places=6)
        flat = plane(0.0)
        plain = costs(self.cm, flat, (100, 600), (700, 600), model.OSM)
        flat.crevasse = np.ones(flat.dem.shape, np.uint8)
        field = costs(self.cm, flat, (100, 600), (700, 600), model.OSM)
        p = self.cfg.crevasse
        self.assertAlmostEqual(field.time_fwd[0] / plain.time_fwd[0], 1 + p.osm_slowdown, places=4)
        self.assertAlmostEqual(field.weight_fwd[0] / field.time_fwd[0], 1 + p.osm_penalty, places=4)

    def testScrubSlowerThanWoodSlowerThanGrass(self):
        times = {}
        for name in ("grass", "wood", "scrub"):
            r = plane(0.0)
            r.terrain[:] = model.TERRAIN[name]
            times[name] = costs(self.cm, r, (100, 600), (700, 600), model.LATTICE).time_fwd[0]
        self.assertLess(times["grass"], times["wood"])
        self.assertLess(times["wood"], times["scrub"])


if __name__ == "__main__":
    unittest.main()
