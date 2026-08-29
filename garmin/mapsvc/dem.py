"""DEM download and conversion via GDAL Python API, with on-disk HGT cache."""

from __future__ import annotations

import math
import shutil
import struct
from pathlib import Path
from typing import Callable

from osgeo import gdal

from mapsvc.constants import DEM_CACHE

gdal.UseExceptions()

GEDTM_VSI_URL = (
    "/vsicurl/https://s3.opengeohub.org/global/dtm/v1.2/"
    "gedtm_rf_m_30m_s_20060101_20151231_go_epsg.4326.3855_v1.2.tif"
)

# SRTM3: 3 arc-second grid, 1201 x 1201 points per 1° tile
HGT_SIDE = 1201
HGT_BYTES = HGT_SIDE * HGT_SIDE * 2
HGT_VOID = -32768

LogFn = Callable[[str], None]


def tile_name(lat: int, lon: int) -> str:
    lat_s = f"S{abs(lat):02d}" if lat < 0 else f"N{lat:02d}"
    lon_s = f"W{abs(lon):03d}" if lon < 0 else f"E{lon:03d}"
    return f"{lat_s}{lon_s}"


def tiles_for_bbox(west: float, south: float, east: float, north: float) -> list[tuple[int, int]]:
    if east < west:
        west, east = east, west
    if north < south:
        south, north = north, south
    tiles: list[tuple[int, int]] = []
    for lon in range(math.floor(west), math.ceil(east)):
        for lat in range(math.floor(south), math.ceil(north)):
            tiles.append((lat, lon))
    return tiles


def translate_bbox_geotiff(
    west: float,
    south: float,
    east: float,
    north: float,
    output_path: Path,
    *,
    source_url: str = GEDTM_VSI_URL,
) -> Path:
    """Crop global GEDTM GeoTIFF to bbox (EPSG:4326) without shell gdal_translate."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    opts = gdal.TranslateOptions(
        format="GTiff",
        projWin=[west, north, east, south],
        projWinSRS="EPSG:4326",
        creationOptions=["COMPRESS=DEFLATE", "TILED=YES"],
    )
    ds = gdal.Translate(str(output_path), source_url, options=opts)
    if ds is None:
        raise RuntimeError(f"GDAL Translate failed for bbox {west},{south},{east},{north}")
    ds.FlushCache()
    ds = None
    return output_path


def _is_valid_hgt(path: Path) -> bool:
    return path.is_file() and path.stat().st_size == HGT_BYTES


def _write_hgt_tile(
    src_ds: gdal.Dataset,
    west: float,
    south: float,
    east: float,
    north: float,
    out_path: Path,
) -> None:
    """Warp a 1° cell to 1201x1201 Int16 HGT."""
    warp_opts = gdal.WarpOptions(
        format="MEM",
        outputBounds=[west, south, east, north],
        width=HGT_SIDE,
        height=HGT_SIDE,
        resampleAlg=gdal.GRA_Bilinear,
        dstNodata=HGT_VOID,
        outputType=gdal.GDT_Int16,
    )
    warped = gdal.Warp("", src_ds, options=warp_opts)
    if warped is None:
        raise RuntimeError(f"GDAL Warp failed for tile {out_path.name}")

    band = warped.GetRasterBand(1)
    data = band.ReadAsArray()
    warped = None

    if data is None:
        raise RuntimeError(f"No raster data for tile {out_path.name}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".part")
    if tmp.exists():
        tmp.unlink()
    with tmp.open("wb") as fh:
        for row in data:
            for val in row:
                fh.write(struct.pack(">h", int(val)))
    if tmp.stat().st_size != HGT_BYTES:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Invalid HGT size for {out_path.name}")
    tmp.replace(out_path)


def _fetch_full_hgt_tile(
    lat: int,
    lon: int,
    dest: Path,
    *,
    source_url: str = GEDTM_VSI_URL,
) -> Path:
    """Download one full 1° tile from GEDTM into dest (.hgt)."""
    west, south, east, north = float(lon), float(lat), float(lon + 1), float(lat + 1)
    tmp_tif = dest.with_suffix(".tif.part")
    if tmp_tif.exists():
        tmp_tif.unlink()
    translate_bbox_geotiff(west, south, east, north, tmp_tif, source_url=source_url)
    src = gdal.Open(str(tmp_tif))
    if src is None:
        raise RuntimeError(f"Cannot open temporary GeoTIFF for {dest.name}")
    try:
        _write_hgt_tile(src, west, south, east, north, dest)
    finally:
        src = None
        tmp_tif.unlink(missing_ok=True)
    return dest


def ensure_hgt_tiles(
    west: float,
    south: float,
    east: float,
    north: float,
    cache_dir: Path,
    log: LogFn | None = None,
) -> list[Path]:
    """Return cached full 1° HGT tiles covering bbox; download missing ones."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for lat, lon in tiles_for_bbox(west, south, east, north):
        name = tile_name(lat, lon)
        dest = cache_dir / f"{name}.hgt"
        if _is_valid_hgt(dest):
            if log:
                log(f"DEM cache hit: {dest.name}")
        else:
            if dest.exists():
                dest.unlink()
            if log:
                log(f"DEM download: {dest.name}")
            _fetch_full_hgt_tile(lat, lon, dest)
            if log:
                log(f"DEM cached: {dest.name} ({dest.stat().st_size // 1024} KB)")
        paths.append(dest)
    if not paths:
        raise RuntimeError("No DEM tiles for requested bbox")
    return paths


