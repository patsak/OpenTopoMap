"""The off-trail graph on synthetic terrain, compiled and routed with the disk engine."""

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pytest
import shapely

# Collected by the whole-www pytest run too, whose venvs need not have numba.
pytest.importorskip("numba")

from routing import model
from routing.build.graph import _offset
from routing.build.osmdata import OsmData
from routing.engine.base import Options, RouteError
from routing.grid import Projector
from routing.tests import terrainaide as ta

WEST = ta.XC - ta.VALLEY
EAST = ta.XC + ta.VALLEY
NORTH_EDGE = ta.Y0
SOUTH_EDGE = ta.Y0 - ta.SIZE * ta.RES


def route(pkg, *xy, allow_offroad=True, pack_kg=0.0):
    """The route through *xy*, with what it was routed on: ``path.graph``
    (the compiled graph its edge ids index) and ``path.step_time``."""
    lon, lat = ta.lonlat([p[0] for p in xy], [p[1] for p in xy])
    engine = ta.engine(pkg)
    options = Options(allow_offroad=allow_offroad, pack_kg=pack_kg)
    path = engine.route(list(zip(lon.tolist(), lat.tolist())), options)
    path.graph = engine.graph
    path.step_time = engine.edge_time(path, options)
    return path


def coords_of(path) -> np.ndarray:
    """The route's vertices in the grid's CRS: (x, y, ele)."""
    coords = np.vstack([path.graph.edge_coords(i, f) for leg in path.legs for i, f in leg.edges])
    x, y = Projector(ta.EPSG).to_xy(coords[:, 0], coords[:, 1])
    return np.column_stack([x, y, coords[:, 2]])


def kinds_of(path) -> set[int]:
    return set(np.asarray(path.graph.edge_kind[edges_of(path)]).tolist())


def edges_of(path):
    return [i for leg in path.legs for i, _ in leg.edges]


class GraphCase(unittest.TestCase):
    def setUp(self):
        self.cfg = ta.config(Path(tempfile.mkdtemp()))


class TestPasses(GraphCase):
    def setUp(self):
        super().setUp()
        osm = OsmData(passes=[ta.osm_point(1, ta.XC, ta.YC, name="Saddle")])
        self.pkg, self.graph = ta.build(self.cfg, ta.two_valleys(), osm)

    def testPassJoinsBothValleys(self):
        [record] = self.graph.debug["passes"]
        self.assertEqual(record["descents"], 2)
        self.assertGreater(int((self.pkg.edge_kind == model.PASS).sum()), 2)

    def testRouteBetweenValleysGoesOverTheSaddle(self):
        path = route(self.pkg, (WEST, ta.YC + 300), (EAST, ta.YC + 300))
        xyz = coords_of(path)
        ele = xyz[:, 2]
        tilt = 0.05 * (ta.YC + 300 - SOUTH_EDGE)
        crest_here = 400 + 150 * (1 - 0.6 * np.exp(-((300 / 500.0) ** 2))) + tilt
        self.assertLess(ele.max(), crest_here - 5)
        # and it crosses the ridge at the saddle, not 300 m north where it started
        x, y = xyz[:, 0], xyz[:, 1]
        on_crest = np.abs(x - ta.XC) < 50
        self.assertTrue(on_crest.any())
        self.assertLess(np.abs(y[on_crest] - ta.YC).min(), 150)


