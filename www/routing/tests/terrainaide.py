"""Synthetic terrain for the routing tests: no PBF, no GDAL warp, no GRASS.

    west valley ── ridge with a saddle (the pass) ── east valley

on a 30 m grid in UTM 33N, about 67.6 N. Both valleys run north-south and
drain south; the saddle is the low point of the ridge half way up. OSM data is
built by hand in lon/lat, like osmdata.read would give it.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import numpy as np
from shapely.geometry import LineString

from routing import config as routing_config
from routing import model, package
from routing.build import dem as dem_mod
from routing.build import hydrology
from routing.build.graph import GraphBuilder
from routing.build.osmdata import OsmData, Point, Way
from routing.build.pipeline import package_arrays
from routing.build.terrain import project, terrain_raster
from routing.cost import Rasters, long_steep
from routing.grid import Grid, Projector

EPSG = 32633
RES = 30.0
SIZE = 120  # cells a side: 3.6 km
X0 = 500_000.0
Y0 = 7_500_000.0  # north edge
XC = X0 + SIZE * RES / 2
YC = Y0 - SIZE * RES / 2
VALLEY = 1200.0  # ridge to valley floor


def grid() -> Grid:
    return Grid(X0, Y0, RES, SIZE, SIZE, EPSG)


def cell_xy() -> tuple[np.ndarray, np.ndarray]:
    g = grid()
    rows, cols = np.mgrid[0:SIZE, 0:SIZE]
    return g.cell_xy(rows, cols)


def two_valleys(saddle: float = 0.6, height: float = 150.0) -> np.ndarray:
    """Ridge along x = XC, valleys VALLEY m either side, a saddle at y = YC
    *saddle* of the way down, everything tilted to drain south."""
    x, y = cell_xy()
    u = (x - XC) / VALLEY
    shape = (np.cos(np.pi * u) + 1) / 2
    notch = 1 - saddle * np.exp(-(((y - YC) / 500.0) ** 2))
    tilt = 0.05 * (y - (Y0 - SIZE * RES))
    return (400.0 + height * shape * notch + tilt).astype(np.float32)


def lonlat(x, y) -> tuple[np.ndarray, np.ndarray]:
    return Projector(EPSG).to_lonlat(np.atleast_1d(x), np.atleast_1d(y))


def bbox() -> tuple[float, float, float, float]:
    lon, lat = lonlat([X0 + 300, X0 + SIZE * RES - 300], [Y0 - SIZE * RES + 300, Y0 - 300])
    return float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max())


def osm_point(osm_id: int, x: float, y: float, **tags) -> Point:
    lon, lat = lonlat(x, y)
    return Point(osm_id, float(lon[0]), float(lat[0]), dict(tags))


def osm_line(xs, ys) -> LineString:
    lon, lat = lonlat(xs, ys)
    return LineString(np.column_stack([lon, lat]))


def osm_way(way_id: int, first_node: int, xs, ys, **tags) -> Way:
    lon, lat = lonlat(xs, ys)
    return Way(way_id, np.arange(first_node, first_node + len(lon), dtype=np.int64), lon, lat, dict(tags))


def config(tmp: Path, **overrides) -> routing_config.Config:
    """Test config: thresholds sized for the 3.6 km grid."""
    raw = {
        "areas": [{"name": "test", "bbox": list(bbox())}],
        "graph": {
            "valley_catchment_km2": 0.05,
            "lattice_spacing_m": 150,
            "hydrology": "numpy",
        },
        "ford": {"max_catchment_km2": 50, "free_below_km2": 0.5},
    }
    for section, values in overrides.items():
        raw.setdefault(section, {}).update(values)
    return routing_config.from_dict(raw, data_dir=tmp)


def build(cfg: routing_config.Config, dem: np.ndarray, osm: OsmData | None = None,
          terrain_areas: dict | None = None, box: tuple | None = None,
          name: str = "test", crevasse: np.ndarray | None = None,
          dem_lines=None) -> tuple[package.Package, object]:
    """The whole build minus I/O: hydrology, graph, costs -> an in-memory package."""
    g = grid()
    osm = osm or OsmData()
    projector = Projector(EPSG)
    gx, gy = dem_mod.gradients(dem, g.res)
    slope = dem_mod.slope_deg(gx, gy)
    hyd = hydrology.with_numpy(dem, g)
    areas_xy = {k: list(project(v, projector)) for k, v in (terrain_areas or {}).items()}
    osm = dataclasses.replace(osm, terrain_areas={**osm.terrain_areas, **(terrain_areas or {})})
    rasters = Rasters(grid=g, dem=dem, gx=gx, gy=gy, terrain=terrain_raster(g, areas_xy), crevasse=crevasse,
                      long_steep=long_steep(dem, slope, cfg.descent.from_deg, cfg.descent.min_drop_m))
    margin = 3 * RES
    box = box or (X0 + margin, Y0 - SIZE * RES + margin, X0 + SIZE * RES - margin, Y0 - margin)
    graph = GraphBuilder(cfg, rasters, slope, hyd, osm, projector, box, dem_lines=dem_lines).build()
    arrays = package_arrays(cfg, rasters, graph, projector)
    meta = {"area": name, "bbox": list(bbox()), "epsg": EPSG, "schema": model.SCHEMA,
            "pass_names": [p["name"] for p in graph.passes]}
    return package.Package(meta=meta, **arrays), graph


def engine(pkg: package.Package):
    """*pkg* compiled in a data dir of its own, and the disk engine on it."""
    import tempfile

    from routing import compile as routing_compile
    from routing.engine.disk import DiskEngine, DiskGraph

    cfg = config(Path(tempfile.mkdtemp()))
    write(cfg, pkg)
    return DiskEngine(DiskGraph(routing_compile.graphs_dir(cfg) / "test"))


def write(cfg: routing_config.Config, pkg: package.Package, name: str = "test",
          compile_it: bool = True) -> Path:
    """The package where the build leaves it, and compiled for the disk engine."""
    from routing import compile as routing_compile

    arrays = {key: getattr(pkg, key) for key in package.ARRAYS}
    out = package.write(cfg.area_dir(name), arrays, {**pkg.meta, "built_at": "test"})
    if compile_it:
        routing_compile.compile_graph(cfg, name)
    return out
