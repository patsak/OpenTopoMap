"""The compiled graph and the disk engine: optimal routes, tiles merged."""

import dataclasses
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("numba")

from routing import compile as routing_compile  # noqa: E402
from routing import model, pack  # noqa: E402
from routing.build.osmdata import OsmData  # noqa: E402
from routing.config import Area, PackParams  # noqa: E402
from routing.engine.base import Options  # noqa: E402
from routing.engine.disk import DiskEngine, DiskGraph  # noqa: E402
from routing.tests import terrainaide as ta  # noqa: E402

WEST = ta.XC - ta.VALLEY
EAST = ta.XC + ta.VALLEY


def points(*xy):
    lon, lat = ta.lonlat([p[0] for p in xy], [p[1] for p in xy])
    return list(zip(lon.tolist(), lat.tolist()))


def best_weight(pkg, src, dst, options):
    """The cheapest way from package node *src* to *dst*, by Dijkstra over the
    package's own weights (routing.pack's for the load): what A* must match."""
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import dijkstra

    wf, wb = pack.weights(pkg, options.pack, options.pack_kg)
    fwd, bwd = np.isfinite(pkg.time_fwd), np.isfinite(pkg.time_bwd)
    tail = np.concatenate([pkg.edge_u[fwd], pkg.edge_v[bwd]])
    head = np.concatenate([pkg.edge_v[fwd], pkg.edge_u[bwd]])
    w = np.concatenate([wf[fwd], wb[bwd]]).astype(np.float64)
    # the cheapest of parallel edges (csr_matrix would add them up)
    order = np.lexsort((w, head, tail))
    tail, head, w = tail[order], head[order], w[order]
    first = np.r_[True, (tail[1:] != tail[:-1]) | (head[1:] != head[:-1])]
    n = pkg.node_count
    graph = csr_matrix((w[first], (tail[first], head[first])), shape=(n, n))
    return float(dijkstra(graph, indices=src)[dst])


class TestSameRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = ta.config(Path(tempfile.mkdtemp()))
        xs = np.linspace(WEST - 300, WEST + 300, 7)
        osm = OsmData(routable=[ta.osm_way(1, 1, xs, np.full(7, ta.YC), highway="path")],
                      passes=[ta.osm_point(9, ta.XC, ta.YC)])
        cls.pkg, _ = ta.build(cls.cfg, ta.two_valleys(), osm)
        ta.write(cls.cfg, cls.pkg)
        cls.graph = DiskGraph(routing_compile.graphs_dir(cls.cfg) / "test")
        cls.disk = DiskEngine(cls.graph)

    def on_nodes(self, pts):
        """Each point's nearest package node, and its position: a point right
        on a node snaps to it in the compiled graph too."""
        out = []
        for lon, lat in pts:
            i = int(np.argmin(np.hypot(self.pkg.node_lon - lon, self.pkg.node_lat - lat)))
            out.append((i, (float(self.pkg.node_lon[i]), float(self.pkg.node_lat[i]))))
        return out

    def compare(self, pts, options):
        (src, a), (dst, b_) = self.on_nodes(pts)
        b = self.disk.route([a, b_], options)
        # disk paths are in compiled edge ids: cost them off the compiled arcs,
        # whose backpack routing.compile folded in - against the package's
        # weights for the load, unfolded (routing.pack)
        arcs = np.concatenate([leg.arcs for leg in b.legs])
        k = options.pack_kg / 10
        w = np.asarray(self.graph.arc_weight[arcs], np.float64) + k * np.asarray(self.graph.arc_pack_w[arcs], np.float64)
        best = best_weight(self.pkg, src, dst, options)
        self.assertAlmostEqual(w.sum() / best, 1.0, places=3)
        self.assertAlmostEqual(self.disk.weight(b, options) / best, 1.0, places=3)

    def testOverTheRidge(self):
        self.compare(points((WEST, ta.YC + 300), (EAST, ta.YC + 300)), Options())

    def testWithAPack(self):
        self.compare(points((WEST, ta.YC + 300), (EAST, ta.YC - 600)), Options(pack_kg=30, pack=PackParams()))

    def testTrailsOnly(self):
        pts = points((WEST - 300, ta.YC), (WEST + 300, ta.YC))
        b = self.disk.route(pts, Options(allow_offroad=False))
        kinds = {int(self.graph.edge_kind[i]) for leg in b.legs for i, _ in leg.edges}
        self.assertEqual(kinds, {model.OSM})

    def testPolylinesKeepTheirDirection(self):
        b = self.disk.route(points((WEST, ta.YC + 300), (EAST, ta.YC + 300)), Options())
        coords = [self.graph.edge_coords(i, f) for leg in b.legs for i, f in leg.edges]
        for prev, nxt in zip(coords[:-1], coords[1:]):
            np.testing.assert_allclose(prev[-1, :2], nxt[0, :2], atol=1e-6)


