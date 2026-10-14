"""Where water runs on the DEM: D8 directions, catchments, fall lines.

The heavy part - a depression-aware flow direction and accumulation over the
whole area - is GRASS ``r.watershed`` (single flow direction, so every cell has
exactly one downstream neighbour), the same module ``tools/dem2rivers.py``
runs. A plain priority-flood in numpy stands in for it on small grids: the
tests, and a quick look at an area without GRASS installed.

Everything after that works on two rasters, ``drain`` (r.watershed's D8 code,
0 where water leaves the grid) and ``accum_km2`` (catchment of each cell):

* thalwegs - cells draining at least ``valley_catchment_km2``, chained down
  their D8 pointers into the valley-floor lines the graph walks;
* fall lines - the D8 path from any cell down to the first thalweg, which is
  how a pass is joined to the valleys on either side;
* canyons - thalweg cells whose walls on both sides are steep and high;
* divides - the boundaries between the basins of streams, each basin kept
  with its main stem: the crests (ridges and spurs) the graph walks along.
"""

from __future__ import annotations

import heapq
import logging
import os
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from numba import njit

from routing.grid import Grid

log = logging.getLogger(__name__)

# r.watershed drainage codes: k * 45 degrees counter-clockwise from east.
#             -   NE  N   NW  W   SW  S   SE  E
DROW = np.array([0, -1, -1, -1, 0, 1, 1, 1, 0], dtype=np.int64)
DCOL = np.array([0, 1, 0, -1, -1, -1, 0, 1, 1], dtype=np.int64)


@dataclass
class Hydrology:
    drain: np.ndarray  # int8 D8 code 1..8, 0 = none / out of the grid
    accum_km2: np.ndarray  # float32 catchment of the cell, km2

    def downstream(self) -> np.ndarray:
        """Flat index of each cell's downstream cell, -1 where water leaves."""
        h, w = self.drain.shape
        code = np.abs(self.drain.astype(np.int64)).ravel()
        code[code > 8] = 0
        rows, cols = np.divmod(np.arange(h * w), w)
        r = rows + DROW[code]
        c = cols + DCOL[code]
        inside = (code > 0) & (r >= 0) & (r < h) & (c >= 0) & (c < w)
        return np.where(inside, r * w + c, -1)


# ------------------------------------------------------------------ sources


def resolve_method(method: str) -> str:
    """``auto`` becomes grass when GRASS is installed, otherwise numpy.

    The DEM cache keys on this: the two methods do not trace the same
    directions. Bump ``routing.build.demcache.DEM_CACHE_VERSION`` when the
    directions or the accumulation of either method change."""
    if method == "auto":
        from otmlib import grass as otm_grass

        return "grass" if otm_grass.available() else "numpy"
    if method not in ("grass", "numpy"):
        raise ValueError(f"unknown hydrology {method}")
    return method


def compute(dem_tif: Path, grid: Grid, dem: np.ndarray, work_dir: Path, method: str,
            memory_mb: int = 2000) -> Hydrology:
    method = resolve_method(method)
    if method == "grass":
        return with_grass(dem_tif, grid, work_dir, memory_mb)
    if dem.size > 4_000_000:
        log.warning("numpy hydrology on %s cells will be slow; install GRASS", dem.size)
    return with_numpy(dem, grid)


