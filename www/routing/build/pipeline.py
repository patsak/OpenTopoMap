"""One area from sources to a published graph package.

    OSM extract ─┐
                 ├─► terrain raster, barriers ─┐
    GEDTM DEM ───┼─► slope, r.watershed ───────┼─► GraphBuilder ─► costs ─► package
                 └─► thalwegs, pass descents ──┘

The warp, both watersheds and the DEM lines are read back from
``routing/.cache/dem`` when the HGT tiles and the grid have not changed
(routing.build.demcache).
"""

from __future__ import annotations

import json
import logging
import math
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from otmlib import grass as otm_grass
from otmlib import osm_areas
from routing import model, package
from routing.build import debug as debug_gpkg
from routing.build import dem as dem_mod
from routing.build import demcache, hydrology, osmdata
from routing.build.graph import GraphBuilder
from routing.build.rivers import RiverSize, effective_catchment
from routing.build.terrain import project, rasterize, terrain_raster
from routing.config import Area, Config
from routing.cost import CostModel, Rasters, long_steep
from routing.grid import Projector

log = logging.getLogger(__name__)

LATTICE_MARGIN_M = 500.0


def grow_bbox(bbox: tuple[float, float, float, float], margin_m: float) -> tuple[float, float, float, float]:
    west, south, east, north = bbox
    dlat = margin_m / 111_320.0
    dlon = margin_m / (111_320.0 * max(0.05, math.cos(math.radians((south + north) / 2))))
    return west - dlon, south - dlat, east + dlon, north + dlat


def source_pbfs(cfg: Config, area: Area) -> list[Path]:
    """The extracts to cut the area from: the config's own, else the regions
    datasvc keeps current that the area touches (otm.regions)."""
    if area.pbf:
        missing = [p for p in area.pbf if not p.is_file()]
        if missing:
            raise FileNotFoundError(f"area {area.name}: no such PBF {missing[0]}")
        return list(area.pbf)
    from shapely.geometry import box

    from otmlib import geofabrik, paths, regionsync

    cache = paths.geofabrik_cache(cfg.data_dir)
    # with the margin: a tile on a district border needs both districts
    shape = box(*grow_bbox(area.bbox, cfg.for_area(area).graph.margin_m))
    if area.within:
        # the regions the config names, from the cached Geofabrik index: no
        # Postgres needed, so a build machine without the stack can run it
        candidates = [geofabrik.region_by_id(rid, cache_dir=cache) for rid in area.within]
    else:
        candidates = regionsync.configured_regions(cache)
    regions = [r for r in candidates if r.geometry.intersects(shape)]
    if not regions:
        raise RuntimeError(f"area {area.name} is outside the regions datasvc covers")
    pbfs = [cache / Path(r.pbf_url).name for r in regions]
    missing = [p for p in pbfs if not p.is_file()]
    if missing:
        raise FileNotFoundError(f"area {area.name}: {missing[0]} not synced yet - run datasvc-job")
    return pbfs


def region_extract(cfg: Config, region: Area, pbfs: list[Path]) -> Path:
    """*region* (or a plain area) cut once out of its districts' extracts,
    wide enough for every tile's margin and catchment grid, its glaciers
    alongside (glaciers_of); cut again only when the extracts change."""
    from otmlib.geofabrik import extract_bbox

    g = cfg.for_area(region).graph
    out = cfg.routing_dir / ".cache" / f"{region.name}.osm.pbf"
    stamp = out.with_name(out.name + ".sources.json")
    state = json.dumps({"bbox": list(region.bbox), "margin": g.catchment_margin_m, "sources": source_state(pbfs)})
    if out.is_file() and glaciers_of(out).is_file() and stamp.is_file() \
            and stamp.read_text(encoding="utf-8") == state:
        return out
    log.info("%s: cutting the region out of %s", region.name, ", ".join(p.name for p in pbfs))
    out.parent.mkdir(parents=True, exist_ok=True)
    extract_bbox(pbfs, *grow_bbox(region.bbox, g.catchment_margin_m + g.margin_m), out)
    # its glaciers apart, for each tile's catchment grid: a few MB to cut a
    # tile's 25 km surround out of, not the whole region
    subset = osm_areas.tagged_subset(out, "natural", "glacier")
    shutil.move(str(subset), glaciers_of(out))
    stamp.write_text(state, encoding="utf-8")
    return out


