"""The drawn route: lines of the terrain smoothed, the rest as it is."""

import math
import unittest
from types import SimpleNamespace

import numpy as np

from routing import model
from routing.engine.base import Leg, Path, Snap
from routing.route import SMOOTH_KINDS, _remove_short_detours, smooth_steps, to_geojson


def sharpest_turn(line: np.ndarray) -> float:
    k = math.cos(math.radians(float(line[:, 1].mean())))
    d = np.diff(line[:, :2] * np.array([k, 1.0]), axis=0)
    a = np.degrees(np.arctan2(d[:, 1], d[:, 0]))
    turn = np.abs((np.diff(a) + 180) % 360 - 180)
    return float(turn.max()) if len(turn) else 0.0


class TestSmoothing(unittest.TestCase):
    # a staircase of right angles over three steps, then a straight step
    steps = [np.array([[0.0, 0.0, 0.0], [0.0, 0.001, 10.0], [0.001, 0.001, 20.0]]),
             np.array([[0.001, 0.001, 20.0], [0.001, 0.002, 30.0], [0.002, 0.002, 40.0]]),
             np.array([[0.002, 0.002, 40.0], [0.002, 0.003, 50.0]]),
             np.array([[0.002, 0.003, 50.0], [0.003, 0.003, 50.0]])]

    def testCornersAreCut(self):
        out = smooth_steps(self.steps, [True, True, True, False])
        joined = np.vstack([out[0]] + [o[1:] for o in out[1:3]])
        self.assertGreater(sharpest_turn(np.vstack(self.steps[:3])), 89)
        self.assertLess(sharpest_turn(joined), 45)

    def testRunEndsStayAndStepsStillJoin(self):
        out = smooth_steps(self.steps, [True, True, True, False])
        np.testing.assert_allclose(out[0][0], self.steps[0][0])
        np.testing.assert_allclose(out[2][-1], self.steps[2][-1])
        for a, b in zip(out[:-1], out[1:]):
            np.testing.assert_allclose(a[-1], b[0], atol=1e-12)

    def testOthersAreLeftAlone(self):
        out = smooth_steps(self.steps, [False, False, False, False])
        for a, b in zip(out, self.steps):
            np.testing.assert_array_equal(a, b)

    def testLatticeSideJerkIsRemoved(self):
        xyz = np.array([[0.0, 0.0, 0.0], [100.0, 100.0, 10.0], [200.0, 0.0, 20.0]])
        reduced, t = _remove_short_detours(xyz, np.arange(3, dtype=np.float64))
        np.testing.assert_array_equal(t, [0.0, 2.0])
        np.testing.assert_array_equal(reduced[:, :2], [[0.0, 0.0], [200.0, 0.0]])

    def testLatticeLoopIsRemoved(self):
        xyz = np.array([[0.0, 0.0, 0.0], [100.0, 0.0, 10.0], [0.0, 20.0, 0.0]])
        reduced, _ = _remove_short_detours(xyz, np.arange(3, dtype=np.float64))
        self.assertEqual(len(reduced), 2)

    def testLargeLatticeDetourStays(self):
        xyz = np.array([[0.0, 0.0, 0.0], [100.0, 200.0, 10.0], [200.0, 0.0, 20.0]])
        reduced, _ = _remove_short_detours(xyz, np.arange(3, dtype=np.float64))
        self.assertEqual(len(reduced), 3)

    def testOnlyLatticeStepsLoseJitter(self):
        m = 1.0 / 111_320.0
        steps = [
            np.array([[0.0, 0.0, 0.0], [100 * m, 100 * m, 10.0]]),
            np.array([[100 * m, 100 * m, 10.0], [200 * m, 0.0, 20.0]]),
        ]
        lattice = smooth_steps(steps, [True, True], lattice=[True, True])
        ordinary = smooth_steps(steps, [True, True], lattice=[False, False])
        self.assertLess(np.vstack([lattice[0], lattice[1][1:]])[:, 1].max(), 1e-12)
        self.assertGreater(np.vstack([ordinary[0], ordinary[1][1:]])[:, 1].max(), 20 * m)

    def testBankIsNotSmoothedIntoWater(self):
        self.assertNotIn(model.BANK, SMOOTH_KINDS)

    def testWaypointIsHardSmoothingBoundary(self):
        lines = [
            np.array([[0.0, 0.0, 0.0], [0.001, 0.0, 0.0]]),
            np.array([[0.001, 0.0, 0.0], [0.001, 0.001, 0.0]]),
            np.array([[0.001, 0.001, 0.0], [0.002, 0.001, 0.0]]),
            np.array([[0.002, 0.001, 0.0], [0.002, 0.002, 0.0]]),
        ]
        pkg = SimpleNamespace(
            edge_coords=lambda i, forward: lines[i] if forward else lines[i][::-1],
            edge_len=np.full(4, 100.0),
            edge_kind=np.full(4, model.LATTICE),
            edge_terrain=np.full(4, model.TERRAIN["unknown"]),
            time_fwd=np.full(4, 60.0),
            time_bwd=np.full(4, 60.0),
            node_lon=np.array([0.0, 0.001, 0.002]),
            node_lat=np.array([0.0, 0.001, 0.002]),
            node_ele=np.zeros(3),
            meta={},
        )
        path = Path(
            snaps=[Snap(0.0, 0.0, 0, 0.0), Snap(0.001, 0.001, 1, 0.0), Snap(0.002, 0.002, 2, 0.0)],
            legs=[Leg([(0, True), (1, True)]), Leg([(2, True), (3, True)])],
        )
        coords = to_geojson(pkg, path, "test")["features"][0]["geometry"]["coordinates"]
        self.assertIn([0.001, 0.001, 0.0], coords)


if __name__ == "__main__":
    unittest.main()
