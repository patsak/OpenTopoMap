"""Tag contour PBF: blue on glaciers, thin majors on slopes steeper than 50°."""

from __future__ import annotations

import logging
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from osgeo import gdal
from shapely.geometry import LineString

import npyosmium as osmium
from npyosmium.osm.mutable import Node, Way

from garminsvc.osm_areas import AreaIndex, count_ways, pbf_bbox, tagged_subset
from garminsvc.proc import check_cancelled, worker_count
from garminsvc.progress import Progress

log = logging.getLogger(__name__)

STEEP_DEG = 50.0
M_PER_DEG_LAT = 111_320.0
HGT_VOID = -32768
CANCEL_EVERY = 4000
# every file gets its own id slot, so files stay unique without a shared counter
ID_SLOT = 200_000_000
JOBS_ENV = "OTM_CONTOUR_JOBS"


def _way_coords(w) -> list[tuple[float, float]] | None:
    coords: list[tuple[float, float]] = []
    for node in w.nodes:
        if not node.location.valid():
            return None
        coords.append((node.location.lon, node.location.lat))
    return coords


class SlopeSampler:
    def __init__(self, paths: list[Path]) -> None:
        self._bands: list[tuple[np.ndarray, tuple]] = []
        for path in paths:
            ds = gdal.Open(str(path))
            if ds is None:
                continue
            band = ds.GetRasterBand(1)
            # float32 keeps ~7 digits, far more than metre-accurate elevations need
            arr = band.ReadAsArray().astype(np.float32)
            nodata = band.GetNoDataValue()
            if nodata is not None:
                arr[arr == nodata] = np.nan
            arr[arr == HGT_VOID] = np.nan
            gt = ds.GetGeoTransform()
            ds = None
            lats = gt[3] + np.arange(arr.shape[0]) * gt[5]
            dx_m = np.maximum(np.abs(gt[1]) * M_PER_DEG_LAT * np.cos(np.radians(lats)), 1e-6)
            dy_m = max(abs(gt[5]) * M_PER_DEG_LAT, 1e-6)
            dlat, dlon = np.gradient(arr)
            slope = np.degrees(np.arctan(np.hypot(dlon / dx_m[:, None], dlat / dy_m)))
            slope[~np.isfinite(arr)] = np.nan
            self._bands.append((slope.astype(np.float32), gt))
        log.info("Slope sampler: %s DEM rasters", len(self._bands))

    def ready(self) -> bool:
        return bool(self._bands)

    def slope_deg(self, lon: float, lat: float) -> float | None:
        for slp, gt in self._bands:
            col = (lon - gt[0]) / gt[1]
            row = (lat - gt[3]) / gt[5]
            r, c = int(round(row)), int(round(col))
            if r < 0 or c < 0 or r >= slp.shape[0] or c >= slp.shape[1]:
                continue
            v = slp[r, c]
            if np.isfinite(v):
                return float(v)
        return None


def _split_steep(line: LineString, sampler: SlopeSampler | None) -> list[tuple[LineString, bool]]:
    coords = list(line.coords)
    if len(coords) < 2 or sampler is None:
        return [(line, False)]
    flags = []
    for lon, lat in coords:
        s = sampler.slope_deg(lon, lat)
        flags.append(s is not None and s > STEEP_DEG)
    if not any(flags):
        return [(line, False)]
    if all(flags):
        return [(line, True)]
    out: list[tuple[LineString, bool]] = []
    start = 0
    current = flags[0]
    for i in range(1, len(coords)):
        if flags[i] == current:
            continue
        chunk = coords[start : i + 1]
        if len(chunk) >= 2:
            out.append((LineString(chunk), current))
        start = i
        current = flags[i]
    chunk = coords[start:]
    if len(chunk) >= 2:
        out.append((LineString(chunk), current))
    return out or [(line, False)]