def glaciers_of(cut: Path) -> Path:
    """The natural=glacier subset region_extract keeps next to its cut."""
    return cut.with_name(cut.name.removesuffix(".osm.pbf") + ".glaciers.osm.pbf")


def osm_crc(pbf: Path) -> str:
    """A checksum of a PBF's objects, not of the file: osmium's, which leaves
    out the header (the replication timestamp moves every night)."""
    from otmlib.geofabrik import _require_osmium

    out = subprocess.run([_require_osmium(), "fileinfo", "-e", "-g", "data.crc32", str(pbf)],
                         check=True, capture_output=True, text=True)
    return out.stdout.strip()


def cut_osm(cfg: Config, area: Area, pbfs: list[Path], work: Path) -> tuple[Path, Path | None]:
    """What *area* is built from: its OSM extract (bbox + margin_m), and the
    glaciers of the catchment grid around it (None when that grid is off)."""
    from otmlib.geofabrik import extract_bbox

    g = cfg.graph
    area_pbf = extract_bbox(pbfs, *grow_bbox(area.bbox, g.margin_m), work / "area.osm.pbf")
    if g.catchment_margin_m <= g.margin_m or cfg.ford.glacier_factor <= 0:
        return area_pbf, None
    if len(pbfs) == 1 and glaciers_of(pbfs[0]).is_file():
        source = glaciers_of(pbfs[0])
    else:  # an area cut straight from the extracts (--pbf, a test)
        source = osm_areas.tagged_subset(extract_bbox(pbfs, *grow_bbox(area.bbox, g.catchment_margin_m),
                                                      work / "wide.osm.pbf"), "natural", "glacier")
    glaciers = extract_bbox([source], *grow_bbox(area.bbox, g.catchment_margin_m), work / "glaciers.osm.pbf")
    if source.parent != pbfs[0].parent:
        source.unlink(missing_ok=True)
    return area_pbf, glaciers


def source_state(pbfs: list[Path]) -> list[dict]:
    """What the graph was built from: each PBF's size and mtime.

    The ``.state.txt`` beside a Geofabrik extract is written once, when the
    file is first downloaded, and later diffs do not move it. The rewritten
    PBF itself does change, so that is what a rebuild follows."""
    out = []
    for pbf in pbfs:
        st = pbf.stat()
        out.append({"name": pbf.name, "state": f"{st.st_size}:{int(st.st_mtime)}"})
    return out


CACHED_DEM = "GEDTM, SRTM1 tiles of the HGT cache"


def dem_label(area: Area) -> str:
    """Where *area*'s relief comes from, as its package records it."""
    return str(area.dem) if area.dem else CACHED_DEM


def params_current(cfg: Config, area: Area) -> bool:
    """Whether *area*'s package was built by this schema with these parameters
    from the same DEM."""
    meta = package.read_meta(cfg.area_dir(area.name))
    return (
        meta is not None
        and meta.get("schema") == model.SCHEMA
        and meta.get("fingerprint") == cfg.fingerprint(area)
        and meta.get("dem") == dem_label(area)
    )


