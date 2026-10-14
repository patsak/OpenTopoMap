"""A north-up raster grid in a metric CRS, and lon/lat <-> grid CRS.

The build works in the UTM zone of the area: the DEM is warped onto this grid,
every raster derived from it (slope, catchment, terrain class) shares it, and
every graph coordinate is in its metres until the package stores lon/lat.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from osgeo import osr

osr.UseExceptions()


@dataclass(frozen=True)
class Grid:
    x0: float  # west edge
    y0: float  # north edge
    res: float  # square cells, metres
    width: int
    height: int
    epsg: int

    @classmethod
    def from_dataset(cls, ds) -> Grid:
        gt = ds.GetGeoTransform()
        if abs(abs(gt[1]) - abs(gt[5])) > 1e-6 or gt[2] or gt[4]:
            raise ValueError("the grid wants square, north-up cells")
        srs = osr.SpatialReference(wkt=ds.GetProjection())
        srs.AutoIdentifyEPSG()
        return cls(gt[0], gt[3], gt[1], ds.RasterXSize, ds.RasterYSize, int(srs.GetAuthorityCode(None)))

    @property
    def geotransform(self) -> tuple[float, ...]:
        return (self.x0, self.res, 0.0, self.y0, 0.0, -self.res)

    @property
    def shape(self) -> tuple[int, int]:
        return self.height, self.width

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return self.x0, self.y0 - self.height * self.res, self.x0 + self.width * self.res, self.y0

    @property
    def cell_km2(self) -> float:
        return self.res * self.res / 1e6

    def srs(self) -> osr.SpatialReference:
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(self.epsg)
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        return srs

    # -------------------------------------------------------------- indexing

    def fractional(self, x, y) -> tuple[np.ndarray, np.ndarray]:
        """(row, col) in cell units, 0.0 at the centre of the first cell."""
        col = (np.asarray(x, dtype=np.float64) - self.x0) / self.res - 0.5
        row = (self.y0 - np.asarray(y, dtype=np.float64)) / self.res - 0.5
        return row, col

    def cell_of(self, x, y) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Integer (row, col) of the cell holding each point, and which are inside."""
        row, col = self.fractional(x, y)
        r = np.floor(row + 0.5).astype(np.int64)
        c = np.floor(col + 0.5).astype(np.int64)
        inside = (r >= 0) & (r < self.height) & (c >= 0) & (c < self.width)
        return r, c, inside

    def cell_xy(self, rows, cols) -> tuple[np.ndarray, np.ndarray]:
        x = self.x0 + (np.asarray(cols) + 0.5) * self.res
        y = self.y0 - (np.asarray(rows) + 0.5) * self.res
        return x, y

    # -------------------------------------------------------------- sampling

    def nearest(self, raster: np.ndarray, x, y, fill=np.nan) -> np.ndarray:
        r, c, inside = self.cell_of(x, y)
        out = np.full(r.shape, fill, dtype=np.result_type(raster.dtype, np.asarray(fill).dtype))
        out[inside] = raster[r[inside], c[inside]]
        return out

    def bilinear(self, raster: np.ndarray, x, y) -> np.ndarray:
        """Bilinear between cell centres; NaN outside the grid or next to a NaN."""
        row, col = self.fractional(x, y)
        row = np.clip(row, 0.0, self.height - 1.0)
        col = np.clip(col, 0.0, self.width - 1.0)
        r0 = np.minimum(np.floor(row).astype(np.int64), self.height - 2) if self.height > 1 else np.zeros(row.shape, np.int64)
        c0 = np.minimum(np.floor(col).astype(np.int64), self.width - 2) if self.width > 1 else np.zeros(col.shape, np.int64)
        r1 = np.minimum(r0 + 1, self.height - 1)
        c1 = np.minimum(c0 + 1, self.width - 1)
        fr = row - r0
        fc = col - c0
        a = raster[r0, c0]
        b = raster[r0, c1]
        c = raster[r1, c0]
        d = raster[r1, c1]
        out = (a * (1 - fc) + b * fc) * (1 - fr) + (c * (1 - fc) + d * fc) * fr
        _, _, inside = self.cell_of(x, y)
        return np.where(inside, out, np.nan)


class Projector:
    """lon/lat (EPSG:4326, lon first) <-> the grid's CRS, vectorised."""

    def __init__(self, epsg: int):
        self.epsg = epsg
        wgs84 = osr.SpatialReference()
        wgs84.ImportFromEPSG(4326)
        wgs84.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        local = osr.SpatialReference()
        local.ImportFromEPSG(epsg)
        local.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        self._fwd = osr.CoordinateTransformation(wgs84, local)
        self._inv = osr.CoordinateTransformation(local, wgs84)

    @staticmethod
    def _run(ct, a, b) -> tuple[np.ndarray, np.ndarray]:
        a = np.asarray(a, dtype=np.float64).ravel()
        b = np.asarray(b, dtype=np.float64).ravel()
        if a.size == 0:
            return a.copy(), b.copy()
        out = np.asarray(ct.TransformPoints(np.column_stack([a, b])))
        return out[:, 0], out[:, 1]

    def to_xy(self, lon, lat) -> tuple[np.ndarray, np.ndarray]:
        return self._run(self._fwd, lon, lat)

    def to_lonlat(self, x, y) -> tuple[np.ndarray, np.ndarray]:
        return self._run(self._inv, x, y)
