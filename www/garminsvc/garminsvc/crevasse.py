"""Crevasse hatch: short ticks perpendicular to the host glacier's flow."""

from __future__ import annotations

import logging
import math
import random
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from shapely import STRtree, wkb
from shapely.geometry import LineString
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform
from shapely.validation import make_valid

from npyosmium import FileProcessor
from npyosmium.geom import WKBFactory

from garminsvc.osm_areas import tagged_subset

log = logging.getLogger(__name__)

STRIPE_SPACING_M = 40.0
MIN_STRIPE_M = 12.0
MIN_TICK_KEEP_M = 6.0
TICK_MIN_M = 10.0
TICK_MAX_M = 50.0
GAP_MIN_M = 10.0
GAP_MAX_M = 50.0
OFFSET_MAX_M = 12.0
TICK_MAX_DEG = 8.0
COVER_AREA_RATIO = 0.9
OVERLAP_AREA_RATIO = 0.5
M_PER_DEG_LAT = 111_320.0

# Compass azimuth in degrees, 0 = north, clockwise (Wikipedia 4/8/16/32-wind).
_CARDINAL_AZIMUTH: dict[str, float] = {
    "N": 0,
    "NORTH": 0,
    "NBE": 11.25,
    "NNE": 22.5,
    "NORTHNORTHEAST": 22.5,
    "NEBN": 33.75,
    "NE": 45,
    "NORTHEAST": 45,
    "NEBE": 56.25,
    "ENE": 67.5,
    "EASTNORTHEAST": 67.5,
    "EBN": 78.75,
    "E": 90,
    "EAST": 90,
    "EBS": 101.25,
    "ESE": 112.5,
    "EASTSOUTHEAST": 112.5,
    "SEBE": 123.75,
    "SE": 135,
    "SOUTHEAST": 135,
    "SEBS": 146.25,
    "SSE": 157.5,
    "SOUTHSOUTHEAST": 157.5,
    "SBE": 168.75,
    "S": 180,
    "SOUTH": 180,
    "SBW": 191.25,
    "SSW": 202.5,
    "SOUTHSOUTHWEST": 202.5,
    "SWBS": 213.75,
    "SW": 225,
    "SOUTHWEST": 225,
    "SWBW": 236.25,
    "WSW": 247.5,
    "WESTSOUTHWEST": 247.5,
    "WBS": 258.75,
    "W": 270,
    "WEST": 270,
    "WBN": 281.25,
    "WNW": 292.5,
    "WESTNORTHWEST": 292.5,
    "NWBW": 303.75,
    "NW": 315,
    "NORTHWEST": 315,
    "NWBN": 326.25,
    "NNW": 337.5,
    "NORTHNORTHWEST": 337.5,
    "NBW": 348.75,
}

_RANGE_RE = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*[-–/]\s*(-?\d+(?:\.\d+)?)\s*$",
)
_NUM_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*(?:°|deg)?\s*$", re.I)


def parse_direction(value: str | None) -> float | None:
    """Return compass azimuth in degrees, or None if the tag cannot be parsed."""
    if not value:
        return None
    raw = value.strip().split(";")[0].strip()
    if not raw:
        return None
    compact = re.sub(r"[\s._-]+", "", raw.upper())
    if compact in _CARDINAL_AZIMUTH:
        return _CARDINAL_AZIMUTH[compact]
    ranged = _RANGE_RE.match(raw.replace("°", ""))
    if ranged:
        a, b = float(ranged.group(1)), float(ranged.group(2))
        return ((a + b) / 2.0) % 360.0
    numbered = _NUM_RE.match(raw)
    if numbered:
        return float(numbered.group(1)) % 360.0
    return None


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


def _polygons(geom: BaseGeometry):
    if geom.is_empty:
        return
    if geom.geom_type == "Polygon":
        yield geom
    elif geom.geom_type == "MultiPolygon":
        yield from geom.geoms
    elif geom.geom_type == "GeometryCollection":
        for item in geom.geoms:
            yield from _polygons(item)