def build_area(cfg: Config, area: Area, *, pbfs: list[Path] | None = None,
               work_dir: Path | None = None, keep_work: bool = False,
               debug: bool = True, force: bool = False,
               state_pbfs: list[Path] | None = None) -> bool:
    """Build *area*'s package unless it is current. Returns whether it built.

    Current: built by this schema and these parameters out of the same OSM
    data - the objects of the area's own cut and of the glaciers around it,
    by checksum (osm_crc). Not the extracts': Geofabrik's districts change
    every night, the few square kilometres of a mountain tile seldom. A
    check costs the cut, seconds.

    *state_pbfs*: the extracts *pbfs* was cut from (a region's), recorded.
    """
    cfg = cfg.for_area(area)
    debug = debug and area.debug
    pbfs = pbfs or source_pbfs(cfg, area)
    state_pbfs = state_pbfs or pbfs
    started = time.monotonic()
    out_dir = cfg.area_dir(area.name)
    work = work_dir or out_dir.with_name(f".{area.name}.work")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        area_pbf, glaciers = cut_osm(cfg, area, pbfs, work)
        osm = {"area": osm_crc(area_pbf), "glaciers": osm_crc(glaciers) if glaciers else None}
        if not force and params_current(cfg, area) and (package.read_meta(out_dir) or {}).get("osm") == osm:
            log.info("%s: graph is current", area.name)
            return False
        arrays, meta, writer = _build(cfg, area, area_pbf, glaciers, work, debug)
        meta["osm"] = osm
        meta["sources"] = source_state(state_pbfs)
        meta["build_seconds"] = round(time.monotonic() - started)
        package.write(out_dir, arrays, meta, writer)
    finally:
        if not keep_work:
            shutil.rmtree(work, ignore_errors=True)
    log.info("%s: built in %.0fs -> %s", area.name, time.monotonic() - started, out_dir)
    return True


def _build(cfg: Config, area: Area, area_pbf: Path, glaciers: Path | None, work: Path, debug: bool):
    g = cfg.graph
    outer = grow_bbox(area.bbox, g.margin_m)
    lon_c = (area.bbox[0] + area.bbox[2]) / 2
    lat_c = (area.bbox[1] + area.bbox[3]) / 2
    epsg = area.epsg or otm_grass.utm_epsg(lon_c, lat_c)
    projector = Projector(epsg)

    log.info("%s: DEM and watershed", area.name)
    fine = demcache.relief(cfg, area, outer, epsg, g.dem_res_m, work, "fine")
    grid, dem, hyd = fine.grid, fine.dem, fine.hyd
    dem_lines = demcache.lines(cfg, fine)
    gx, gy = dem_mod.gradients(dem, grid.res)
    slope = dem_mod.slope_deg(gx, gy)

    osm = osmdata.read(area_pbf, cfg.obstacles.barrier_pairs())
    areas_xy = {name: list(project(geoms, projector)) for name, geoms in osm.terrain_areas.items() if geoms}
    terrain = terrain_raster(grid, areas_xy)
    crevasse, crevasse_shapes = crevasse_raster(cfg, grid, osm, projector)
    rasters = Rasters(grid=grid, dem=dem, gx=gx, gy=gy, terrain=terrain, crevasse=crevasse,
                      long_steep=long_steep(dem, slope, cfg.descent.from_deg, cfg.descent.min_drop_m))

    rivers = river_size(cfg, area, glaciers, projector, work, grid, hyd, terrain)

    inner = dem_mod.utm_bounds(grow_bbox(area.bbox, LATTICE_MARGIN_M), epsg, 1.0)
    gb = grid.bounds
    box = (max(inner[0], gb[0] + grid.res), max(inner[1], gb[1] + grid.res),
           min(inner[2], gb[2] - grid.res), min(inner[3], gb[3] - grid.res))
    graph = GraphBuilder(cfg, rasters, slope, hyd, osm, projector, box, rivers, dem_lines).build()
    graph.debug["crevasses"] = crevasse_shapes

    log.info("%s: edge costs", area.name)
    arrays = package_arrays(cfg, rasters, graph, projector)
    counts = np.bincount(graph.edge_kind, minlength=len(model.KINDS))
    closed = ~np.isfinite(arrays["time_fwd"]) & ~np.isfinite(arrays["time_bwd"])
    meta = {
        "area": area.name,
        "bbox": list(area.bbox),
        "epsg": epsg,
        "fingerprint": cfg.fingerprint(area),
        "dem": dem_label(area),
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "nodes": int(len(graph.node_x)),
        "edges": {model.KINDS[k]: int(c) for k, c in enumerate(counts)},
        "closed_edges": int(closed.sum()),
        "barriers": _barrier_counts(graph.debug.get("barriers", [])),
        "pass_names": [p["name"] for p in graph.passes],
        "params": cfg.model_params(),
    }
    log.info("%s: %s nodes, edges %s, %s closed both ways", area.name, meta["nodes"],
             meta["edges"], meta["closed_edges"])

    def to_lonlat(geoms):
        import shapely

        def fn(coords):
            lon, lat = projector.to_lonlat(coords[:, 0], coords[:, 1])
            return np.column_stack([lon, lat])

        return shapely.transform(np.asarray(geoms, dtype=object), fn)

    writer = (lambda path: debug_gpkg.write(path, arrays, graph.debug, to_lonlat)) if debug else None
    return arrays, meta, writer


