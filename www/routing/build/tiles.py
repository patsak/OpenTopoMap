"""Cutting a region too big for one graph into tiles.

A region (an area with ``tile_km``) is laid out in rows of square tiles,
``tile_km`` a side; a tile is kept when

* its ground in the Geofabrik regions named in ``within`` (the mountains "in
  Russia": the federal districts, not the neighbours) spans at least
  ``min_relief_m`` on a coarse (~1 km) DEM - the plains around a range are not
  worth an off-trail graph. However little of a border tile is inside, its
  mountains count: the Russian side of the main Caucasus ridge is a strip a
  few km wide in tiles mostly in Georgia.

The kept tiles are written to ``<routing>/<region>.tiles.json``; routing.config
reads them back as areas of their own, each built like any other area. Tiles
only abut: each graph reaches LATTICE_MARGIN past its tile anyway, and
routing.compile stitches neighbours across that strip.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import numpy as np
import shapely
from osgeo import gdal

from routing.config import Area, Config, plan_fingerprint

gdal.UseExceptions()
log = logging.getLogger(__name__)

MIN_CELLS = 20  # coarse cells inside, ~20 km2
RELIEF_RES_DEG = 0.01
KM_PER_DEG = 111.32


def layout(region: Area) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Every candidate tile: rows south to north, columns west to east."""
    west, south, east, north = region.bbox
    step_lat = region.tile_km / KM_PER_DEG
    out = []
    for r in range(math.ceil((north - south) / step_lat - 1e-9)):
        s = south + r * step_lat
        n = min(north, s + step_lat)
        step_lon = region.tile_km / (KM_PER_DEG * math.cos(math.radians((s + n) / 2)))
        for c in range(math.ceil((east - west) / step_lon - 1e-9)):
            w = west + c * step_lon
            out.append((f"{region.name}-{r:02d}-{c:02d}", (w, s, min(east, w + step_lon), n)))
    return out


def within_shape(region: Area, cache_dir: Path):
    if not region.within:
        return None
    from otmlib import geofabrik

    shapes = [geofabrik.region_by_id(rid, cache_dir=cache_dir).geometry for rid in region.within]
    return shapely.union_all(shapes)


def coarse_dem(region: Area, source: str) -> tuple[np.ndarray, tuple[float, ...]]:
    west, south, east, north = region.bbox
    ds = gdal.Warp("", source, options=gdal.WarpOptions(
        format="MEM", outputBounds=[west, south, east, north], dstSRS="EPSG:4326",
        xRes=RELIEF_RES_DEG, yRes=RELIEF_RES_DEG, resampleAlg=gdal.GRA_Average,
        outputType=gdal.GDT_Float32, dstNodata=-32768,
    ))
    dem = ds.GetRasterBand(1).ReadAsArray().astype(np.float32)
    dem[(dem < -1000) | (dem > 9000)] = np.nan
    return dem, ds.GetGeoTransform()


def plan(cfg: Config, region: Area) -> list[str]:
    """Lay out *region*, keep its mountain tiles, write the plan; their names."""
    from otmlib import paths

    from routing.build import dem as dem_mod

    shape = within_shape(region, paths.geofabrik_cache(cfg.data_dir))
    source = str(region.dem) if region.dem else dem_mod.default_source()
    dem, gt = coarse_dem(region, source)
    rows, cols = np.mgrid[0:dem.shape[0], 0:dem.shape[1]]
    lon = gt[0] + (cols + 0.5) * gt[1]
    lat = gt[3] + (rows + 0.5) * gt[5]
    inside = np.isfinite(dem)
    if shape is not None:
        shapely.prepare(shape)
        inside &= shapely.contains_xy(shape, lon, lat)

    kept, dropped_out, dropped_flat = [], 0, 0
    for name, (w, s, e, n) in layout(region):
        cells = inside & (lon >= w) & (lon < e) & (lat >= s) & (lat < n)
        heights = dem[cells]
        if heights.size < MIN_CELLS:
            dropped_out += 1
            continue
        relief = float(np.percentile(heights, 99) - np.percentile(heights, 1))
        if relief < region.min_relief_m:
            dropped_flat += 1
            continue
        kept.append({"name": name, "bbox": [round(v, 6) for v in (w, s, e, n)], "relief_m": round(relief)})
    path = cfg.tiles_path(region.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"plan": plan_fingerprint(region), "region": region.name,
                                "tile_km": region.tile_km, "tiles": kept}, indent=1), encoding="utf-8")
    log.info("%s: %s tiles of %s km kept, %s outside %s, %s flatter than %s m", region.name, len(kept),
             region.tile_km, dropped_out, ", ".join(region.within) or "-", dropped_flat, region.min_relief_m)
    return [t["name"] for t in kept]
