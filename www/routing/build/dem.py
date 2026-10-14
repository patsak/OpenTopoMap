"""The area's DEM on the working grid: UTM, square cells, NaN for no data."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import numpy as np
from osgeo import gdal

from routing.grid import Grid, Projector

gdal.UseExceptions()
log = logging.getLogger(__name__)

WARP_ATTEMPTS = 3


def cached_source(data_dir: Path, bbox: tuple[float, float, float, float], work_dir: Path,
                  name: str) -> str:
    """The DEM for *bbox* out of the HGT cache the Garmin builds read too
    (otmlib.dem: 1° SRTM1 tiles cut from GEDTM, fetched the first time they
    are wanted), joined by a VRT. Off a local disk a tile's DEM takes
    seconds, not the minutes of remote reads."""
    from otmlib import dem as otm_dem
    from otmlib import paths

    tiles = otm_dem.ensure_hgt_tiles(*bbox, paths.hgt_cache(data_dir), log=lambda m: log.debug("%s", m))
    vrt = work_dir / f"{name}.vrt"
    ds = gdal.BuildVRT(str(vrt), [str(t) for t in tiles])
    if ds is None:
        raise RuntimeError(f"gdal.BuildVRT failed for {len(tiles)} HGT tiles")
    ds.FlushCache()
    del ds
    return str(vrt)


def default_source() -> str:
    # Importing otmlib.dem also sets GDAL's HTTP timeouts and cache for the
    # remote GEDTM reads.
    from otmlib import dem as otm_dem

    return otm_dem.GEDTM_VSI_URL


def utm_bounds(bbox: tuple[float, float, float, float], epsg: int, res: float) -> tuple[float, ...]:
    """The bbox's envelope in the UTM zone, snapped outwards to whole cells."""
    west, south, east, north = bbox
    t = np.linspace(0.0, 1.0, 33)
    lon = np.concatenate([west + (east - west) * t, np.full(33, east),
                          west + (east - west) * t, np.full(33, west)])
    lat = np.concatenate([np.full(33, south), south + (north - south) * t,
                          np.full(33, north), south + (north - south) * t])
    x, y = Projector(epsg).to_xy(lon, lat)
    x0 = np.floor(x.min() / res) * res
    y0 = np.floor(y.min() / res) * res
    x1 = np.ceil(x.max() / res) * res
    y1 = np.ceil(y.max() / res) * res
    return float(x0), float(y0), float(x1), float(y1)


def warp(source: str, bbox: tuple[float, float, float, float], epsg: int, res: float,
         out_tif: Path) -> Path:
    bounds = utm_bounds(bbox, epsg, res)
    log.info("DEM warp to EPSG:%s at %.0f m: %.0f x %.0f km", epsg, res,
             (bounds[2] - bounds[0]) / 1000, (bounds[3] - bounds[1]) / 1000)
    options = gdal.WarpOptions(
        format="GTiff",
        dstSRS=f"EPSG:{epsg}",
        outputBounds=bounds,
        xRes=res,
        yRes=res,
        resampleAlg=gdal.GRA_Bilinear,
        outputType=gdal.GDT_Float32,
        dstNodata=-32768,
        multithread=True,
        creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"],
    )
    # The DEM is read over HTTP: GDAL retries a request a few times itself
    # (otmlib.dem), but under a busy parallel build a block read still fails
    # now and then - the whole warp is tried again before the tile gives up.
    for attempt in range(WARP_ATTEMPTS):
        try:
            ds = gdal.Warp(str(out_tif), source, options=options)
            if ds is None:
                raise RuntimeError(f"gdal.Warp failed for {source}")
            ds.FlushCache()
            del ds
            return out_tif
        except RuntimeError as exc:
            if attempt == WARP_ATTEMPTS - 1:
                raise
            log.warning("DEM warp failed (%s), trying again", exc)
            time.sleep(10 * (attempt + 1))
    return out_tif


def load(tif: Path) -> tuple[Grid, np.ndarray]:
    ds = gdal.Open(str(tif))
    grid = Grid.from_dataset(ds)
    band = ds.GetRasterBand(1)
    dem = band.ReadAsArray().astype(np.float32)
    nodata = band.GetNoDataValue()
    if nodata is not None:
        dem[dem == nodata] = np.nan
    dem[(dem < -1000) | (dem > 9000)] = np.nan
    return grid, dem


def write(grid: Grid, array: np.ndarray, path: Path, nodata: float | None = None) -> Path:
    """A raster on *grid*, for GRASS or for looking at."""
    types = {np.dtype("float32"): gdal.GDT_Float32, np.dtype("float64"): gdal.GDT_Float64,
             np.dtype("uint8"): gdal.GDT_Byte, np.dtype("int16"): gdal.GDT_Int16,
             np.dtype("int32"): gdal.GDT_Int32}
    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(str(path), grid.width, grid.height, 1, types[array.dtype],
                       options=["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"])
    ds.SetGeoTransform(grid.geotransform)
    ds.SetProjection(grid.srs().ExportToWkt())
    band = ds.GetRasterBand(1)
    if nodata is not None:
        band.SetNoDataValue(nodata)
    band.WriteArray(array)
    ds.FlushCache()
    del ds
    return path


def gradients(dem: np.ndarray, res: float) -> tuple[np.ndarray, np.ndarray]:
    """dz/dx (east) and dz/dy (north). Next to a hole the one-sided difference
    is used where there is one; a cell with no neighbour at all gets 0."""
    filled = dem.copy()
    holes = np.isnan(filled)
    if holes.any():
        from scipy import ndimage

        _, (ri, ci) = ndimage.distance_transform_edt(holes, return_indices=True)
        filled = filled[ri, ci]
    if filled.shape[0] < 2 or filled.shape[1] < 2:
        zero = np.zeros_like(filled)
        return zero, zero
    d_row, d_col = np.gradient(filled, res)
    return d_col.astype(np.float32), (-d_row).astype(np.float32)


def slope_deg(gx: np.ndarray, gy: np.ndarray) -> np.ndarray:
    return np.degrees(np.arctan(np.hypot(gx, gy))).astype(np.float32)