class TestSteepPass(GraphCase):
    """A ridge too steep to walk anywhere (walls up to ~57 degrees, DEM cliffs
    along them) with a saddle: only the pass gets a route across."""

    def build(self, with_pass: bool):
        osm = OsmData(passes=[ta.osm_point(1, ta.XC, ta.YC, name="Steep")] if with_pass else [])
        return ta.build(self.cfg, ta.two_valleys(saddle=0.5, height=2400.0), osm)

    def testNoWayOverWithoutAPass(self):
        pkg, graph = self.build(with_pass=False)
        self.assertIn("steep", {label for _, label in graph.debug["barriers"]})
        with self.assertRaises(RouteError):
            route(pkg, (WEST, ta.YC), (EAST, ta.YC))

    def testThePassIsCrossedHoweverSteep(self):
        pkg, graph = self.build(with_pass=True)
        [record] = graph.debug["passes"]
        self.assertEqual(record["descents"], 2)
        path = route(pkg, (WEST, ta.YC), (EAST, ta.YC))
        times = [path.step_time(i, f) for leg in path.legs for i, f in leg.edges]
        self.assertTrue(np.isfinite(times).all())
        # over the ridge where the pass is, inside its zone
        x, y, _ = coords_of(path).T
        crest = np.abs(x - ta.XC) < 60
        self.assertTrue(crest.any())
        self.assertLess(np.abs(y[crest] - ta.YC).max(), self.cfg.graph.pass_zone_apex_m / 2 + 100)
        # steep going is slow but never slower than steep_min_speed_kmh
        floor = self.cfg.graph.steep_min_speed_kmh / 3.6
        for (i, f), t in zip(((i, f) for leg in path.legs for i, f in leg.edges), times):
            self.assertLessEqual(t, path.graph.edge_len[i] / floor + 1e-3)

    def testZoneIsASectorWidening_downhill(self):
        _, graph = self.build(with_pass=True)
        zones = graph.debug["pass_zones"]
        self.assertTrue(zones)
        zone = shapely.union_all(zones)
        apex = self.cfg.graph.pass_zone_apex_m
        near = zone.intersection(shapely.box(ta.XC - 30, ta.Y0 - ta.SIZE * ta.RES, ta.XC + 30, ta.Y0))
        # at the ridge it is about the apex wide...
        self.assertAlmostEqual(near.bounds[3] - near.bounds[1], apex, delta=60)
        # ...and wider down in the valleys, at 45 degrees overall
        far = zone.intersection(shapely.box(ta.XC + 600, ta.Y0 - ta.SIZE * ta.RES, ta.XC + 660, ta.Y0))
        self.assertAlmostEqual(far.bounds[3] - far.bounds[1], apex + 2 * 630 * np.tan(np.radians(22.5)), delta=80)

    def testMappedCliffIsWalkedRound(self):
        # an OSM cliff square across the east descent, 400 m long
        cliff = ta.osm_line([ta.XC + 500, ta.XC + 500], [ta.YC - 200, ta.YC + 200])
        osm = OsmData(passes=[ta.osm_point(1, ta.XC, ta.YC)], barrier_lines=[(cliff, "natural=cliff")])
        pkg, graph = ta.build(self.cfg, ta.two_valleys(saddle=0.5, height=2400.0), osm)
        path = route(pkg, (WEST, ta.YC), (EAST, ta.YC))
        line = shapely.LineString(coords_of(path)[:, :2])
        wall = shapely.LineString([(ta.XC + 500, ta.YC - 200), (ta.XC + 500, ta.YC + 200)]).buffer(10)
        self.assertFalse(line.intersects(wall))


    def testACliffAcrossTheWholeZoneIsClimbed(self):
        # an OSM cliff right across the east side, edge to edge: no way round
        cliff = ta.osm_line([ta.XC + 500, ta.XC + 500], [SOUTH_EDGE, NORTH_EDGE])
        osm = OsmData(passes=[ta.osm_point(1, ta.XC, ta.YC)], barrier_lines=[(cliff, "natural=cliff")])
        pkg, graph = ta.build(self.cfg, ta.two_valleys(saddle=0.5, height=2400.0), osm)
        self.assertTrue(graph.cliff_crossed.any())
        path = route(pkg, (WEST, ta.YC), (EAST, ta.YC))
        line = shapely.LineString(coords_of(path)[:, :2])
        self.assertTrue(line.intersects(shapely.LineString([(ta.XC + 500, SOUTH_EDGE), (ta.XC + 500, NORTH_EDGE)])))
        # an hour of rope work on the clock for it
        climbed = graph.cliff_crossed & np.isfinite(pkg.time_fwd)
        self.assertTrue((pkg.time_fwd[climbed] >= self.cfg.graph.pass_cliff_time_s).all())

    def testNoClimbingOutsideAPassZone(self):
        cliff = ta.osm_line([WEST + 300, WEST + 300], [SOUTH_EDGE, NORTH_EDGE])
        osm = OsmData(barrier_lines=[(cliff, "natural=cliff")])
        pkg, graph = ta.build(self.cfg, ta.two_valleys(), osm)
        self.assertFalse(graph.cliff_crossed.any())
        with self.assertRaises(RouteError):
            route(pkg, (WEST, ta.YC), (WEST + 600, ta.YC))


