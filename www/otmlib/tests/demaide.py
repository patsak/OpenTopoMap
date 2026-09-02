"""Real SRTM3 HGT tiles for the DEM cache — the format otmlib.dem reads."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import numpy as np

from otmlib.dem import HGT_SIDE, tile_name

# lat, lon (degrees) -> elevation in metres
Surface = Callable[[np.ndarray, np.ndarray], np.ndarray]


def cone(peak_lat: float, peak_lon: float, height_m: float, radius_deg: float) -> Surface:
    """A single mountain: elevation falls linearly from the peak to zero."""

    def surface(lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
        distance = np.hypot(lats - peak_lat, lons - peak_lon)
        return np.clip(height_m * (1.0 - distance / radius_deg), 0.0, None)

    return surface


def ramp(low_m: float, high_m: float) -> Surface:
    """Elevation rising from south to north, so every contour is one line."""

    def surface(lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
        south = np.floor(lats.min())
        return low_m + (lats - south) * (high_m - low_m)

    return surface


def makeHgtTile(cache_dir: Path, lat: int, lon: int, surface: Surface) -> Path:
    """Write one 1° SRTM3 HGT tile into *cache_dir*."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    # HGT rows run north to south, columns west to east, corners inclusive.
    lats = np.linspace(lat + 1, lat, HGT_SIDE)
    lons = np.linspace(lon, lon + 1, HGT_SIDE)
    grid_lat, grid_lon = np.meshgrid(lats, lons, indexing="ij")

    elevation = surface(grid_lat, grid_lon).astype(">i2")
    path = cache_dir / f"{tile_name(lat, lon)}.hgt"
    path.write_bytes(elevation.tobytes())
    return path
