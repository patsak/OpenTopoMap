"""Classify DEM tiles as plain or mountain for adaptive contour spacing."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

import numpy as np
from osgeo import gdal

from garminsvc.constants import MOUNTAIN_MAX_ELEV_M, MOUNTAIN_RELIEF_M

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


def classify_dem(path: Path) -> Terrain:
    dem = _read_dem(path)
    valid = np.isfinite(dem)
    if valid.sum() == 0:
        log.warning("%s: no valid elevation data, treating as plain", path.name)
        return "plain"
    elev = dem[valid]
    relief = float(elev.max() - elev.min())
    max_elev = float(elev.max())
    zone: Terrain = (
        "mountain"
        if relief >= MOUNTAIN_RELIEF_M or max_elev >= MOUNTAIN_MAX_ELEV_M
        else "plain"
    )
    log.info("%s: %s relief=%.0fm max=%.0fm", path.name, zone, relief, max_elev)
    return zone


def contour_step(path: Path) -> int:
    return 20 if classify_dem(path) == "mountain" else 10