def crevasse_raster(cfg: Config, grid, osm, projector: Projector) -> tuple[np.ndarray | None, list]:
    """Mapped crevasse fields (and buffered single crevasses) on the grid,
    and the shapes themselves (for debug.gpkg)."""
    shapes = list(project(osm.crevasse_areas, projector)) if osm.crevasse_areas else []
    if osm.crevasse_lines:
        shapes += [line.buffer(cfg.crevasse.osm_line_buffer_m)
                   for line in project(osm.crevasse_lines, projector)]
    if not shapes:
        return None, []
    return rasterize(grid, [(1, shapes)], all_touched=True), shapes


def river_size(cfg: Config, area: Area, glaciers: Path | None, projector: Projector,
               work: Path, grid, hyd: hydrology.Hydrology, terrain) -> RiverSize:
    """Effective catchments from the area's own DEM and from a coarse one
    reaching catchment_margin_m further, glaciers weighted (routing.build.rivers).
    *glaciers*: cut_osm's natural=glacier subset of that wider area."""
    g, factor = cfg.graph, cfg.ford.glacier_factor
    sizes = RiverSize().add(grid, effective_catchment(
        grid, hyd, terrain == model.TERRAIN["glacier"], factor))
    if g.catchment_margin_m <= g.margin_m:
        return sizes

    wide = grow_bbox(area.bbox, g.catchment_margin_m)
    wide_dir = work / "wide"
    wide_dir.mkdir(exist_ok=True)
    log.info("%s: catchments on a %.0f m DEM, %.0f km around", area.name, g.catchment_res_m,
             g.catchment_margin_m / 1000)
    caught = demcache.relief(cfg, area, wide, projector.epsg, g.catchment_res_m, wide_dir, "wide")
    wgrid, whyd = caught.grid, caught.hyd

    glacier = np.zeros(wgrid.shape, dtype=bool)
    if factor > 0 and glaciers is not None:
        geoms = osm_areas.load_area_geoms(glaciers, "natural", "glacier", prefiltered=True)
        if geoms:
            glacier = rasterize(wgrid, [(1, list(project(geoms, projector)))]) > 0
        log.info("%s: %.0f km2 of glacier within the catchment grid", area.name,
                 glacier.sum() * wgrid.cell_km2)
    return sizes.add(wgrid, effective_catchment(wgrid, whyd, glacier, factor))


