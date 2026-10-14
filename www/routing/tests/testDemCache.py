"""The DEM cache: warped relief, watershed and the lines taken from them."""

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pytest
import shapely

pytest.importorskip("numba")

from routing import model
from routing.build import demcache, hydrology
from routing.build.demcache import DemLines, Relief, Surface
from routing.build.osmdata import OsmData
from routing.tests import terrainaide as ta


def surface_of(dem: np.ndarray | None = None) -> Surface:
    dem = ta.two_valleys() if dem is None else dem
    grid = ta.grid()
    dem = dem.copy()
    dem[0, 0] = np.nan
    return Surface(grid, dem, hydrology.with_numpy(dem, grid))


class TestDemCache(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = ta.config(self.tmp)

    def testSurfaceRoundTripKeepsTheWatershed(self):
        path = self.tmp / "surface.npz"
        original = surface_of()
        demcache.save_surface(path, original)
        loaded = demcache.load_surface(path)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.grid, original.grid)
        np.testing.assert_array_equal(loaded.dem, original.dem)
        np.testing.assert_array_equal(loaded.hyd.drain, original.hyd.drain)
        np.testing.assert_array_equal(loaded.hyd.accum_km2, original.hyd.accum_km2)
        self.assertTrue(np.isnan(loaded.dem[0, 0]))

    def testSecondReadSkipsTheBuild(self):
        path = self.tmp / "surface.npz"
        calls = {"n": 0}

        def build() -> Surface:
            calls["n"] += 1
            return surface_of()

        first = demcache.load_or_build_surface(path, build)
        second = demcache.load_or_build_surface(path, build)
        self.assertEqual(calls["n"], 1)
        np.testing.assert_array_equal(first.hyd.accum_km2, second.hyd.accum_km2)

    def testUnreadableCacheIsRebuilt(self):
        path = self.tmp / "surface.npz"
        path.write_bytes(b"not a zip")
        built = demcache.load_or_build_surface(path, surface_of)
        self.assertEqual(built.dem.shape, ta.grid().shape)
        self.assertIsNotNone(demcache.load_surface(path))

    def testAnotherDemFileIsAnotherKey(self):
        bbox = (0.0, 0.0, 1.0, 1.0)
        same = dict(bbox=bbox, epsg=32633, res=30.0, method="numpy", kind="fine")
        first = demcache.raster_key(sources=[("n55e102.hgt", 10, 1)], **same)
        touched = demcache.raster_key(sources=[("n55e102.hgt", 10, 2)], **same)
        other = demcache.raster_key(sources=[("n55e103.hgt", 10, 1)], **same)
        self.assertNotEqual(first, touched)
        self.assertNotEqual(first, other)
        self.assertEqual(first, demcache.raster_key(sources=[("n55e102.hgt", 10, 1)], **same))

    def testThresholdChangeIsAnotherEdgeKey(self):
        base = demcache.edge_key(0.2, 75.0, 1.0)
        self.assertNotEqual(base, demcache.edge_key(0.05, 75.0, 1.0))
        self.assertEqual(base, demcache.edge_key(0.2, 75.0, 1.0))

    def testEdgesRoundTrip(self):
        original = surface_of()
        lines = demcache.extract_lines(self.cfg, original.grid, original.hyd)
        self.assertTrue(lines.chains)
        self.assertTrue(lines.ridges)
        path = self.tmp / "edges.npz"
        demcache.save_lines(path, lines)
        loaded = demcache.load_lines(path)
        self.assertEqual(len(loaded.chains), len(lines.chains))
        self.assertEqual(len(loaded.ridges), len(lines.ridges))
        for a, b in zip(loaded.chains, lines.chains):
            np.testing.assert_array_equal(a, b)
        for a, b in zip(loaded.ridges, lines.ridges):
            np.testing.assert_array_equal(a, b)

    def testSecondEdgeReadDoesNotRewrite(self):
        original = surface_of()
        relief = Relief(original.grid, original.dem, original.hyd, key="abc")
        first = demcache.lines(self.cfg, relief)
        stamp = demcache.edge_key(
            self.cfg.graph.valley_catchment_km2,
            self.cfg.graph.thalweg_node_spacing_m,
            self.cfg.graph.ridge_catchment_km2,
        )
        path = self.cfg.routing_dir / ".cache" / "dem" / f"abc-{stamp}.npz"
        mtime = path.stat().st_mtime_ns
        second = demcache.lines(self.cfg, relief)
        self.assertEqual(path.stat().st_mtime_ns, mtime)
        self.assertEqual(len(first.chains), len(second.chains))
        np.testing.assert_array_equal(first.chains[0], second.chains[0])

    def testCachedLinesMatchAFreshGraph(self):
        dem = ta.two_valleys()
        hyd = hydrology.with_numpy(dem, ta.grid())
        cached = demcache.extract_lines(self.cfg, ta.grid(), hyd)
        _, fresh = ta.build(self.cfg, dem)
        _, again = ta.build(self.cfg, dem, dem_lines=cached)
        np.testing.assert_array_equal(fresh.edge_kind, again.edge_kind)
        np.testing.assert_array_equal(fresh.edge_u, again.edge_u)
        np.testing.assert_array_equal(fresh.edge_v, again.edge_v)

    def testPassesResplitCachedThalwegs(self):
        dem = ta.two_valleys()
        hyd = hydrology.with_numpy(dem, ta.grid())
        cached = demcache.extract_lines(self.cfg, ta.grid(), hyd)
        # Empty runs would drop the valley if the cache were trusted blindly.
        wrong = DemLines(chains=[], ridges=cached.ridges)
        osm = OsmData(passes=[ta.osm_point(1, ta.XC, ta.YC, name="Saddle")])
        _, fresh = ta.build(self.cfg, dem, osm)
        _, again = ta.build(self.cfg, dem, osm, dem_lines=wrong)
        self.assertTrue((fresh.edge_kind == model.THALWEG).any())
        np.testing.assert_array_equal(fresh.edge_kind, again.edge_kind)
        np.testing.assert_array_equal(fresh.edge_u, again.edge_u)

    def testALakeRebuildsCachedCrests(self):
        dem = ta.two_valleys()
        hyd = hydrology.with_numpy(dem, ta.grid())
        cached = demcache.extract_lines(self.cfg, ta.grid(), hyd)
        # Empty divides would drop the crests if a lake did not force a retrace.
        wrong = DemLines(chains=cached.chains, ridges=[])
        lon, lat = ta.lonlat([ta.XC - 450, ta.XC + 450], [ta.YC - 600, ta.YC + 600])
        lake = shapely.box(float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max()))
        kwargs = {"terrain_areas": {"water": [lake]}}
        _, fresh = ta.build(self.cfg, dem, **kwargs)
        _, again = ta.build(self.cfg, dem, dem_lines=wrong, **kwargs)
        self.assertTrue((fresh.edge_kind == model.RIDGE).any())
        np.testing.assert_array_equal(fresh.edge_kind, again.edge_kind)
        np.testing.assert_array_equal(fresh.edge_u, again.edge_u)
