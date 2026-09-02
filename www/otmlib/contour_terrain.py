"""Classify DEM tiles as plain or mountain for adaptive contour spacing."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

import numpy as np
from osgeo import gdal

from otmlib.constants import MOUNTAIN_MAX_ELEV_M, MOUNTAIN_RELIEF_M

log = logging.getLogger(__name__)

HGT_VOID = -32768
Terrain = Literal["mountain", "plain"]


def _read_dem(path: Path) -> np.ndarray:
    ds = gdal.Open(str(path))
    if ds is None:
        raise ValueError(f"Cannot open DEM: {path}")
    band = ds.GetRasterBand(1)
    arr = band.ReadAsArray().astype(np.float64)
    nodata = band.GetNoDataValue()
    if nodata is not None:
        arr[arr == nodata] = np.nan
    arr[arr == HGT_VOID] = np.nan
    ds = None
    return arr


def classify_grid(dem: np.ndarray) -> Terrain:
    """Mountain or plain, from relief and absolute height."""
    valid = np.isfinite(dem)
    if valid.sum() == 0:
        return "plain"
    elev = dem[valid]
    relief = float(elev.max() - elev.min())
    max_elev = float(elev.max())
    return (
        "mountain"
        if relief >= MOUNTAIN_RELIEF_M or max_elev >= MOUNTAIN_MAX_ELEV_M
        else "plain"
    )


def classify_dem(path: Path) -> Terrain:
    dem = _read_dem(path)
    zone = classify_grid(dem)
    valid = np.isfinite(dem)
    if valid.sum() == 0:
        log.warning("%s: no valid elevation data, treating as plain", path.name)
        return zone
    elev = dem[valid]
    log.info(
        "%s: %s relief=%.0fm max=%.0fm",
        path.name,
        zone,
        float(elev.max() - elev.min()),
        float(elev.max()),
    )
    return zone


def step_for(zone: Terrain) -> int:
    return 20 if zone == "mountain" else 10


def contour_step(path: Path) -> int:
    return step_for(classify_dem(path))
