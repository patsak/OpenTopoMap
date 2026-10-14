"""Warped relief, its watershed and the lines taken from them, on disk.

The HGT tiles change only when a degree is fetched again. OSM changes every
night. So the expensive DEM work is remembered under ``routing/.cache/dem``
and a later rebuild of the same tile reads it back:

* the relief on the working grid (the warp) and ``r.watershed``'s directions
  and accumulation, for the fine grid and for the coarse catchment grid;
* the thalweg runs and the divides drawn from that watershed at the current
  catchment thresholds.

A pass's arrival cell still resplits the runs, and a mapped lake still keeps
crests off its shore: both depend on OSM, so they are applied when the graph
is built, from the cached watershed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from routing.build import dem as dem_mod
from routing.build import hydrology
from routing.config import Area, Config
from routing.grid import Grid

log = logging.getLogger(__name__)

# Bump when a cached raster or line would come out differently for the same
# tiles, grid and thresholds (see hydrology.resolve_method).
DEM_CACHE_VERSION = 1


@dataclass
class Surface:
    grid: Grid
    dem: np.ndarray
    hyd: hydrology.Hydrology


@dataclass
class Relief:
    """A surface plus the cache key of the tiles and grid it was built from."""

    grid: Grid
    dem: np.ndarray
    hyd: hydrology.Hydrology
    key: str


@dataclass
class DemLines:
    """Thalweg runs (flat cell indices) and divides (polylines in the grid CRS),
    with no OSM in them: no pass arrivals, no lake shore."""

    chains: list[np.ndarray]
    ridges: list[np.ndarray]


def raster_key(bbox: tuple[float, float, float, float], epsg: int, res: float, method: str,
               sources: list[tuple[str, int, int]], kind: str) -> str:
    """Identity of one warped watershed: the DEM files, the grid, the method."""
    return _key({
        "version": DEM_CACHE_VERSION,
        "kind": kind,
        "bbox": [float(v) for v in bbox],
        "epsg": int(epsg),
        "res": float(res),
        "hydrology": method,
        "sources": [[name, int(size), int(mtime)] for name, size, mtime in sorted(sources)],
    })


def edge_key(valley_catchment_km2: float, thalweg_node_spacing_m: float,
             ridge_catchment_km2: float) -> str:
    """Identity of the lines taken off one watershed."""
    return _key({
        "version": DEM_CACHE_VERSION,
        "valley_catchment_km2": float(valley_catchment_km2),
        "thalweg_node_spacing_m": float(thalweg_node_spacing_m),
        "ridge_catchment_km2": float(ridge_catchment_km2),
    })


def relief(cfg: Config, area: Area, bbox: tuple[float, float, float, float], epsg: int,
           res: float, work: Path, kind: str) -> Relief:
    """The warped DEM and its watershed for *bbox*, from the cache when the
    HGT tiles (or *area*'s own DEM) and the grid have not changed."""
    method = hydrology.resolve_method(cfg.graph.hydrology)
    sources = dem_sources(cfg, area, bbox)
    key = raster_key(bbox, epsg, res, method, sources, kind)
    path = _cache_dir(cfg) / f"{key}.npz"
    cached = load_surface(path)
    if cached is not None:
        log.info("%s: DEM cache hit (%s, %.0f m, %sx%s)", kind, method, res,
                 cached.grid.width, cached.grid.height)
        return Relief(cached.grid, cached.dem, cached.hyd, key)
    log.info("%s: DEM cache miss, warping at %.0f m and running %s watershed", kind, res, method)
    surface = _compute(cfg, area, bbox, epsg, res, work, kind, method)
    save_surface(path, surface)
    log.info("%s: DEM cached (%sx%s)", kind, surface.grid.width, surface.grid.height)
    return Relief(surface.grid, surface.dem, surface.hyd, key)


def lines(cfg: Config, relief_: Relief) -> DemLines:
    """Thalweg runs and divides of *relief_*, from the cache when the catchment
    thresholds have not changed."""
    g = cfg.graph
    stamp = edge_key(g.valley_catchment_km2, g.thalweg_node_spacing_m, g.ridge_catchment_km2)
    path = _cache_dir(cfg) / f"{relief_.key}-{stamp}.npz"
    cached = load_lines(path)
    if cached is not None:
        log.info("DEM edges cache hit: %s thalweg runs, %s divides", len(cached.chains), len(cached.ridges))
        return cached
    extracted = extract_lines(cfg, relief_.grid, relief_.hyd)
    save_lines(path, extracted)
    for stale in path.parent.glob(f"{relief_.key}-*.npz"):
        if stale != path:
            stale.unlink()
    log.info("DEM edges cached: %s thalweg runs, %s divides", len(extracted.chains), len(extracted.ridges))
    return extracted


def extract_lines(cfg: Config, grid: Grid, hyd: hydrology.Hydrology) -> DemLines:
    """The lines ``GraphBuilder`` walks, before OSM moves a pass or a lake."""
    g = cfg.graph
    spacing = max(1, round(g.thalweg_node_spacing_m / grid.res))
    chains = hydrology.thalweg_chains(hyd.accum_km2, hyd.downstream(), g.valley_catchment_km2, spacing)
    labels = hydrology.basin_labels(hyd.accum_km2, hyd.downstream(), g.ridge_catchment_km2)
    ridges = hydrology.divide_lines(labels, grid)
    return DemLines(chains=chains, ridges=ridges)


def dem_sources(cfg: Config, area: Area, bbox: tuple[float, float, float, float]) -> list[tuple[str, int, int]]:
    """The DEM files *bbox* is cut from, each as (name, size, mtime_ns)."""
    if area.dem:
        path = Path(area.dem)
        if not path.is_file():
            raise FileNotFoundError(f"area {area.name}: no such DEM {path}")
        st = path.stat()
        return [(str(path.resolve()), st.st_size, st.st_mtime_ns)]
    from otmlib import dem as otm_dem
    from otmlib import paths

    tiles = otm_dem.ensure_hgt_tiles(*bbox, paths.hgt_cache(cfg.data_dir),
                                     log=lambda m: log.debug("%s", m))
    out = []
    for tile in tiles:
        st = tile.stat()
        out.append((tile.name, st.st_size, st.st_mtime_ns))
    return out


def load_or_build_surface(path: Path, build: Callable[[], Surface]) -> Surface:
    """*build* only when *path* is missing or unreadable."""
    cached = load_surface(path)
    if cached is not None:
        return cached
    surface = build()
    save_surface(path, surface)
    return surface


def load_surface(path: Path) -> Surface | None:
    if not path.is_file():
        return None
    try:
        with np.load(path) as z:
            spec = np.array(z["grid"], dtype=np.float64)
            dem = np.array(z["dem"], dtype=np.float32)
            drain = np.array(z["drain"], dtype=np.int8)
            accum = np.array(z["accum"], dtype=np.float32)
    except (OSError, ValueError, KeyError, IndexError) as exc:
        log.warning("DEM cache unreadable (%s), rebuilding %s", exc, path.name)
        return None
    x0, y0, res, width, height, epsg = spec
    grid = Grid(float(x0), float(y0), float(res), int(width), int(height), int(epsg))
    if dem.shape != grid.shape or drain.shape != grid.shape or accum.shape != grid.shape:
        log.warning("DEM cache shape does not match its grid, rebuilding %s", path.name)
        return None
    return Surface(grid, dem, hydrology.Hydrology(drain=drain, accum_km2=accum))


def save_surface(path: Path, surface: Surface) -> None:
    g = surface.grid
    _write(path, {
        "grid": np.array([g.x0, g.y0, g.res, g.width, g.height, g.epsg], dtype=np.float64),
        "dem": np.ascontiguousarray(surface.dem, dtype=np.float32),
        "drain": np.ascontiguousarray(surface.hyd.drain, dtype=np.int8),
        "accum": np.ascontiguousarray(surface.hyd.accum_km2, dtype=np.float32),
    })


def load_lines(path: Path) -> DemLines | None:
    if not path.is_file():
        return None
    try:
        with np.load(path) as z:
            chain_idx = np.array(z["chain_idx"], dtype=np.int64)
            chain_off = np.array(z["chain_off"], dtype=np.int64)
            ridge_xy = np.array(z["ridge_xy"], dtype=np.float64)
            ridge_off = np.array(z["ridge_off"], dtype=np.int64)
    except (OSError, ValueError, KeyError, IndexError) as exc:
        log.warning("DEM edge cache unreadable (%s), rebuilding %s", exc, path.name)
        return None
    if chain_off.ndim != 1 or ridge_off.ndim != 1 or chain_off[0] != 0 or ridge_off[0] != 0:
        return None
    if chain_off[-1] != len(chain_idx) or ridge_off[-1] != len(ridge_xy):
        return None
    chains = [chain_idx[a:b] for a, b in zip(chain_off[:-1], chain_off[1:])]
    ridges = [ridge_xy[a:b] for a, b in zip(ridge_off[:-1], ridge_off[1:])]
    return DemLines(chains=chains, ridges=ridges)


def save_lines(path: Path, lines_: DemLines) -> None:
    chain_idx, chain_off = _pack([np.asarray(c, dtype=np.int64).ravel() for c in lines_.chains], np.int64)
    parts = [np.asarray(xy, dtype=np.float64).reshape(-1, 2) for xy in lines_.ridges]
    if parts:
        ridge_xy = np.concatenate(parts)
        ridge_off = np.zeros(len(parts) + 1, dtype=np.int64)
        ridge_off[1:] = np.cumsum([len(p) for p in parts])
    else:
        ridge_xy = np.zeros((0, 2), dtype=np.float64)
        ridge_off = np.zeros(1, dtype=np.int64)
    _write(path, {
        "chain_idx": chain_idx, "chain_off": chain_off,
        "ridge_xy": ridge_xy, "ridge_off": ridge_off,
    })


def _pack(parts: list[np.ndarray], dtype) -> tuple[np.ndarray, np.ndarray]:
    if not parts:
        return np.zeros(0, dtype=dtype), np.zeros(1, dtype=np.int64)
    flat = np.concatenate(parts).astype(dtype, copy=False)
    off = np.zeros(len(parts) + 1, dtype=np.int64)
    off[1:] = np.cumsum([len(p) for p in parts])
    return flat, off


def _compute(cfg: Config, area: Area, bbox: tuple[float, float, float, float], epsg: int,
             res: float, work: Path, kind: str, method: str) -> Surface:
    work.mkdir(parents=True, exist_ok=True)
    source = str(area.dem) if area.dem else dem_mod.cached_source(cfg.data_dir, bbox, work, kind)
    tif = dem_mod.warp(source, bbox, epsg, res, work / f"{kind}-dem.tif")
    grid, dem = dem_mod.load(tif)
    hyd = hydrology.compute(tif, grid, dem, work, method)
    return Surface(grid, dem, hyd)


def _cache_dir(cfg: Config) -> Path:
    return cfg.routing_dir / ".cache" / "dem"


def _key(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:24]


def _write(path: Path, arrays: dict[str, np.ndarray]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".partial")
    try:
        with tmp.open("wb") as fh:
            np.savez_compressed(fh, **arrays)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