def _emit_parts(
    line: LineString,
    base: dict[str, str],
    glacier: bool,
    sampler: SlopeSampler | None,
    out: list[tuple[LineString, dict[str, str]]],
) -> None:
    major = base.get("contour_ext") == "elevation_major"
    pieces = _split_steep(line, sampler) if major else [(line, False)]
    for part, steep in pieces:
        tags = dict(base)
        if glacier:
            tags["glacier"] = "yes"
        if steep:
            tags["steep"] = "yes"
        out.append((part, tags))


class _ContourRewriter(osmium.SimpleHandler):
    def __init__(
        self,
        writer: osmium.SimpleWriter,
        glaciers: AreaIndex | None,
        sampler: SlopeSampler | None,
        progress: Progress | None,
        first_id: int = 1,
    ) -> None:
        super().__init__()
        self._writer = writer
        self._glaciers = glaciers
        self._sampler = sampler
        self._progress = progress
        self.node_id = first_id - 1
        self.way_id = first_id - 1
        self.seen = 0
        self.emitted = 0
        self.ice_n = 0
        self.steep_n = 0

    def _write(self, line: LineString, tags: dict[str, str]) -> None:
        refs: list[int] = []
        for lon, lat in line.coords:
            self.node_id += 1
            refs.append(self.node_id)
            self._writer.add_node(Node(id=self.node_id, location=(lon, lat)))
        self.way_id += 1
        self._writer.add_way(Way(id=self.way_id, nodes=refs, tags=tags))
        self.emitted += 1
        if tags.get("glacier") == "yes":
            self.ice_n += 1
        if tags.get("steep") == "yes":
            self.steep_n += 1

    def way(self, w) -> None:
        if w.tags.get("contour") != "elevation":
            return
        coords = _way_coords(w)
        if coords is None or len(coords) < 2:
            return
        line = LineString(coords)
        if line.is_empty or line.length <= 0:
            return
        tags = {tag.k: tag.v for tag in w.tags}
        self.seen += 1
        if self._progress is not None:
            self._progress.advance()
        if self.seen % CANCEL_EVERY == 0:
            check_cancelled()
        parts: list[tuple[LineString, dict[str, str]]] = []
        if self._glaciers is None:
            _emit_parts(line, tags, glacier=False, sampler=self._sampler, out=parts)
        else:
            ice, land = self._glaciers.clip_line(line)
            if not ice and not land:
                land = [line]
            for part in ice:
                _emit_parts(part, tags, glacier=True, sampler=self._sampler, out=parts)
            for part in land:
                _emit_parts(part, tags, glacier=False, sampler=self._sampler, out=parts)
        for part, part_tags in parts:
            self._write(part, part_tags)


def _header_like(pbf: Path) -> osmium.io.Header | None:
    """Carry the source bbox into the rewritten file; later steps use it to skip
    files that cannot matter."""
    box = pbf_bbox(pbf)
    if box is None:
        return None
    west, south, east, north = box
    header = osmium.io.Header()
    header.add_box(osmium.osm.Box(west, south, east, north))
    return header


@dataclass
class _FileStats:
    """Per-file result, passed back from worker processes."""

    name: str
    seen: int
    emitted: int
    ice: int
    steep: int
    max_id: int


def _rewrite_file(
    pbf: Path,
    first_id: int,
    glaciers: AreaIndex | None,
    sampler: SlopeSampler | None,
    progress: Progress | None,
) -> _FileStats:
    tmp = pbf.with_name(pbf.stem + ".post.osm.pbf")
    tmp.unlink(missing_ok=True)
    writer = osmium.SimpleWriter(str(tmp), header=_header_like(pbf))
    try:
        handler = _ContourRewriter(writer, glaciers, sampler, progress, first_id)
        handler.apply_file(str(pbf), locations=True, idx="flex_mem")
    except BaseException:
        writer.close()
        tmp.unlink(missing_ok=True)
        raise
    writer.close()
    tmp.replace(pbf)
    return _FileStats(
        name=pbf.name,
        seen=handler.seen,
        emitted=handler.emitted,
        ice=handler.ice_n,
        steep=handler.steep_n,
        max_id=max(handler.node_id, handler.way_id),
    )


_WORKER: dict[str, object] = {}


