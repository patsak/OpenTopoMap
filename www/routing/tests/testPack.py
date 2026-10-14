"""Walking with a backpack: slight on a flat trail, heavy on climbs and hard ground."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("numba")

from routing import model, pack  # noqa: E402
from routing.config import PackParams  # noqa: E402
from routing.cost import CostModel  # noqa: E402
from routing.server import create_app  # noqa: E402
from routing.tests import terrainaide as ta  # noqa: E402
from routing.tests.testCost import costs, plane  # noqa: E402

P = PackParams()


def as_package(c, extra=0.0):
    """EdgeCosts -> just what routing.pack reads off a package."""
    return SimpleNamespace(
        time_fwd=c.time_fwd, time_bwd=c.time_bwd, weight_fwd=c.weight_fwd, weight_bwd=c.weight_bwd,
        climb_fwd=c.climb_fwd, climb_bwd=c.climb_bwd, descent_fwd=c.descent_fwd,
        descent_bwd=c.descent_bwd, rough_fwd=c.rough_fwd, rough_bwd=c.rough_bwd,
        steep_fwd=c.steep_fwd, steep_bwd=c.steep_bwd,
        edge_extra=np.full(len(c.time_fwd), extra),
    )


class TestComponents(unittest.TestCase):
    def setUp(self):
        self.cm = CostModel(ta.config(Path(tempfile.mkdtemp())))

    def testClimbAndDescentAreTimeWeightedGradient(self):
        c = costs(self.cm, plane(0.3), (100, 600), (700, 600), model.OSM)  # up 30% eastwards
        self.assertAlmostEqual(c.climb_fwd[0], 3.0 * c.time_fwd[0], places=3)
        self.assertAlmostEqual(c.descent_bwd[0], 3.0 * c.time_bwd[0], places=3)
        self.assertEqual(c.descent_fwd[0], 0.0)
        self.assertEqual(c.climb_bwd[0], 0.0)
        self.assertEqual(c.rough_fwd[0], 0.0)  # a plain path: no roughness

    def testRoughnessIsWhatTheGroundTakesAway(self):
        r = plane(0.0)
        r.terrain[:] = model.TERRAIN["scrub"]
        c = costs(self.cm, r, (100, 600), (700, 600), model.LATTICE)
        self.assertAlmostEqual(c.rough_fwd[0], (1 - 0.3) * c.time_fwd[0], places=3)


class TestTimes(unittest.TestCase):
    def setUp(self):
        self.cm = CostModel(ta.config(Path(tempfile.mkdtemp())))

    def ratio(self, c, kg=20.0, forward=True):
        tf, tb = pack.times(as_package(c), P, kg)
        return (tf / c.time_fwd)[0] if forward else (tb / c.time_bwd)[0]

    def testNoLoadNoChange(self):
        c = costs(self.cm, plane(0.3), (100, 600), (700, 600), model.OSM)
        tf, tb = pack.times(as_package(c), P, 0)
        self.assertIs(tf, c.time_fwd)

    def testFlatTrailBarelyNotices(self):
        c = costs(self.cm, plane(0.0), (100, 600), (700, 600), model.OSM)
        self.assertAlmostEqual(self.ratio(c), 1 + 2 * P.flat_per_10kg, places=6)  # 20 kg: +6%

    def testClimbsSlowMostDescentsLess(self):
        c = costs(self.cm, plane(0.3), (100, 600), (700, 600), model.OSM)
        up, down = self.ratio(c), self.ratio(c, forward=False)
        self.assertAlmostEqual(up, 1 + 2 * (P.flat_per_10kg + 3 * P.climb_per_10kg), places=6)
        self.assertAlmostEqual(down, 1 + 2 * (P.flat_per_10kg + 3 * P.descent_per_10kg), places=6)
        flat = 1 + 2 * P.flat_per_10kg
        self.assertGreater(up, down)
        self.assertGreater(down, flat)

    def testHardGroundSlowsMore(self):
        grass, scrub = plane(0.0), plane(0.0)
        grass.terrain[:] = model.TERRAIN["grass"]
        scrub.terrain[:] = model.TERRAIN["scrub"]
        r_grass = self.ratio(costs(self.cm, grass, (100, 600), (700, 600), model.LATTICE))
        r_scrub = self.ratio(costs(self.cm, scrub, (100, 600), (700, 600), model.LATTICE))
        self.assertAlmostEqual(r_scrub, 1 + 2 * (P.flat_per_10kg + 0.7 * P.rough_per_10kg), places=4)
        self.assertGreater(r_scrub, r_grass)

    def testClosedStaysClosed(self):
        c = costs(self.cm, plane(1.0), (600, 100), (600, 700), model.LATTICE)  # too steep
        tf, tb = pack.times(as_package(c), P, 20)
        self.assertTrue(np.isinf(tf[0]) and np.isinf(tb[0]))

    def testWeightScalesButNotItsExtra(self):
        c = costs(self.cm, plane(0.3), (100, 600), (700, 600), model.OSM)
        c.weight_fwd = c.weight_fwd + 60.0  # a connector's 60 s
        wf, _ = pack.weights(as_package(c, extra=60.0), P, 20)
        tf, _ = pack.times(as_package(c), P, 20)
        self.assertAlmostEqual(wf[0], tf[0] + 60.0, places=3)


    def testSteepDescentWeighsMoreWithALoad(self):
        c = costs(self.cm, plane(0.6), (600, 700), (600, 100), model.LATTICE)  # straight down 31 degrees
        c.steep_fwd = np.array([100.0])
        c.weight_fwd = c.weight_fwd + 100.0
        wf, _ = pack.weights(as_package(c), P, 20)
        tf, _ = pack.times(as_package(c), P, 20)
        ratio = tf[0] / c.time_fwd[0]
        self.assertAlmostEqual(wf[0], c.weight_fwd[0] * ratio + 2 * P.steep_descent_per_10kg * 100.0, places=3)


class TestService(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = ta.config(Path(tempfile.mkdtemp()))
        pkg, _ = ta.build(cls.cfg, ta.two_valleys())
        ta.write(cls.cfg, pkg)
        lon, lat = ta.lonlat([ta.XC - 1200, ta.XC + 1200], [ta.YC, ta.YC])
        cls.body = {"type": "LineString", "coordinates": [[float(lon[0]), float(lat[0])],
                                                          [float(lon[1]), float(lat[1])]]}

    def setUp(self):
        self.client = create_app(self.cfg).test_client()

    def route(self, **query):
        return self.client.post("/route/api/v1/route", data=json.dumps(self.body),
                                content_type="application/json", query_string=query)

    def testHeavierIsSlower(self):
        light = self.route().get_json()["features"][0]["properties"]
        heavy = self.route(pack_kg=25).get_json()["features"][0]["properties"]
        self.assertEqual(heavy["pack_kg"], 25.0)
        # slower - though not necessarily by the whole load's worth: a load
        # weighs climbs, rough ground and steep descents, not wading or
        # stepping off a line, so the heavy route may well be another one
        self.assertGreater(heavy["duration_s"], light["duration_s"])

    def testBadLoads(self):
        self.assertEqual(self.route(pack_kg="heavy").status_code, 400)
        self.assertEqual(self.route(pack_kg=-1).status_code, 400)
        self.assertEqual(self.route(pack_kg=P.max_kg + 1).status_code, 400)


if __name__ == "__main__":
    unittest.main()