class TestRecompile(unittest.TestCase):
    def testACurrentGraphIsLeftAlone(self):
        import dataclasses

        cfg = ta.config(Path(tempfile.mkdtemp()))
        pkg, _ = ta.build(cfg, ta.two_valleys())
        ta.write(cfg, pkg)
        self.assertTrue(routing_compile.is_current(cfg, "test"))
        # another pack config: the folded backpack is stale
        other = dataclasses.replace(cfg, pack=dataclasses.replace(cfg.pack, climb_per_10kg=0.1))
        self.assertFalse(routing_compile.is_current(other, "test"))
        # a rebuilt package: stale too
        ta.write(cfg, pkg, compile_it=False)
        meta = cfg.area_dir("test") / "meta.json"
        meta.write_text(meta.read_text().replace('"built_at": "test"', '"built_at": "later"'))
        self.assertFalse(routing_compile.is_current(cfg, "test"))


class TestTilesMerge(unittest.TestCase):
    def testSeamIsCrossed(self):
        tmp = Path(tempfile.mkdtemp())
        base = ta.config(tmp)
        m = 3 * ta.RES
        south, north = ta.Y0 - ta.SIZE * ta.RES + m, ta.Y0 - m
        # two tiles meeting on the ridge, each reaching 500 m past it
        boxes = {"r-00-00": (ta.X0 + m, south, ta.XC + 500, north),
                 "r-00-01": (ta.XC - 500, south, ta.X0 + ta.SIZE * ta.RES - m, north)}
        tiles = tuple(Area(name, ta.bbox(), region="r", debug=False) for name in boxes)
        cfg = dataclasses.replace(base, areas=tiles, regions=(Area("r", ta.bbox(), tile_km=10),))
        sizes = 0
        for name, box in boxes.items():
            pkg, _ = ta.build(cfg, ta.two_valleys(), box=box, name=name)
            ta.write(cfg, pkg, name, compile_it=False)
            sizes += pkg.node_count
        routing_compile.compile_graph(cfg, "r")
        graph = DiskGraph(routing_compile.graphs_dir(cfg) / "r")
        self.assertLess(graph.node_count, sizes)  # the strip's lattice merged
        path = DiskEngine(graph).route(points((WEST, ta.YC + 300), (EAST, ta.YC + 300)), Options())
        self.assertTrue(path.legs[0].edges)

    def testLastTileWithoutPasses(self):
        tmp = Path(tempfile.mkdtemp())
        base = ta.config(tmp)
        m = 3 * ta.RES
        south, north = ta.Y0 - ta.SIZE * ta.RES + m, ta.Y0 - m
        # the pass is in the first tile only
        boxes = {"r-00-00": (ta.X0 + m, south, ta.XC + 500, north),
                 "r-00-01": (ta.XC + 300, south, ta.X0 + ta.SIZE * ta.RES - m, north)}
        tiles = tuple(Area(name, ta.bbox(), region="r", debug=False) for name in boxes)
        cfg = dataclasses.replace(base, areas=tiles, regions=(Area("r", ta.bbox(), tile_km=10),))
        osm = OsmData(passes=[ta.osm_point(9, ta.XC, ta.YC)])
        for name, box in boxes.items():
            pkg, _ = ta.build(cfg, ta.two_valleys(), osm if name == "r-00-00" else None, box=box, name=name)
            ta.write(cfg, pkg, name, compile_it=False)
        self.assertEqual(len(pkg.pass_osm), 0)
        routing_compile.compile_graph(cfg, "r")
        graph = DiskGraph(routing_compile.graphs_dir(cfg) / "r")
        self.assertEqual(len(graph.pass_osm), 1)
        self.assertTrue((np.asarray(graph.pass_need) == 0).any())


if __name__ == "__main__":
    unittest.main()
