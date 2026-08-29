"""Crevasse hatch: short ticks along DEM contours, slightly askew."""

from __future__ import annotations

import logging
import math
import random
import xml.etree.ElementTree as ET
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from shapely import wkb
from shapely.geometry import LineString
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform
from shapely.prepared import prep

from npyosmium import FileProcessor

from mapsvc.osm_areas import count_ways, load_area_mask, pbf_bbox
from mapsvc.proc import check_cancelled, worker_count
from mapsvc.progress import Progress

log = logging.getLogger(__name__)

CANCEL_EVERY = 4000
MIN_STRIPE_M = 12.0
MIN_TICK_KEEP_M = 6.0
TICK_MIN_M = 10.0
TICK_MAX_M = 50.0
GAP_MIN_M = 10.0
GAP_MAX_M = 50.0
OFFSET_MAX_M = 20.0
TICK_MAX_DEG = 20.0
M_PER_DEG_LAT = 111_320.0
JOBS_ENV = "OTM_CREVASSE_JOBS"


def _way_coords(w) -> list[tuple[float, float]] | None:
    coords: list[tuple[float, float]] = []
    for node in w.nodes:
        if not node.location.valid():
            return None
        coords.append((node.location.lon, node.location.lat))
    return coords


def _to_xy(lon: float, lat: float, lon0: float, lat0: float) -> tuple[float, float]:
    m_lon = M_PER_DEG_LAT * math.cos(math.radians(lat0))
    return ((lon - lon0) * m_lon, (lat - lat0) * M_PER_DEG_LAT)


def _to_lonlat(x: float, y: float, lon0: float, lat0: float) -> tuple[float, float]:
    m_lon = M_PER_DEG_LAT * math.cos(math.radians(lat0))
    if abs(m_lon) < 1e-6:
        m_lon = 1e-6
    return (lon0 + x / m_lon, lat0 + y / M_PER_DEG_LAT)


def _project_geom(geom: BaseGeometry, lon0: float, lat0: float) -> BaseGeometry:
    return transform(lambda lon, lat, z=None: _to_xy(lon, lat, lon0, lat0), geom)


def _unproject_line(line: LineString, lon0: float, lat0: float) -> LineString:
    return LineString([_to_lonlat(x, y, lon0, lat0) for x, y in line.coords])


def _line_parts(geom: BaseGeometry):
    if geom.is_empty:
        return
    if geom.geom_type == "LineString":
        yield geom
    elif geom.geom_type == "MultiLineString":
        yield from geom.geoms
    elif geom.geom_type == "GeometryCollection":
        for item in geom.geoms:
            yield from _line_parts(item)


def _contour_rng(ele: float, line: LineString) -> random.Random:
    x0, y0 = line.coords[0]
    seed = (
        int(round(ele * 10))
        ^ (int(round(x0 * 100)) * 1_000_003)
        ^ (int(round(y0 * 100)) * 9_007_199)
        ^ (int(round(line.length * 10)) * 97)
    )
    return random.Random(seed & 0xFFFFFFFF)


def _point_tangent(line: LineString, dist: float) -> tuple[float, float, float, float] | None:
    """Return (x, y, ux, uy) at `dist` metres along a projected line."""
    length = line.length
    if length < 1.0:
        return None
    d0 = min(max(dist, 0.0), length)
    delta = min(1.0, length * 0.5)
    d1 = d0 + delta if d0 + delta <= length else d0 - delta
    a = line.interpolate(d0)
    b = line.interpolate(d1)
    dx, dy = b.x - a.x, b.y - a.y
    n = math.hypot(dx, dy)
    if n < 1e-6:
        return None
    if d1 < d0:
        dx, dy = -dx, -dy
    return a.x, a.y, dx / n, dy / n