def _stripe_rng(azimuth: float, line: LineString, index: int) -> random.Random:
    x0, y0 = line.coords[0]
    seed = (
        int(round(azimuth * 10))
        ^ (index * 1_000_003)
        ^ (int(round(x0 * 100)) * 9_007_199)
        ^ (int(round(y0 * 100)) * 97)
        ^ (int(round(line.length * 10)) * 13)
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
    """10–50 m ticks along the guide, skewed a few degrees and offset slightly."""
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


def _guides(local: BaseGeometry, flow_az: float) -> list[LineString]:
    """Parallel lines across the projected polygon, perpendicular to glacier flow."""
    minx, miny, maxx, maxy = local.bounds
    span = math.hypot(maxx - minx, maxy - miny) + STRIPE_SPACING_M * 2
    stripe_az = math.radians((flow_az + 90.0) % 360.0)
    flow_az_r = math.radians(flow_az)
    se, sn = math.sin(stripe_az), math.cos(stripe_az)
    fe, fn = math.sin(flow_az_r), math.cos(flow_az_r)
    cx, cy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    n = int(span / STRIPE_SPACING_M) + 3
    lines: list[LineString] = []
    for i in range(-n, n + 1):
        ox, oy = cx + fe * i * STRIPE_SPACING_M, cy + fn * i * STRIPE_SPACING_M
        raw = LineString(
            [
                (ox - se * span, oy - sn * span),
                (ox + se * span, oy + sn * span),
            ]
        )
        clipped = raw.intersection(local)
        for part in _line_parts(clipped):
            if isinstance(part, LineString) and part.length >= MIN_STRIPE_M:
                lines.append(part)
    return lines


def _load_areas(pbf: Path, key: str, value: str) -> list[tuple[BaseGeometry, str | None]]:
    subset = tagged_subset(pbf, key, value)
    factory = WKBFactory()
    out: list[tuple[BaseGeometry, str | None]] = []
    failed = 0
    try:
        for obj in FileProcessor(str(subset)).with_areas().with_locations():
            if not obj.is_area() or obj.tags.get(key) != value:
                continue
            try:
                geom = wkb.loads(factory.create_multipolygon(obj), hex=True)
            except (RuntimeError, ValueError, TypeError):
                failed += 1
                continue
            if geom.is_empty:
                continue
            if not geom.is_valid:
                geom = make_valid(geom)
            if geom.is_empty:
                continue
            out.append((geom, obj.tags.get("direction")))
    finally:
        subset.unlink(missing_ok=True)
    if failed:
        log.warning("%s=%s: %s area(s) with broken geometry skipped", key, value, failed)
    return out


def _area_ratio(glacier: BaseGeometry, crevasse: BaseGeometry) -> float:
    if crevasse.area <= 0:
        return 0.0
    inter = glacier.intersection(crevasse)
    if inter.is_empty:
        return 0.0
    return inter.area / crevasse.area


def _host_glacier(
    crevasse: BaseGeometry,
    glaciers: list[tuple[BaseGeometry, float]],
    tree: STRtree,
) -> float | None:
    """Azimuth of the glacier that hosts *crevasse*, or None."""
    found = tree.query(crevasse, predicate="intersects")
    covering: list[tuple[BaseGeometry, float]] = []
    overlapping: list[tuple[float, float]] = []
    for i in found:
        glacier, azimuth = glaciers[int(i)]
        if glacier.covers(crevasse) or _area_ratio(glacier, crevasse) >= COVER_AREA_RATIO:
            covering.append((glacier, azimuth))
        else:
            overlapping.append((_area_ratio(glacier, crevasse), azimuth))
    if covering:
        covering.sort(key=lambda item: item[0].area)
        return covering[0][1]
    if not overlapping:
        return None
    overlapping.sort(reverse=True)
    ratio, azimuth = overlapping[0]
    if ratio >= OVERLAP_AREA_RATIO:
        return azimuth
    return None


def _hatch(crevasse: BaseGeometry, flow_az: float) -> list[tuple[LineString, int]]:
    lines: list[tuple[LineString, int]] = []
    for poly in _polygons(crevasse):
        lon0, lat0 = poly.centroid.x, poly.centroid.y
        local = _project_geom(poly, lon0, lat0)
        if local.is_empty:
            continue
        for index, guide in enumerate(_guides(local, flow_az)):
            rng = _stripe_rng(flow_az, guide, index)
            for tick, width in _ticks(guide, rng):
                kept = tick.intersection(local)
                for piece in _line_parts(kept):
                    if not isinstance(piece, LineString) or piece.length < MIN_TICK_KEEP_M:
                        continue
                    geo = _unproject_line(piece, lon0, lat0)
                    if not geo.is_empty:
                        lines.append((geo, width))
    return lines


def extract_crevasse_stripes(pbf: Path) -> list[tuple[LineString, int]]:
    crevasses = [geom for geom, _ in _load_areas(pbf, "natural", "crevasse")]
    if not crevasses:
        log.info("Crevasse hatch: no natural=crevasse areas")
        return []
    glaciers: list[tuple[BaseGeometry, float]] = []
    skipped_no_dir = 0
    for geom, raw_dir in _load_areas(pbf, "natural", "glacier"):
        azimuth = parse_direction(raw_dir)
        if azimuth is None:
            skipped_no_dir += 1
            continue
        glaciers.append((geom, azimuth))
    if not glaciers:
        log.info(
            "Crevasse hatch: no glacier with a usable direction tag (%s glaciers without one)",
            skipped_no_dir,
        )
        return []

    tree = STRtree([geom for geom, _ in glaciers])
    lines: list[tuple[LineString, int]] = []
    skipped_no_host = 0
    for crevasse in crevasses:
        azimuth = _host_glacier(crevasse, glaciers, tree)
        if azimuth is None:
            skipped_no_host += 1
            continue
        lines.extend(_hatch(crevasse, azimuth))
    log.info(
        "Crevasse hatch: %s glaciers, %s crevasse areas, %s ticks (no host: %s, glacier without direction: %s)",
        len(glaciers),
        len(crevasses),
        len(lines),
        skipped_no_host,
        skipped_no_dir,
    )
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


def build_crevasse_stripes(pbf: Path, output: Path) -> Path | None:
    lines = extract_crevasse_stripes(pbf)
    if not lines:
        output.unlink(missing_ok=True)
        return None
    write_crevasse_osm(output, lines)
    return output