def geotiff_to_hgt_tiles(geotiff: Path, hgt_dir: Path) -> list[Path]:
    """Split GeoTIFF into 1° SRTM3-compatible .hgt tiles for mkgmap --dem."""
    hgt_dir.mkdir(parents=True, exist_ok=True)
    src = gdal.Open(str(geotiff))
    if src is None:
        raise RuntimeError(f"Cannot open GeoTIFF: {geotiff}")

    gt = src.GetGeoTransform()
    origin_x, pixel_w, _, origin_y, _, pixel_h = gt
    pixel_h = abs(pixel_h)
    width = src.RasterXSize
    height = src.RasterYSize

    west = origin_x
    north = origin_y
    east = west + width * pixel_w
    south = north - height * pixel_h

    out_paths: list[Path] = []
    for lat, lon in tiles_for_bbox(west, south, east, north):
        tile_west = max(west, float(lon))
        tile_south = max(south, float(lat))
        tile_east = min(east, float(lon + 1))
        tile_north = min(north, float(lat + 1))
        if tile_east <= tile_west or tile_north <= tile_south:
            continue
        name = tile_name(lat, lon)
        out_path = hgt_dir / f"{name}.hgt"
        _write_hgt_tile(src, tile_west, tile_south, tile_east, tile_north, out_path)
        out_paths.append(out_path)

    src = None
    if not out_paths:
        raise RuntimeError(f"No HGT tiles produced from {geotiff}")
    return out_paths


def prepare_dem_for_bbox(
    west: float,
    south: float,
    east: float,
    north: float,
    work_dir: Path,
    *,
    cache_dir: Path | None = None,
    log: LogFn | None = None,
) -> tuple[Path | None, Path]:
    """Prepare DEM for bbox using cached 1° HGT tiles.

    Returns (dem_tiff_or_None, hgt_dir). Contours use HGT when GeoTIFF is omitted.
    """
    cache = cache_dir or (DEM_CACHE / "hgt")
    cached = ensure_hgt_tiles(west, south, east, north, cache, log=log)

    hgt_dir = work_dir / "hgt"
    if hgt_dir.exists():
        shutil.rmtree(hgt_dir)
    hgt_dir.mkdir(parents=True)

    for src in cached:
        dest = hgt_dir / src.name
        try:
            dest.symlink_to(src.resolve())
        except OSError:
            shutil.copy2(src, dest)

    return None, hgt_dir