def package_arrays(cfg: Config, rasters: Rasters, graph, projector: Projector) -> dict[str, np.ndarray]:
    """Cost every edge both ways and lay the graph out as the package stores it."""
    grid, dem = rasters.grid, rasters.dem
    cm = CostModel(cfg)
    costs = cm.edge_costs(rasters, graph.geom_xy, graph.geom_off, graph.edge_kind, graph.way_factor,
                          graph.steep_ok)
    # a cliff climbed on the way to a pass: ropes, belays - an hour more
    climbed = graph.cliff_crossed
    for t in (costs.time_fwd, costs.time_bwd):
        t[climbed & np.isfinite(t)] += cfg.graph.pass_cliff_time_s
    ford = graph.edge_kind == model.FORD
    costs.time_fwd[ford] = costs.time_bwd[ford] = cfg.ford.marked_ford_time_s / 2
    costs.weight_fwd[ford] = costs.weight_bwd[ford] = cfg.ford.marked_ford_time_s / 2
    # wading a big river with a pack on is the roughest going there is
    costs.rough_fwd[ford] = costs.rough_bwd[ford] = cfg.ford.marked_ford_time_s / 2
    for name in ("climb_fwd", "climb_bwd", "descent_fwd", "descent_bwd", "steep_fwd", "steep_bwd"):
        getattr(costs, name)[ford] = 0.0
    costs.terrain[ford] = model.TERRAIN["water"]
    penalty = cm.penalty[graph.edge_kind]
    weight_fwd = costs.weight_fwd * penalty + graph.extra_s
    weight_bwd = costs.weight_bwd * penalty + graph.extra_s
    # the steep-descent part as the weight carries it
    costs.steep_fwd *= penalty
    costs.steep_bwd *= penalty

    node_lon, node_lat = projector.to_lonlat(graph.node_x, graph.node_y)
    geom_lon, geom_lat = projector.to_lonlat(graph.geom_xy[:, 0], graph.geom_xy[:, 1])
    node_ele = np.nan_to_num(grid.bilinear(dem, graph.node_x, graph.node_y), nan=0.0)
    geom_ele = np.nan_to_num(grid.bilinear(dem, graph.geom_xy[:, 0], graph.geom_xy[:, 1]), nan=0.0)
    return {
        "node_lon": node_lon, "node_lat": node_lat,
        "node_x": graph.node_x, "node_y": graph.node_y,
        "node_ele": node_ele.astype(np.float32),
        "node_kind": graph.node_kind, "node_osm": graph.node_osm,
        "edge_u": graph.edge_u, "edge_v": graph.edge_v, "edge_kind": graph.edge_kind,
        "edge_terrain": costs.terrain, "edge_len": costs.length.astype(np.float32),
        "time_fwd": costs.time_fwd.astype(np.float32), "time_bwd": costs.time_bwd.astype(np.float32),
        "weight_fwd": weight_fwd.astype(np.float32), "weight_bwd": weight_bwd.astype(np.float32),
        "edge_osm_way": graph.osm_way,
        "edge_extra": graph.extra_s.astype(np.float32),
        # routing.passlimit: the pass an edge crosses the ridge at (counted,
        # and closed when that pass is ruled out) ...
        "edge_cross": graph.cross_pass.astype(np.int32),
        # ... and the pass an edge needs at all: that one, else its own
        # descents, else the pass whose zone alone makes it walkable
        "edge_pass": np.where(
            graph.cross_pass >= 0, graph.cross_pass,
            np.where((graph.edge_kind == model.PASS) | (graph.steep_ok & (graph.soft_crossed | costs.waived)),
                     graph.zone_pass, -1)).astype(np.int32),
        **pass_arrays(graph, projector),
        **{name: getattr(costs, name).astype(np.float32) for name in
           ("climb_fwd", "climb_bwd", "descent_fwd", "descent_bwd", "rough_fwd", "rough_bwd",
            "steep_fwd", "steep_bwd")},
        "geom_off": graph.geom_off, "geom_lon": geom_lon, "geom_lat": geom_lat,
        "geom_ele": geom_ele.astype(np.float32),
    }


def pass_arrays(graph, projector: Projector) -> dict[str, np.ndarray]:
    """The pass table: OSM id, rtsa_scale grade and position of each pass."""
    t = graph.passes
    lon, lat = projector.to_lonlat([p["x"] for p in t], [p["y"] for p in t])
    return {
        "pass_osm": np.array([p["osm"] for p in t], dtype=np.int64),
        "pass_grade": np.array([p["grade"] for p in t], dtype=np.uint8),
        "pass_lon": np.asarray(lon, dtype=np.float64), "pass_lat": np.asarray(lat, dtype=np.float64),
    }


def _barrier_counts(items) -> dict[str, int]:
    out: dict[str, int] = {}
    for _, label in items:
        out[label] = out.get(label, 0) + 1
    return out
