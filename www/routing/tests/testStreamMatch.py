"""OSM alignment for thalweg geometry and deduplicated crossings."""

import tempfile
import unittest
from pathlib import Path

import numpy as np
from shapely.geometry import LineString

from routing import model
from routing.build import stream_match
from routing.tests import terrainaide as ta
from routing.tests.testGraph import GraphCase, crossings_of, route

WEST = ta.XC - ta.VALLEY


class TestStreamMatchUnit(unittest.TestCase):
    def testParallelChainAlignsToOsm(self):
        dem_line = np.column_stack([np.linspace(100, 100, 20), np.linspace(0, 600, 20)])
        osm = LineString(np.column_stack([np.full(20, 130.0), np.linspace(0, 600, 20)]))
        out = stream_match._align_parallel_chain(dem_line, osm, 50.0)
        self.assertIsNotNone(out)
        self.assertLess(float(LineString(out).distance(osm)), 5.0)
        self.assertAlmostEqual(float(out[0, 0]), 130.0)
        self.assertAlmostEqual(float(out[-1, 0]), 130.0)

    def testCrossingLinePerpendicularDoesNotAlign(self):
        dem_line = np.column_stack([np.linspace(0, 600, 20), np.full(20, 300.0)])
        osm = LineString([(300, 0), (300, 600)])
        self.assertIsNone(stream_match._align_parallel_chain(dem_line, osm, 50.0))

    def testAlignmentKeepsChainDirection(self):
        dem_line = np.array([[100.0, 500.0], [100.0, 100.0]])
        osm = LineString([(130.0, 0.0), (130.0, 600.0)])
        out = stream_match._align_parallel_chain(dem_line, osm, 50.0)
        self.assertIsNotNone(out)
        self.assertGreater(float(out[0, 1]), float(out[-1, 1]))


class TestDemOsmDuplicateCrossingSuppressed(GraphCase):
    def testShiftedOsmRiverDoesNotDuplicateCrossings(self):
        from routing.build.osmdata import OsmData

        _, graph_dem = ta.build(self.cfg, ta.two_valleys(), OsmData())
        n_dem = len(graph_dem.debug["crossings"])
        ys = np.linspace(ta.Y0, ta.Y0 - ta.SIZE * ta.RES, 30)
        osm_line = ta.osm_line(np.full(len(ys), WEST + 40), ys)
        osm = OsmData(waterways=[(osm_line, {"waterway": "river"})])
        _, graph = ta.build(self.cfg, ta.two_valleys(), osm)
        self.assertEqual(len(graph.debug["crossings"]), n_dem)
        thalweg = graph.edge_kind == model.THALWEG
        mid = np.array([graph.geom_xy[graph.geom_off[i]:graph.geom_off[i + 1]].mean(axis=0)
                        for i in np.flatnonzero(thalweg)[:40]])
        near_stream = np.abs(mid[:, 1] - ta.YC) < 800
        self.assertTrue(near_stream.any())
        self.assertLess(float(np.min(np.abs(mid[near_stream, 0] - (WEST + 40)))), 50.0)


class TestStreamRoutingRegression(GraphCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = ta.config(Path(tempfile.mkdtemp()))
        ys = np.linspace(ta.Y0, ta.Y0 - ta.SIZE * ta.RES, 30)
        osm = __import__("routing.build.osmdata", fromlist=["OsmData"]).OsmData(
            waterways=[(ta.osm_line(np.full(len(ys), WEST), ys), {"waterway": "river"})])
        cls.pkg, cls.graph = ta.build(cls.cfg, ta.two_valleys(), osm)

    def testAlongTheStreamItStaysOnOneBank(self):
        path = route(self.pkg, (WEST - 120, ta.YC - 1200), (WEST - 120, ta.YC + 1200))
        self.assertEqual(crossings_of(path, WEST), 0)

    def testAcrossItCrossesOnce(self):
        path = route(self.pkg, (WEST - 120, ta.YC - 1200), (WEST + 120, ta.YC + 1200))
        self.assertEqual(crossings_of(path, WEST), 1)


if __name__ == "__main__":
    unittest.main()