class TestCrevasses(GraphCase):
    def testMappedFieldIsWalkedRound(self):
        """A crevasse field across the middle of the west valley floor, with
        room to pass it on the valley side: the route goes round."""
        x, y = ta.cell_xy()
        field = ((np.abs(x - WEST) < 120) & (np.abs(y - ta.YC) < 150)).astype(np.uint8)
        pkg, _ = ta.build(self.cfg, ta.two_valleys(), crevasse=field)
        path = route(pkg, (WEST, ta.YC - 900), (WEST, ta.YC + 900))
        line = shapely.LineString(coords_of(path)[:, :2])
        core = shapely.box(WEST - 60, ta.YC - 90, WEST + 60, ta.YC + 90)
        self.assertFalse(line.intersects(core))


class TestRivers(GraphCase):
    def river(self, width: str | None, fords=()):
        ys = np.linspace(NORTH_EDGE, SOUTH_EDGE, 30)
        tags = {"waterway": "river"} | ({"width": width} if width else {})
        osm = OsmData(waterways=[(ta.osm_line(np.full(len(ys), WEST), ys), tags)],
                      fords=[ta.osm_point(100 + i, x, y, ford="yes") for i, (x, y) in enumerate(fords)])
        pkg, _ = ta.build(self.cfg, ta.two_valleys(), osm)
        return pkg

    def testRiverTooWideToFordCutsTheAreaInTwo(self):
        pkg = self.river("20")
        with self.assertRaises(RouteError):
            route(pkg, (WEST - 450, ta.YC), (WEST + 450, ta.YC))
        self.assertGreater(int((pkg.edge_kind == model.BANK).sum()), 0)

    def testMarkedFordLetsYouAcross(self):
        pkg = self.river("20", fords=[(WEST, ta.YC - 600)])
        path = route(pkg, (WEST - 450, ta.YC), (WEST + 450, ta.YC))
        self.assertIn(model.FORD, kinds_of(path))

    def testSmallStreamIsWadedAnywhere(self):
        pkg = self.river(None)  # a few km2 of catchment, under max_catchment_km2
        path = route(pkg, (WEST - 450, ta.YC), (WEST + 450, ta.YC))
        self.assertNotIn(model.FORD, kinds_of(path))
        self.assertEqual(int((pkg.edge_kind == model.BANK).sum()), 0)

    def testWadingCostsExtra(self):
        pkg = self.river(None)
        waded = (pkg.weight_fwd > pkg.time_fwd * self.cfg.graph.lattice_penalty + 1) & (
            pkg.edge_kind == model.LATTICE)
        self.assertTrue(waded.any())


class TestLakeBanks(GraphCase):
    def testLakeHasWalkablePreferredBank(self):
        _, y = ta.cell_xy()
        dem = (500.0 + 0.001 * (y - SOUTH_EDGE)).astype(np.float32)
        lon, lat = ta.lonlat(
            [ta.XC - 450, ta.XC + 450],
            [ta.YC - 600, ta.YC + 600],
        )
        lake = shapely.box(float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max()))
        pkg, _ = ta.build(self.cfg, dem, terrain_areas={"water": [lake]})
        bank = (pkg.edge_kind == model.BANK) & np.isfinite(pkg.time_fwd) & (pkg.time_fwd > 0)
        self.assertTrue(bank.any())
        self.assertTrue((pkg.edge_terrain[bank] != model.TERRAIN["water"]).all())
        np.testing.assert_allclose(
            pkg.weight_fwd[bank] / pkg.time_fwd[bank],
            self.cfg.graph.bank_penalty,
            rtol=1e-4,
        )
        self.assertGreater(self.cfg.graph.bank_penalty, 1.0)
        self.assertLess(self.cfg.graph.bank_penalty, self.cfg.graph.lattice_penalty)


