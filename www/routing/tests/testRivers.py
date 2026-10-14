"""River sizes: glacier meltwater and catchments that reach past the DEM."""

import tempfile
import unittest
from pathlib import Path

import numpy as np
from shapely.geometry import box

from routing.build import hydrology
from routing.build.osmdata import OsmData
from routing.build.rivers import RiverSize, effective_catchment, weighted_accumulation
from routing.grid import Grid
from routing.tests import terrainaide as ta

WEST = ta.XC - ta.VALLEY


class TestAccumulation(unittest.TestCase):
    def setUp(self):
        self.grid = ta.grid()
        self.hyd = hydrology.with_numpy(ta.two_valleys(), self.grid)

    def testUnitWeightIsTheCatchment(self):
        acc = weighted_accumulation(self.hyd, np.full(self.grid.shape, self.grid.cell_km2))
        np.testing.assert_allclose(acc, self.hyd.accum_km2, rtol=1e-4)

    def testGlacierCountsExtra(self):
        x, y = ta.cell_xy()
        glacier = (y > ta.YC) & (np.abs(x - WEST) < 600)  # the head of the west valley
        plain = effective_catchment(self.grid, self.hyd, glacier, 0.0)
        eff = effective_catchment(self.grid, self.hyd, glacier, 3.0)
        ice_km2 = glacier.sum() * self.grid.cell_km2
        # every glaciated cell's extra reaches exactly one outlet of the grid
        outlets = (self.hyd.downstream() < 0).reshape(plain.shape)
        self.assertAlmostEqual(float((eff - plain)[outlets].sum()), 3.0 * ice_km2, places=6)
        # and none of it the other side of the ridge
        east = x > ta.XC
        np.testing.assert_allclose(eff[east], plain[east])


class TestRiverSize(unittest.TestCase):
    def testLargestLayerWins(self):
        fine = Grid(0, 300, 30, 10, 10, ta.EPSG)
        coarse = Grid(-900, 1200, 90, 25, 25, ta.EPSG)
        sizes = RiverSize().add(fine, np.full(fine.shape, 2.0)).add(coarse, np.full(coarse.shape, 40.0))
        self.assertEqual(float(sizes.at(150, 150)[0]), 40.0)
        self.assertEqual(float(sizes.at(5000, 5000)[0]), 0.0)  # off both grids


class TestGlacierRivers(unittest.TestCase):
    def build(self, glacier_factor: float):
        cfg = ta.config(Path(tempfile.mkdtemp()), ford={"max_catchment_km2": 10, "glacier_factor": glacier_factor})
        ys = np.linspace(ta.Y0, ta.Y0 - ta.SIZE * ta.RES, 30)
        osm = OsmData(waterways=[(ta.osm_line(np.full(len(ys), WEST), ys), {"waterway": "river"})])
        lon, lat = ta.lonlat([WEST - 600, WEST + 600], [ta.YC, ta.Y0])
        ice = box(float(lon[0]), float(lat[0]), float(lon[1]), float(lat[1]))
        _, graph = ta.build(cfg, ta.two_valleys(), osm, terrain_areas={"glacier": [ice]})
        return sum(1 for _, label in graph.debug["barriers"] if label == "water")

    def testMeltwaterMakesTheRiverUnfordable(self):
        self.assertEqual(self.build(0.0), 0)  # a few km2: waded anywhere
        self.assertGreater(self.build(20.0), 0)  # the same basin under ice: not below it


if __name__ == "__main__":
    unittest.main()
