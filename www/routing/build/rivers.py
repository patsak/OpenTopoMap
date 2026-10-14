"""How big the water is at a point: the catchment that decides fording.

Two corrections over the area's own r.watershed:

* **The whole catchment.** The fine DEM ends a couple of kilometres past the
  area, so a river rising beyond it - every glacier stream off a main ridge
  just outside the bbox - came out a brook. Catchments are also accumulated on
  a coarse DEM reaching ``catchment_margin_m`` further, and the larger of the
  two is taken.
* **Glacier water.** A glaciated basin sheds several times the summer
  discharge of a bare one of the same size: each km2 of glacier upstream
  counts ``1 + glacier_factor`` times. Glaciers come from OSM natural=glacier.

The result is an "effective catchment" in km2, compared with
``ford.max_catchment_km2`` and used for the wading penalty and the width of a
river nobody measured.
"""

from __future__ import annotations

import logging

import numpy as np
from scipy import ndimage

from routing.build import hydrology
from routing.grid import Grid

log = logging.getLogger(__name__)

# The OSM line and the DEM channel of one river are seldom the same line: a
# point counts the biggest water within this many cells of it.
NEAR_CELLS = 2


def weighted_accumulation(hyd: hydrology.Hydrology, weight: np.ndarray) -> np.ndarray:
    """Sum of *weight* over each cell's catchment, down the same D8 pointers."""
    down = hyd.downstream()
    acc = weight.astype(np.float64).ravel().copy()
    # With one downstream cell each, the catchment strictly grows downstream,
    # so ascending catchment is an upstream-first order.
    order = np.argsort(hyd.accum_km2.ravel(), kind="stable")
    for i in order:
        a = acc[i]
        if a:
            d = down[i]
            if d >= 0:
                acc[d] += a
    return acc.reshape(weight.shape)


def effective_catchment(grid: Grid, hyd: hydrology.Hydrology, glacier: np.ndarray,
                        glacier_factor: float) -> np.ndarray:
    eff = hyd.accum_km2.astype(np.float64)
    if glacier_factor > 0 and glacier.any():
        eff = eff + glacier_factor * weighted_accumulation(hyd, glacier.astype(np.float64) * grid.cell_km2)
    return eff


class RiverSize:
    """Effective catchment anywhere, from one or more grids (largest wins)."""

    def __init__(self) -> None:
        self.layers: list[tuple[Grid, np.ndarray]] = []

    def add(self, grid: Grid, effective: np.ndarray) -> RiverSize:
        size = 2 * NEAR_CELLS + 1
        self.layers.append((grid, ndimage.maximum_filter(effective.astype(np.float32), size=size)))
        return self

    def at(self, x, y) -> np.ndarray:
        x = np.atleast_1d(np.asarray(x, dtype=np.float64))
        y = np.atleast_1d(np.asarray(y, dtype=np.float64))
        out = np.zeros(x.shape)
        for grid, raster in self.layers:
            out = np.maximum(out, np.nan_to_num(grid.nearest(raster, x, y, fill=0.0)))
        return out

    def of_line(self, coords: np.ndarray) -> float:
        return float(self.at(coords[:, 0], coords[:, 1]).max()) if len(coords) else 0.0