def crossings_of(path, x_stream: float, slack: float = 25.0) -> int:
    """How many times the route goes from one side of the north-south stream
    at *x_stream* to the other (ignoring the strip of *slack* along it)."""
    x = coords_of(path)[:, 0] - x_stream
    side = np.sign(x[np.abs(x) > slack])
    return int((side[1:] != side[:-1]).sum())


class TestStreamBanks(GraphCase):
    """The west valley's stream (a few km2: wadable, not free) runs north-south
    down its floor at x = WEST."""

    @classmethod
    def setUpClass(cls):
        cls.cfg = ta.config(Path(tempfile.mkdtemp()))
        cls.pkg, cls.graph = ta.build(cls.cfg, ta.two_valleys())

    def setUp(self):
        pass

    def testAlongTheStreamItStaysOnOneBank(self):
        path = route(self.pkg, (WEST - 120, ta.YC - 1200), (WEST - 120, ta.YC + 1200))
        self.assertEqual(crossings_of(path, WEST), 0)
        self.assertIn(model.THALWEG, kinds_of(path))

    def testAcrossItCrossesOnce(self):
        path = route(self.pkg, (WEST - 120, ta.YC - 1200), (WEST + 120, ta.YC + 1200))
        self.assertEqual(crossings_of(path, WEST), 1)

    def testBothBanksAreWalked(self):
        lines = shapely.linestrings(self.graph.geom_xy[:2])  # warm-up of the import
        del lines
        thalweg = np.flatnonzero(self.graph.edge_kind == model.THALWEG)
        mid = np.array([self.graph.geom_xy[self.graph.geom_off[i]:self.graph.geom_off[i + 1]].mean(axis=0)
                        for i in thalweg])
        near = (np.abs(mid[:, 1] - ta.YC) < 600) & (np.abs(mid[:, 0] - WEST) < 60)
        self.assertTrue(((mid[near, 0] < WEST - 2).any()) and ((mid[near, 0] > WEST + 2).any()))

    def testLeavingTheValleyFloorCosts(self):
        k = self.pkg.node_kind
        conn = self.pkg.edge_kind == model.CONNECTOR
        ends = np.stack([k[self.pkg.edge_u], k[self.pkg.edge_v]], axis=1)
        off_floor = conn & (ends == model.THALWEG).any(axis=1) & (ends == model.LATTICE).any(axis=1)
        on_floor = conn & (ends == model.THALWEG).all(axis=1)
        self.assertTrue(off_floor.any())
        self.assertTrue((self.pkg.edge_extra[off_floor] >= self.cfg.graph.thalweg_leave_penalty_s).all())
        # along it - the joins between its runs - only wading costs
        self.assertTrue((self.pkg.edge_extra[on_floor] < self.cfg.graph.thalweg_leave_penalty_s).any())

    def testCrossingWeighsTheBasePenalty(self):
        base = self.cfg.ford.crossing_base_s
        crossing = (self.pkg.edge_extra >= base) & np.isin(self.pkg.edge_kind, (model.LATTICE, model.CONNECTOR))
        self.assertTrue(crossing.any())

    def testOffsetBankDoesNotCrossWindingCenterline(self):
        center = np.array([
            [0.0, 0.0],
            [80.0, 0.0],
            [25.0, 15.0],
            [90.0, 30.0],
            [20.0, 45.0],
        ])
        source = shapely.LineString(center)
        for side in (-8.0, 8.0):
            bank = shapely.LineString(_offset(center, side))
            self.assertFalse(bank.crosses(source))


def scarp(steep_w: float = 130.0, gentle_w: float = 600.0) -> np.ndarray:
    """Two plateaus 300 m apart, the step between them at y = YC: a steep face
    west of XC (*steep_w*: 130 is ~30 degrees at its steepest, 180 ~23),
    easing to a gentle ramp (*gentle_w*, ~7 degrees) from XC + 800 m on."""
    x, y = ta.cell_xy()
    blend = np.clip((x - ta.XC) / 800.0, 0.0, 1.0)
    w = steep_w + (gentle_w - steep_w) * blend
    return (500.0 + 300.0 / (1.0 + np.exp(-(y - ta.YC) / w)) + 0.001 * (y - SOUTH_EDGE)).astype(np.float32)