def _ticks(line: LineString, rng: random.Random) -> list[tuple[LineString, int]]:
    """10–50 m ticks along the contour, skewed 0–20° and offset 0–20 m."""
    if line.length < MIN_STRIPE_M:
        return []
    out: list[tuple[LineString, int]] = []
    cursor = rng.uniform(0.0, GAP_MAX_M)
    while cursor < line.length:
        length = rng.uniform(TICK_MIN_M, TICK_MAX_M)
        center = cursor + length / 2.0
        cursor += length + rng.uniform(GAP_MIN_M, GAP_MAX_M)
        if center >= line.length:
            break
        tangent = _point_tangent(line, center)
        if tangent is None:
            continue
        x, y, ux, uy = tangent
        angle = math.radians(rng.uniform(0.0, TICK_MAX_DEG) * rng.choice((-1, 1)))
        ca, sa = math.cos(angle), math.sin(angle)
        rx = ux * ca - uy * sa
        ry = ux * sa + uy * ca
        nx, ny = -uy, ux
        off = rng.uniform(0.0, OFFSET_MAX_M) * rng.choice((-1, 1))
        x += nx * off
        y += ny * off
        half = length / 2.0
        tick = LineString(
            [
                (x - rx * half, y - ry * half),
                (x + rx * half, y + ry * half),
            ]
        )
        width = rng.choice((1, 2))
        out.append((tick, width))
    return out


def _bbox_hits(pbf: Path, mask: BaseGeometry) -> bool:
    """True unless the file's header bbox is disjoint from *mask*."""
    box = pbf_bbox(pbf)
    if box is None:
        return True
    west, south, east, north = box
    m_west, m_south, m_east, m_north = mask.bounds
    return not (east < m_west or west > m_east or north < m_south or south > m_north)


def _iter_contours(path: Path):
    """Stream (line, ele) from a contour PBF instead of holding them all in memory."""
    for obj in FileProcessor(str(path)).with_locations():
        if not obj.is_way() or obj.tags.get("contour") != "elevation":
            continue
        coords = _way_coords(obj)
        if coords is None or len(coords) < 2:
            continue
        try:
            ele = float(obj.tags.get("ele") or 0)
        except ValueError:
            ele = 0.0
        line = LineString(coords)
        if line.is_empty or line.length <= 0:
            continue
        yield line, ele


class _MaskCtx:
    """Crevasse mask plus its projected twin; prepared geometry cannot be pickled,
    so each process builds this from the mask WKB."""

    def __init__(self, mask: BaseGeometry) -> None:
        self.mask = mask
        self.prepared = prep(mask)
        self.lon0, self.lat0 = mask.centroid.x, mask.centroid.y
        self.mask_xy = _project_geom(mask, self.lon0, self.lat0)


def _hatch_one_file(
    path: Path,
    ctx: _MaskCtx,
    progress: Progress | None,
) -> tuple[int, int, list[tuple[LineString, int]]]:
    lines: list[tuple[LineString, int]] = []
    seen = 0
    used = 0
    for contour, ele in _iter_contours(path):
        seen += 1
        if progress is not None:
            progress.advance()
        if seen % CANCEL_EVERY == 0:
            check_cancelled()
        if not ctx.prepared.intersects(contour):
            continue
        clipped = contour.intersection(ctx.mask)
        if clipped.is_empty:
            continue
        used += 1
        for part in _line_parts(clipped):
            if not isinstance(part, LineString) or part.is_empty:
                continue
            local = _project_geom(part, ctx.lon0, ctx.lat0)
            if not isinstance(local, LineString) or local.length < MIN_STRIPE_M:
                continue
            rng = _contour_rng(ele, local)
            for tick, width in _ticks(local, rng):
                kept = tick.intersection(ctx.mask_xy)
                for piece in _line_parts(kept):
                    if not isinstance(piece, LineString) or piece.length < MIN_TICK_KEEP_M:
                        continue
                    geo = _unproject_line(piece, ctx.lon0, ctx.lat0)
                    if not geo.is_empty:
                        lines.append((geo, width))
    return seen, used, lines


_WORKER: dict[str, _MaskCtx] = {}


def _init_hatch_worker(mask_wkb: bytes) -> None:
    _WORKER["ctx"] = _MaskCtx(wkb.loads(mask_wkb))


def _run_hatch_worker(path: str) -> tuple[int, int, list[tuple[bytes, int]]]:
    seen, used, lines = _hatch_one_file(Path(path), _WORKER["ctx"], progress=None)
    return seen, used, [(line.wkb, width) for line, width in lines]