def with_grass(dem_tif: Path, grid: Grid, work_dir: Path, memory_mb: int) -> Hydrology:
    from osgeo import gdal

    from otmlib import grass as otm_grass

    os.environ.setdefault("GRASS_MESSAGE_FORMAT", "plain")
    os.environ.setdefault("GRASS_VERBOSE", "0")
    gs = otm_grass.import_grass_script()
    acc_tif = work_dir / "accum.tif"
    dir_tif = work_dir / "drain.tif"
    # The throwaway project goes to local disk, not the work dir: on a data
    # volume Docker mounts from the host the files belong to another uid, and
    # GRASS refuses a mapset it does not own ("MAPSET PERMANENT - permission
    # denied").
    with tempfile.TemporaryDirectory(prefix="otm-grass-") as grass_dir, \
            otm_grass.dem_project(gs, dem_tif, Path(grass_dir), "routing"):
        log.info("r.watershed -s on %sx%s cells", grid.width, grid.height)
        # -a: accumulation stays positive where part of the catchment is off
        # the grid; -s: one downstream cell each, which the tracing relies on.
        gs.run_command("r.watershed", elevation="dem", accumulation="acc", drainage="dir",
                       memory=memory_mb, flags="as", overwrite=True)
        gs.run_command("r.out.gdal", input="acc", output=str(acc_tif.resolve()), format="GTiff",
                       type="Float64", flags="cf", overwrite=True)
        gs.run_command("r.out.gdal", input="dir", output=str(dir_tif.resolve()), format="GTiff",
                       type="Int16", flags="cf", overwrite=True)
    acc = gdal.Open(str(acc_tif)).ReadAsArray().astype(np.float64)
    drain = gdal.Open(str(dir_tif)).ReadAsArray().astype(np.int16)
    if acc.shape != grid.shape:
        raise RuntimeError(f"r.watershed returned {acc.shape}, expected {grid.shape}")
    acc = np.nan_to_num(np.abs(acc), nan=0.0)
    drain = np.where(np.abs(drain) <= 8, np.abs(drain), 0).astype(np.int8)
    return Hydrology(drain=drain, accum_km2=(acc * grid.cell_km2).astype(np.float32))


def with_numpy(dem: np.ndarray, grid: Grid) -> Hydrology:
    """Priority-flood (Barnes et al. 2014, with epsilon) then steepest descent.

    Pits are filled to their spill point plus a hair per cell, so every cell
    but the border has a strictly lower neighbour and water reaches the edge.
    """
    h, w = dem.shape
    z = np.where(np.isnan(dem), -np.inf, dem).astype(np.float64)
    filled = z.copy()
    closed = np.zeros((h, w), dtype=bool)
    heap: list[tuple[float, int, int]] = []
    eps = 1e-4
    for r in range(h):
        for c in range(w):
            if r in (0, h - 1) or c in (0, w - 1) or np.isneginf(z[r, c]):
                closed[r, c] = True
                heapq.heappush(heap, (filled[r, c], r, c))
    while heap:
        zc, r, c = heapq.heappop(heap)
        for k in range(1, 9):
            rr, cc = r + DROW[k], c + DCOL[k]
            if 0 <= rr < h and 0 <= cc < w and not closed[rr, cc]:
                closed[rr, cc] = True
                filled[rr, cc] = max(z[rr, cc], zc + eps)
                heapq.heappush(heap, (filled[rr, cc], rr, cc))

    drain = np.zeros((h, w), dtype=np.int8)
    best = np.zeros((h, w))
    for k in range(1, 9):
        dist = np.hypot(DROW[k], DCOL[k]) * grid.res
        shifted = np.full((h, w), np.inf)
        rs = slice(max(0, -DROW[k]), h - max(0, DROW[k]))
        cs = slice(max(0, -DCOL[k]), w - max(0, DCOL[k]))
        rd = slice(max(0, DROW[k]), h - max(0, -DROW[k]))
        cd = slice(max(0, DCOL[k]), w - max(0, -DCOL[k]))
        shifted[rs, cs] = filled[rd, cd]
        drop = (filled - shifted) / dist
        better = drop > best
        best[better] = drop[better]
        drain[better] = k
    nodata = np.isneginf(z)
    drain[nodata] = 0

    hyd = Hydrology(drain=drain, accum_km2=np.zeros((h, w), dtype=np.float32))
    down = hyd.downstream()
    order = np.argsort(-filled.ravel(), kind="stable")
    acc = np.where(nodata.ravel(), 0.0, 1.0)
    for i in order:
        d = down[i]
        if d >= 0:
            acc[d] += acc[i]
    hyd.accum_km2 = (acc.reshape(h, w) * grid.cell_km2).astype(np.float32)
    return hyd