class TestSteepDescent(GraphCase):
    """From the upper plateau straight down the steep face, or round by the
    ramp 1 km east: weighing steep descents makes the ramp worth it."""

    built: dict = {}  # (steep_w, descent) -> package: a scarp takes seconds to build

    def route_down(self, steep_w=130.0, pack_kg=0.0, **descent):
        key = (steep_w, tuple(sorted(descent.items())))
        if key not in self.built:
            cfg = ta.config(Path(tempfile.mkdtemp()), descent=descent) if descent else self.cfg
            self.built[key] = ta.build(cfg, scarp(steep_w))[0]
        return route(self.built[key], (ta.XC - 300, ta.YC + 1200), (ta.XC - 300, ta.YC - 1200), pack_kg=pack_kg)

    def testStraightDownWhenDescentsAreNotWeighed(self):
        path = self.route_down(penalty_per_10deg=0.0)
        self.assertLess(coords_of(path)[:, 0].max(), ta.XC + 200)

    def testOnASlantWhenTheyAre(self):
        # off the fall line towards the ramp, if not all the way to it
        path = self.route_down()
        self.assertGreater(coords_of(path)[:, 0].max(), ta.XC + 250)

    def testAHeavyPackGoesRoundWhereALightOneDoesNot(self):
        light = self.route_down(steep_w=180.0)  # ~23 degrees at its steepest
        heavy = self.route_down(steep_w=180.0, pack_kg=30.0)
        self.assertLess(coords_of(light)[:, 0].max(), ta.XC + 200)
        self.assertGreater(coords_of(heavy)[:, 0].max(), ta.XC + 250)

    def testAShortStepIsNotWeighed(self):
        # the same face but only 60 m high: under min_drop_m
        x, y = ta.cell_xy()
        dem = (500.0 + 60.0 / (1.0 + np.exp(-(y - ta.YC) / 20.0)) + 0.001 * (y - SOUTH_EDGE)).astype(np.float32)
        pkg, _ = ta.build(self.cfg, dem)
        self.assertEqual(float(np.max(pkg.steep_fwd)), 0.0)
        self.assertEqual(float(np.max(pkg.steep_bwd)), 0.0)


def fin_ridge(fin: float = 40.0) -> np.ndarray:
    """A broad ridge along x = XC, 10 degrees down either side, with a fin
    *fin* m high and 90 m wide each way along its top (40: ~32 degrees, under
    max_slope_deg); tilted to drain south."""
    x, y = ta.cell_xy()
    d = np.abs(x - ta.XC)
    z = 1200.0 - 0.18 * d + fin * np.maximum(0.0, 1.0 - d / 90.0) + 0.02 * (y - SOUTH_EDGE)
    return z.astype(np.float32)