def _hatch_parallel(
    relevant: list[Path],
    mask: BaseGeometry,
    jobs: int,
) -> tuple[int, int, list[tuple[LineString, int]]]:
    log.info("Crevasse hatch: %s files on %s workers", len(relevant), jobs)
    # results are kept in file order so the output does not depend on completion order
    per_file: list[list[tuple[LineString, int]]] = [[] for _ in relevant]
    seen = 0
    used = 0
    pool = ProcessPoolExecutor(
        max_workers=jobs,
        initializer=_init_hatch_worker,
        initargs=(mask.wkb,),
    )
    try:
        pending = {pool.submit(_run_hatch_worker, str(p)): i for i, p in enumerate(relevant)}
        done = 0
        for future in as_completed(pending):
            index = pending[future]
            file_seen, file_used, blobs = future.result()
            seen += file_seen
            used += file_used
            per_file[index] = [(wkb.loads(blob), width) for blob, width in blobs]
            done += 1
            log.info(
                "Crevasse hatch %s/%s %s: %s ticks",
                done,
                len(relevant),
                relevant[index].name,
                len(per_file[index]),
            )
            check_cancelled()
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown()
    return seen, used, [item for chunk in per_file for item in chunk]


def extract_crevasse_stripes(pbf: Path, contour_pbfs: list[Path]) -> list[tuple[LineString, int]]:
    mask = load_area_mask(pbf, "natural", "crevasse")
    if mask is None:
        log.info("Crevasse hatch: no natural=crevasse areas")
        return []
    # crevasse areas are tiny; skip contour files that cannot touch them at all
    relevant = [p for p in contour_pbfs if _bbox_hits(p, mask)]
    if not relevant:
        log.info("Crevasse hatch: no contour file overlaps the crevasse areas")
        return []
    if len(relevant) < len(contour_pbfs):
        log.info(
            "Crevasse hatch: %s of %s contour files overlap crevasse areas",
            len(relevant),
            len(contour_pbfs),
        )

    jobs = worker_count(len(relevant), JOBS_ENV)
    if jobs > 1:
        seen, used, lines = _hatch_parallel(relevant, mask, jobs)
    else:
        ctx = _MaskCtx(mask)
        total = sum(count_ways(p, "contour", "elevation") for p in relevant)
        progress = Progress("Crevasse hatch", total=total)
        seen = used = 0
        lines = []
        for path in relevant:
            file_seen, file_used, file_lines = _hatch_one_file(path, ctx, progress)
            seen += file_seen
            used += file_used
            lines.extend(file_lines)
        progress.finish(f"{used} contour ways hit, {len(lines)} ticks")
    if seen == 0:
        log.warning("Crevasse hatch: contour PBF has no elevation lines")
        return []
    log.info("Crevasse hatch: %s contour ways hit, %s ticks", used, len(lines))
    return lines


def write_crevasse_osm(path: Path, lines: list[tuple[LineString, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    osm = ET.Element("osm", version="0.6", generator="otm-crevasse-stripes")
    node_id = 0
    way_id = 0
    for line, width in lines:
        refs: list[int] = []
        for lon, lat in line.coords:
            node_id -= 1
            refs.append(node_id)
            ET.SubElement(
                osm,
                "node",
                id=str(node_id),
                lat=f"{lat:.7f}",
                lon=f"{lon:.7f}",
            )
        way_id -= 1
        way = ET.SubElement(osm, "way", id=str(way_id))
        for ref in refs:
            ET.SubElement(way, "nd", ref=str(ref))
        ET.SubElement(way, "tag", k="crevasse", v="stripe2" if width == 2 else "stripe")
        ET.SubElement(way, "tag", k="natural", v="crevasse")
    tree = ET.ElementTree(osm)
    tree.write(path, encoding="UTF-8", xml_declaration=True)


def build_crevasse_stripes(pbf: Path, contour_pbfs: list[Path], output: Path) -> Path | None:
    lines = extract_crevasse_stripes(pbf, contour_pbfs)
    if not lines:
        output.unlink(missing_ok=True)
        return None
    write_crevasse_osm(output, lines)
    return output
