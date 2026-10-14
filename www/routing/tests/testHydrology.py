"""Thalwegs, fall lines and canyons on synthetic DEMs."""

import unittest

import numpy as np

from routing.build import dem as dem_mod
from routing.build import hydrology
from routing.tests import terrainaide as ta


class TestNumpyHydrology(unittest.TestCase):
    def setUp(self):
        self.grid = ta.grid()
        self.dem = ta.two_valleys()
        self.hyd = hydrology.with_numpy(self.dem, self.grid)
        self.down = self.hyd.downstream()

    def testWaterGathersInBothValleys(self):
        x, _ = ta.cell_xy()
        acc = self.hyd.accum_km2
        bottom = acc[-2]  # the southern row, where both valleys drain out
        west = bottom[np.abs(x[-2] - (ta.XC - ta.VALLEY)) < 100].max()
        ridge = bottom[np.abs(x[-2] - ta.XC) < 50].max()
        self.assertGreater(west, 1.0)
        # the crest itself only carries what runs down its own line
        self.assertLess(ridge, 0.05 * west)

    def testThalwegChainsShareTheirEnds(self):
        chains = hydrology.thalweg_chains(self.hyd.accum_km2, self.down, 0.05, 2)
        self.assertTrue(chains)
        heads = {int(c[0]) for c in chains}
        tails = {int(c[-1]) for c in chains}
        # every interior break is a tail of one run and the head of the next
        self.assertTrue(tails & heads)
        for c in chains:
            self.assertGreaterEqual(len(c), 2)
            self.assertLessEqual(len(c), 3)

    def testFallLinesFromTheSaddleReachDifferentValleys(self):
        stream = self.hyd.accum_km2.ravel() >= 0.05
        ends = []
        for dx in (-150.0, 150.0):
            r, c, _ = self.grid.cell_of([ta.XC + dx], [ta.YC])
            path = hydrology.fall_line(self.down, int(r[0] * self.grid.width + c[0]), stream, 1000)
            self.assertIsNotNone(path)
            ends.append(int(path[-1]))
        xs = [self.grid.cell_xy(*divmod(e, self.grid.width))[0] for e in ends]
        self.assertLess(xs[0], ta.XC)
        self.assertGreater(xs[1], ta.XC)


class TestCanyons(unittest.TestCase):
    def detect(self, wall_grade: float, depth: float) -> bool:
        """A straight north-south incision of *depth* m with walls of
        *wall_grade*, in otherwise flat ground draining south."""
        grid = ta.grid()
        x, y = ta.cell_xy()
        dist = np.abs(x - ta.XC)
        dem = np.minimum(dist * wall_grade, depth).astype(np.float32) + 0.01 * (y - y.min())
        gx, gy = dem_mod.gradients(dem, grid.res)
        slope = dem_mod.slope_deg(gx, gy)
        drain = np.full(dem.shape, 6, np.int8)  # everything flows south
        r, c, _ = grid.cell_of([ta.XC], [ta.YC])
        cell = np.array([r[0] * grid.width + c[0]])
        return bool(hydrology.canyon_cells(dem, slope, drain, cell, grid.res, 38, 60, 25)[0])

    def testSteepDeepWallsAreACanyon(self):
        # walls 1.5 (56 degrees) up to 150 m: still rising across the whole band
        self.assertTrue(self.detect(wall_grade=1.5, depth=150))

    def testGentleValleyIsNot(self):
        self.assertFalse(self.detect(wall_grade=0.3, depth=150))

    def testShallowGullyIsNot(self):
        self.assertFalse(self.detect(wall_grade=1.5, depth=10))



class TestDivides(unittest.TestCase):
    def testBetweenBasinsNotAlongWater(self):
        g = ta.grid()
        labels = np.zeros((6, 6), dtype=np.int64)
        labels[:, 3:] = 1  # two basins side by side
        lines = hydrology.divide_lines(labels, g)
        self.assertEqual(len(lines), 1)
        self.assertTrue(np.allclose(lines[0][:, 0], g.x0 + 3 * g.res))
        # a lake over the lower half: the divide stops at its shore, and no
        # line runs along the shore or across the water
        labels[3:, :] = -1
        lines = hydrology.divide_lines(labels, g)
        self.assertEqual(len(lines), 1)
        self.assertGreaterEqual(lines[0][:, 1].min(), g.y0 - 3 * g.res)

    def testMainStemKeepsItsLabel(self):
        # a stream down column 2, a tributary joining from column 0 at row 3
        acc = np.zeros((6, 4), dtype=np.float32)
        down = np.full(24, -1, dtype=np.int64)
        for r in range(5):
            down[r * 4 + 2] = (r + 1) * 4 + 2
            acc[r, 2] = 10 + r * 2
        acc[5, 2] = 30
        down[3 * 4 + 0], down[3 * 4 + 1] = 3 * 4 + 1, 3 * 4 + 2
        acc[3, 0], acc[3, 1] = 2, 3
        labels = hydrology.basin_labels(acc, down, 1.0)
        main = labels[:, 2]
        self.assertTrue((main == main[0]).all())
        self.assertNotEqual(labels[3, 0], main[0])


if __name__ == "__main__":
    unittest.main()