# ----------------------------------------------------------------- thalwegs


def thalweg_chains(accum_km2: np.ndarray, down: np.ndarray, threshold_km2: float,
                   spacing_cells: int, must_keep: np.ndarray | None = None) -> list[np.ndarray]:
    """Valley-floor lines as runs of flat cell indices, upstream first.

    A run starts and ends at a kept cell: a source, a confluence, an outlet,
    a cell in *must_keep* (where a pass descent arrives) or every
    *spacing_cells* along the way, so consecutive runs share their end cells
    and the graph stays connected at confluences.
    """
    flat_acc = accum_km2.ravel()
    stream = flat_acc >= threshold_km2
    cells = np.flatnonzero(stream)
    if cells.size == 0:
        return []
    d = down[cells]
    ok = d >= 0
    ok[ok] = stream[d[ok]]
    nxt = np.full(flat_acc.size, -1, dtype=np.int64)
    nxt[cells[ok]] = d[ok]
    indeg = np.bincount(d[ok], minlength=flat_acc.size)

    keep = np.zeros(flat_acc.size, dtype=bool)
    keep[cells] = (indeg[cells] != 1) | (nxt[cells] < 0)
    if must_keep is not None:
        keep |= must_keep.ravel() & stream

    spacing = max(1, int(spacing_cells))
    chains: list[np.ndarray] = []
    heads = list(np.flatnonzero(keep))
    seen_head = set(heads)
    while heads:
        start = heads.pop()
        if nxt[start] < 0:
            continue
        run = [start]
        cur = start
        while True:
            cur = int(nxt[cur])
            run.append(cur)
            if keep[cur] or nxt[cur] < 0:
                break
            if len(run) - 1 >= spacing:
                keep[cur] = True
                if cur not in seen_head:
                    seen_head.add(cur)
                    heads.append(cur)
                break
            if len(run) > flat_acc.size:  # a loop in the pointers - never from r.watershed
                raise RuntimeError("cycle in the drainage directions")
        chains.append(np.asarray(run, dtype=np.int64))
    return chains


def clear_downstream(accum_km2: np.ndarray, down: np.ndarray, threshold_km2: float,
                     rock: np.ndarray, res: float, run_m: float) -> np.ndarray:
    """Stream cells whose channel runs at least *run_m* downstream without
    touching a *rock* cell (or leaves the grid first): a flat mask.

    Catchment grows strictly downstream, so walking the stream cells from the
    largest catchment down visits every cell after the one below it.
    """
    flat_acc = accum_km2.ravel()
    cells = np.flatnonzero(flat_acc >= threshold_km2)
    order = cells[np.argsort(-flat_acc[cells], kind="stable")]
    rock = rock.ravel()
    clear = np.zeros(flat_acc.size, dtype=np.float32)
    cap = np.float32(run_m)
    for c in order:
        if rock[c]:
            continue
        d = down[c]
        clear[c] = cap if d < 0 else min(cap, clear[d] + res)
    out = np.zeros(flat_acc.size, dtype=bool)
    out[cells] = clear[cells] >= cap
    return out


def fall_line(down: np.ndarray, start: int, stop: np.ndarray, max_cells: int) -> np.ndarray | None:
    """Flat cells from *start* down the D8 pointers to the first *stop* cell
    (inclusive), or None if water leaves the grid or runs too long first."""
    path = [start]
    cur = start
    for _ in range(max_cells):
        if stop[cur]:
            return np.asarray(path, dtype=np.int64)
        cur = int(down[cur])
        if cur < 0:
            return None
        path.append(cur)
    return None


# ------------------------------------------------------------------ canyons