class TestRidges(GraphCase):
    def testTheCrestIsALine(self):
        pkg, graph = ta.build(self.cfg, ta.two_valleys())
        ridge = np.flatnonzero(graph.edge_kind == model.RIDGE)
        self.assertTrue(ridge.size)
        mid = np.array([graph.geom_xy[graph.geom_off[i]:graph.geom_off[i + 1]].mean(axis=0) for i in ridge])
        on_crest = np.abs(mid[:, 0] - ta.XC) < 45
        self.assertGreater(int(on_crest.sum()), 10)

    def testASharpCrestIsLeftForTheShoulder(self):
        """A fin 40 m high (~32 degrees either side) along the top of a broad
        ridge (10 degrees): walked beside, on the shoulder, not on its crest."""
        pkg, _ = ta.build(self.cfg, fin_ridge())
        path = route(pkg, (ta.XC, ta.YC - 1200), (ta.XC, ta.YC + 1200))
        edges = edges_of(path)
        lengths = np.asarray(path.graph.edge_len[edges], np.float64)
        kinds = np.asarray(path.graph.edge_kind[edges])
        self.assertLess(lengths[kinds == model.RIDGE].sum() / lengths.sum(), 0.3)

    def testSharpCrestsAreSlowerAndDearer(self):
        from routing.build import dem as dem_mod
        from routing.build.terrain import terrain_raster
        from routing.cost import CostModel, Rasters

        cm = CostModel(self.cfg)
        g = ta.grid()

        def along_crest(dem):
            gx, gy = dem_mod.gradients(dem, g.res)
            r = Rasters(grid=g, dem=dem, gx=gx, gy=gy, terrain=terrain_raster(g, {}))
            xy = np.array([[ta.XC, ta.YC - 300], [ta.XC, ta.YC + 300]])
            c = cm.edge_costs(r, xy, np.array([0, 2]), np.array([model.RIDGE]), np.ones(1))
            return c.time_fwd[0], c.weight_fwd[0]

        broad_t, broad_w = along_crest(fin_ridge(fin=0.0))
        sharp_t, sharp_w = along_crest(fin_ridge())
        knife_t, knife_w = along_crest(fin_ridge(fin=120.0))  # ~56 degrees
        self.assertAlmostEqual(broad_w, broad_t, places=3)  # 10 degree flanks: free
        self.assertGreater(sharp_t, broad_t * 1.3)
        self.assertGreater(sharp_w / sharp_t, 1.5)
        # a knife edge: walkable, but ten times dearer again
        self.assertTrue(np.isfinite(knife_t))
        self.assertGreater(knife_w / knife_t, 10 * (sharp_w / sharp_t))

    def testAlongTheCrestTheRouteFollowsIt(self):
        pkg, _ = ta.build(self.cfg, ta.two_valleys())
        path = route(pkg, (ta.XC, ta.YC - 1200), (ta.XC, ta.YC + 1200))
        self.assertIn(model.RIDGE, kinds_of(path))
        edges = edges_of(path)
        lengths = np.asarray(path.graph.edge_len[edges], np.float64)
        kinds = np.asarray(path.graph.edge_kind[edges])
        self.assertGreater(lengths[kinds == model.RIDGE].sum() / lengths.sum(), 0.6)
        self.assertLess(np.abs(coords_of(path)[:, 0] - ta.XC).max(), 120)


def stepped(height: float) -> np.ndarray:
    """Flat ground with a *height* m wall across it at y = YC, north side up."""
    _, y = ta.cell_xy()
    return (500.0 + np.where(y > ta.YC, height, 0.0) + 0.001 * (y - SOUTH_EDGE)).astype(np.float32)


class TestCliffsAndCanyons(GraphCase):
    def testCliffCannotBeWalked(self):
        pkg, graph = ta.build(self.cfg, stepped(60))
        labels = {label for _, label in graph.debug["barriers"]}
        self.assertIn("steep", labels)
        with self.assertRaises(RouteError):
            route(pkg, (ta.XC, ta.YC - 400), (ta.XC, ta.YC + 400))

    def testTrailUpTheCliffIsTaken(self):
        osm = OsmData(routable=[ta.osm_way(1, 1, [ta.XC, ta.XC + 100, ta.XC + 200],
                                           [ta.YC - 300, ta.YC, ta.YC + 300], highway="path")])
        pkg, _ = ta.build(self.cfg, stepped(60), osm)
        path = route(pkg, (ta.XC, ta.YC - 400), (ta.XC, ta.YC + 400))
        self.assertIn(model.OSM, kinds_of(path))

    def testCanyonIsNeitherCrossedNorWalked(self):
        x, y = ta.cell_xy()
        dem = (500 + np.minimum(np.abs(x - ta.XC) * 1.5, 150) + 0.02 * (y - SOUTH_EDGE)).astype(np.float32)
        pkg, graph = ta.build(self.cfg, dem)
        self.assertTrue(graph.debug["canyons"])
        with self.assertRaises(RouteError):
            route(pkg, (ta.XC - 600, ta.YC), (ta.XC + 600, ta.YC))