def _init_worker(glacier_subset: str | None, dem_files: list[str]) -> None:
    """Each worker builds its own glacier index and slope rasters (~2s, few hundred MB).

    The glacier subset PBF is prepared once by the parent, so workers do not
    re-filter the whole region file.
    """
    glaciers = None
    if glacier_subset:
        glaciers = AreaIndex.load(Path(glacier_subset), "natural", "glacier", prefiltered=True)
    sampler = SlopeSampler([Path(p) for p in dem_files]) if dem_files else None
    _WORKER["glaciers"] = glaciers
    _WORKER["sampler"] = sampler if sampler and sampler.ready() else None


def _run_worker(pbf: str, first_id: int) -> _FileStats:
    return _rewrite_file(
        Path(pbf),
        first_id,
        _WORKER.get("glaciers"),  # type: ignore[arg-type]
        _WORKER.get("sampler"),  # type: ignore[arg-type]
        progress=None,
    )


def _check_slot(stats: _FileStats, first_id: int) -> None:
    if stats.max_id >= first_id + ID_SLOT:
        raise RuntimeError(
            f"{stats.name}: ids overflowed its {ID_SLOT} slot "
            f"({stats.max_id - first_id + 1} used); raise ID_SLOT"
        )


def postprocess_contour_pbfs(
    contour_pbfs: list[Path],
    osm_pbf: Path | None,
    dem_files: list[Path],
) -> None:
    if not contour_pbfs:
        return
    src = osm_pbf if osm_pbf is not None and osm_pbf.is_file() else None
    subset = tagged_subset(src, "natural", "glacier") if src else None
    try:
        glaciers = (
            AreaIndex.load(subset, "natural", "glacier", prefiltered=True) if subset else None
        )
        sampler = SlopeSampler(dem_files) if dem_files else None
        if glaciers is None and (sampler is None or not sampler.ready()):
            log.info("Contour postprocess: nothing to do (no glaciers, no DEM)")
            return
        ready_sampler = sampler if sampler and sampler.ready() else None
        slots = [1 + i * ID_SLOT for i in range(len(contour_pbfs))]

        jobs = worker_count(len(contour_pbfs), JOBS_ENV)
        if jobs > 1:
            _postprocess_parallel(contour_pbfs, slots, subset, dem_files, jobs)
            return

        for pbf, first_id in zip(contour_pbfs, slots):
            check_cancelled()
            progress = Progress(
                f"Contour postprocess {pbf.name}",
                total=count_ways(pbf, "contour", "elevation"),
            )
            stats = _rewrite_file(pbf, first_id, glaciers, ready_sampler, progress)
            _check_slot(stats, first_id)
            progress.finish(f"→ {stats.emitted} ways (glacier={stats.ice} steep={stats.steep})")
    finally:
        if subset is not None:
            subset.unlink(missing_ok=True)


def _postprocess_parallel(
    contour_pbfs: list[Path],
    slots: list[int],
    glacier_subset: Path | None,
    dem_files: list[Path],
    jobs: int,
) -> None:
    log.info("Contour postprocess: %s files on %s workers", len(contour_pbfs), jobs)
    progress = Progress("Contour postprocess", total=len(contour_pbfs))
    args = (str(glacier_subset) if glacier_subset else None, [str(p) for p in dem_files])
    pool = ProcessPoolExecutor(max_workers=jobs, initializer=_init_worker, initargs=args)
    try:
        pending = {
            pool.submit(_run_worker, str(pbf), first_id): (pbf, first_id)
            for pbf, first_id in zip(contour_pbfs, slots)
        }
        done = 0
        for future in as_completed(pending):
            pbf, first_id = pending[future]
            stats = future.result()
            _check_slot(stats, first_id)
            done += 1
            progress.advance()
            log.info(
                "Contour postprocess %s/%s %s: %s ways (glacier=%s steep=%s)",
                done,
                len(contour_pbfs),
                stats.name,
                stats.emitted,
                stats.ice,
                stats.steep,
            )
            check_cancelled()
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown()
    progress.finish()