def canyon_cells(dem: np.ndarray, slope_deg: np.ndarray, drain: np.ndarray, cells: np.ndarray,
                 res: float, wall_slope_deg: float, band_m: float, min_depth_m: float) -> np.ndarray:
    """Which of the thalweg *cells* (flat indices) lie at the bottom of a canyon.

    Both walls are sampled square to the flow, out to *band_m*: each must
    average at least *wall_slope_deg* and rise *min_depth_m* above the floor.
    One steep side is a valley under a cliff, not a canyon - it can still be
    walked along its foot.
    """
    if cells.size == 0:
        return np.zeros(0, dtype=bool)
    h, w = dem.shape
    rows, cols = np.divmod(cells, w)
    code = np.abs(drain.ravel()[cells].astype(np.int64))
    code[code > 8] = 0
    fr, fc = DROW[code], DCOL[code]
    # where water leaves the grid there is no direction: call it east
    fr = np.where(code == 0, 0, fr)
    fc = np.where(code == 0, 1, fc)
    step = np.hypot(fr, fc) * res
    nsteps = np.maximum(1, np.round(band_m / step).astype(np.int64))
    kmax = int(nsteps.max())
    floor = dem.ravel()[cells]

    def side(sign: int) -> tuple[np.ndarray, np.ndarray]:
        # perpendicular to (fr, fc) in grid space: (-fc, fr) or (fc, -fr)
        pr, pc = -fc * sign, fr * sign
        k = np.arange(1, kmax + 1)[None, :]
        rr = rows[:, None] + pr[:, None] * k
        cc = cols[:, None] + pc[:, None] * k
        valid = (k <= nsteps[:, None]) & (rr >= 0) & (rr < h) & (cc >= 0) & (cc < w)
        rr = np.clip(rr, 0, h - 1)
        cc = np.clip(cc, 0, w - 1)
        s = np.where(valid, slope_deg[rr, cc], np.nan)
        z = np.where(valid, dem[rr, cc], np.nan)
        with np.errstate(invalid="ignore"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN rows at the grid edge
            mean_slope = np.nanmean(s, axis=1)
            rise = np.nanmax(z, axis=1) - floor
        return np.nan_to_num(mean_slope, nan=0.0), np.nan_to_num(rise, nan=0.0)

    left_slope, left_rise = side(1)
    right_slope, right_rise = side(-1)
    return (
        (np.minimum(left_slope, right_slope) >= wall_slope_deg)
        & (np.minimum(left_rise, right_rise) >= min_depth_m)
    )


# ------------------------------------------------------------------ divides


@njit(cache=True)
def _basin_labels(order, down, stream, main_parent):
    lab = np.full(down.shape[0], -1, np.int64)
    nxt = 0
    for c in order:
        d = down[c]
        if d < 0 or lab[d] < 0 or (stream[c] and main_parent[d] != c):
            lab[c] = nxt
            nxt += 1
        else:
            lab[c] = lab[d]
    return lab


def basin_labels(accum_km2: np.ndarray, down: np.ndarray, threshold_km2: float) -> np.ndarray:
    """Each cell labelled with the stream it drains into: streams are the
    cells draining *threshold_km2* or more, and a stream keeps its label
    through a confluence along its main stem (the bigger branch), so a
    tributary is a basin of its own and the main valley one basin, both banks
    alike. A (h, w) int64 raster.

    Catchment grows strictly downstream, so walking the cells from the
    largest catchment down labels every cell after the one below it.
    """
    flat_acc = accum_km2.ravel()
    stream = flat_acc >= threshold_km2
    cells = np.flatnonzero(stream & (down >= 0))
    d = down[cells]
    main_parent = np.full(flat_acc.size, -1, dtype=np.int64)
    if cells.size:
        by = np.lexsort((flat_acc[cells], d))  # by downstream cell, biggest catchment last
        last = np.r_[d[by][1:] != d[by][:-1], True]
        main_parent[d[by][last]] = cells[by][last]
    order = np.argsort(-flat_acc, kind="stable")
    return _basin_labels(order, down, stream, main_parent).reshape(accum_km2.shape)


@njit(cache=True)
def _trace_divides(labels):
    """Corner chains along the label boundaries: vertical boundary segments
    vert[r, c] join corners (r, c)-(r + 1, c), horizontal horiz[r, c] join
    (r, c)-(r, c + 1); a chain runs between corners where other than two
    segments meet. Returns (corner ids, offsets), corner id = r * (w + 1) + c.
    A negative label (water) borders nothing: no divide runs along a shore or
    across a lake."""
    h, w = labels.shape
    vert = np.zeros((h, w + 1), np.uint8)
    horiz = np.zeros((h + 1, w), np.uint8)
    for r in range(h):
        for c in range(1, w):
            a, b = labels[r, c - 1], labels[r, c]
            if a != b and a >= 0 and b >= 0:
                vert[r, c] = 1
    for r in range(1, h):
        for c in range(w):
            a, b = labels[r - 1, c], labels[r, c]
            if a != b and a >= 0 and b >= 0:
                horiz[r, c] = 1
    deg = np.zeros((h + 1, w + 1), np.int8)
    for r in range(h):
        for c in range(w + 1):
            if vert[r, c]:
                deg[r, c] += 1
                deg[r + 1, c] += 1
    for r in range(h + 1):
        for c in range(w):
            if horiz[r, c]:
                deg[r, c] += 1
                deg[r, c + 1] += 1

    ids = []
    offs = [0]
    # starts: junctions and ends first, then whatever is left (closed rings)
    for ring_pass in range(2):
        for r0 in range(h + 1):
            for c0 in range(w + 1):
                if deg[r0, c0] == 0 or (ring_pass == 0 and deg[r0, c0] == 2):
                    continue
                while True:
                    # an unused segment leaving (r0, c0)
                    r, c = r0, c0
                    if r > 0 and vert[r - 1, c]:
                        vert[r - 1, c] = 0
                        nr, nc = r - 1, c
                    elif r < h and vert[r, c]:
                        vert[r, c] = 0
                        nr, nc = r + 1, c
                    elif c > 0 and horiz[r, c - 1]:
                        horiz[r, c - 1] = 0
                        nr, nc = r, c - 1
                    elif c < w and horiz[r, c]:
                        horiz[r, c] = 0
                        nr, nc = r, c + 1
                    else:
                        break
                    ids.append(r * (w + 1) + c)
                    r, c = nr, nc
                    while True:
                        ids.append(r * (w + 1) + c)
                        if deg[r, c] != 2 or (r == r0 and c == c0):
                            break
                        if r > 0 and vert[r - 1, c]:
                            vert[r - 1, c] = 0
                            r -= 1
                        elif r < h and vert[r, c]:
                            vert[r, c] = 0
                            r += 1
                        elif c > 0 and horiz[r, c - 1]:
                            horiz[r, c - 1] = 0
                            c -= 1
                        elif c < w and horiz[r, c]:
                            horiz[r, c] = 0
                            c += 1
                        else:
                            break
                    offs.append(len(ids))
    return np.array(ids, np.int64), np.array(offs, np.int64)


def divide_lines(labels: np.ndarray, grid: Grid) -> list[np.ndarray]:
    """The boundaries between *labels* as polylines in the grid's CRS, on the
    cell edges (a staircase: simplify before use), split where three or more
    basins meet. Cells labelled negative (water) border nothing."""
    ids, offs = _trace_divides(np.ascontiguousarray(labels))
    rows, cols = np.divmod(ids, labels.shape[1] + 1)
    x = grid.x0 + cols * grid.res
    y = grid.y0 - rows * grid.res
    xy = np.column_stack([x, y])
    return [xy[a:b] for a, b in zip(offs[:-1], offs[1:]) if b - a >= 2]