class TestTrailsOnly(GraphCase):
    def testNoOffTrailWhenItIsOff(self):
        xs = np.linspace(WEST - 300, WEST + 300, 7)
        osm = OsmData(routable=[ta.osm_way(1, 1, xs, np.full(7, ta.YC), highway="track")])
        pkg, _ = ta.build(self.cfg, ta.two_valleys(), osm)
        path = route(pkg, (xs[0], ta.YC), (xs[-1], ta.YC), allow_offroad=False)
        self.assertEqual(kinds_of(path), {model.OSM})
        with self.assertRaises(RouteError):  # nowhere near a trail
            route(pkg, (xs[0], ta.YC), (EAST, ta.YC - 900), allow_offroad=False)


if __name__ == "__main__":
    unittest.main()


def two_saddles() -> np.ndarray:
    """The steep ridge with two saddles: at YC and 1200 m north of it."""
    x, y = ta.cell_xy()
    u = (x - ta.XC) / ta.VALLEY
    shape = (np.cos(np.pi * u) + 1) / 2
    notch = 1 - 0.5 * (np.exp(-(((y - ta.YC) / 400.0) ** 2)) + np.exp(-(((y - ta.YC - 1200) / 400.0) ** 2)))
    tilt = 0.05 * (y - (ta.Y0 - ta.SIZE * ta.RES))
    return (400.0 + 2400.0 * shape * notch + tilt).astype(np.float32)


class TestPassLimits(GraphCase):
    """A 2Б pass right between the two points and a 1А one 1.2 km off."""

    @classmethod
    def setUpClass(cls):
        cls.cfg = ta.config(Path(tempfile.mkdtemp()))
        osm = OsmData(passes=[ta.osm_point(1, ta.XC, ta.YC, name="Hard", rtsa_scale="2Б"),
                              ta.osm_point(2, ta.XC, ta.YC + 1200, name="Easy", rtsa_scale="1А")])
        cls.pkg, cls.raw = ta.build(cls.cfg, two_saddles(), osm)
        cls.engine = ta.engine(cls.pkg)
        lon, lat = ta.lonlat([WEST, EAST], [ta.YC, ta.YC])
        cls.points = list(zip(lon.tolist(), lat.tolist()))

    def setUp(self):
        pass

    def crossed(self, **limits):
        path = self.engine.route(self.points, Options(**limits))
        return [self.engine.graph.pass_osm[i] for i in path.passes]

    def testHardPassByDefault(self):
        self.assertEqual(self.crossed(), [1])

    def testNoHarderThan(self):
        self.assertEqual(self.crossed(max_pass_grade=model.pass_grade("1Б")), [2])

    def testHigherSummitBypassRequiresTheNearbyPass(self):
        graph = self.raw
        hard = next(i for i, p in enumerate(graph.passes) if p["osm"] == 1)
        # The ridge is higher 400 m north of the hard saddle, well outside
        # the small apex which marks the pass edge itself. It must still carry
        # that pass requirement, or a category limit can evade the saddle by
        # walking over the neighbouring summit.
        mid = np.array([
            graph.geom_xy[graph.geom_off[:-1]],
            graph.geom_xy[graph.geom_off[1:] - 1],
        ]).mean(axis=0)
        near = (
            (np.abs(mid[:, 0] - ta.XC) < 200)
            & (np.abs(mid[:, 1] - (ta.YC + 400)) < 150)
        )
        self.assertTrue(near.any())
        self.assertTrue((graph.cross_pass[near] == hard).any())

    def testAtMostNOfTheTopGrade(self):
        two_b = model.pass_grade("2Б")
        self.assertEqual(self.crossed(max_pass_grade=two_b, max_top_passes=1), [1])
        self.assertEqual(self.crossed(max_pass_grade=two_b, max_top_passes=0), [2])

    def testNothingEasyEnough(self):
        with self.assertRaises(RouteError):
            self.crossed(max_pass_grade=model.pass_grade("н/к"))

    def testPassNamesSurviveCompiling(self):
        graph = self.engine.graph
        self.assertEqual(graph.meta["pass_names"][int(np.flatnonzero(graph.pass_osm == 2)[0])], "Easy")
