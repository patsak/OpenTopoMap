"""Assemble OSM areas (closed ways + multipolygon relations) into shapely geometry."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

from npyosmium import FileProcessor, io
from npyosmium.geom import WKBFactory
from npyosmium.osm import NOTHING, WAY
from shapely import STRtree, wkb
from shapely.geometry import LineString
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.prepared import prep
from shapely.validation import make_valid

from mapsvc.proc import run

log = logging.getLogger(__name__)

UNION_CACHE_MAX = 4096


def pbf_bbox(pbf: Path) -> tuple[float, float, float, float] | None:
    """Header bbox (west, south, east, north), without scanning the file."""
    try:
        reader = io.Reader(str(pbf), NOTHING)
    except (RuntimeError, OSError):
        return None
    try:
        box = reader.header().box()
        if not box.valid():
            return None
        bl, tr = box.bottom_left, box.top_right
        return bl.lon, bl.lat, tr.lon, tr.lat
    finally:
        reader.close()


def count_ways(pbf: Path, key: str | None = None, value: str | None = None) -> int:
    """Way count for progress totals, optionally only ways tagged key=value.

    Restricting entities lets osmium skip node blocks at the source, making this
    ~40x cheaper than a full read.
    """
    try:
        ways = FileProcessor(str(pbf), entities=WAY)
        if key is None:
            return sum(1 for _ in ways)
        return sum(1 for w in ways if w.tags.get(key) == value)
    except (RuntimeError, OSError):
        return 0


def _line_parts(geom: BaseGeometry):
    if geom is None or geom.is_empty:
        return
    if geom.geom_type == "LineString":
        yield geom
    elif geom.geom_type == "MultiLineString":
        yield from geom.geoms
    elif geom.geom_type == "GeometryCollection":
        for item in geom.geoms:
            yield from _line_parts(item)


def tagged_subset(pbf: Path, key: str, value: str) -> Path:
    """Tiny PBF with only objects tagged key=value; the caller owns the file.

    tags-filter also carries over referenced way nodes and relation members
    (that is its default; -R would drop them), so no second pass is needed.
    """
    osmium_bin = shutil.which("osmium")
    if not osmium_bin:
        raise RuntimeError("osmium not found; install osmium-tool")
    fd, name = tempfile.mkstemp(prefix=f"otm-{key}-{value}-", suffix=".osm.pbf")
    os.close(fd)
    tmp = Path(name)
    try:
        run(
            [
                osmium_bin,
                "tags-filter",
                str(pbf),
                f"nwr/{key}={value}",
                "-o",
                str(tmp),
                "--overwrite",
            ]
        )
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def load_area_geoms(
    pbf: Path,
    key: str,
    value: str,
    *,
    prefiltered: bool = False,
) -> list[BaseGeometry]:
    """Individual polygons tagged key=value (no union).

    With *prefiltered* the file is read as is, skipping the tags-filter pass —
    used when several processes share one subset built by their parent.
    """
    started = time.perf_counter()
    subset = pbf if prefiltered else tagged_subset(pbf, key, value)
    factory = WKBFactory()
    polys: list[BaseGeometry] = []
    failed = 0
    try:
        processor = FileProcessor(str(subset)).with_areas().with_locations()
        for obj in processor:
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
            if not geom.is_empty:
                polys.append(geom)
    finally:
        if not prefiltered:
            subset.unlink(missing_ok=True)
    if failed:
        log.warning("%s=%s: %s area(s) with broken geometry skipped", key, value, failed)
    log.info(
        "%s=%s areas: %s in %.1fs",
        key,
        value,
        len(polys),
        time.perf_counter() - started,
    )
    return polys


def load_area_mask(pbf: Path, key: str, value: str) -> BaseGeometry | None:
    """Union of all areas tagged key=value, or None if there are none."""
    polys = load_area_geoms(pbf, key, value)
    if not polys:
        return None
    mask = unary_union(polys)
    return None if mask.is_empty else mask


class AreaIndex:
    """Spatial index over area polygons, for clipping lines without a global union."""

    def __init__(self, geoms: list[BaseGeometry]) -> None:
        self._geoms = geoms
        self._tree = STRtree(geoms)
        self._prep = [prep(g) for g in geoms]
        # neighbouring contours hit the same polygon sets, so unions repeat a lot
        self._union_cache: dict[tuple[int, ...], BaseGeometry] = {}

    @classmethod
    def load(cls, pbf: Path, key: str, value: str, *, prefiltered: bool = False) -> AreaIndex | None:
        geoms = load_area_geoms(pbf, key, value, prefiltered=prefiltered)
        return cls(geoms) if geoms else None

    def _local_union(self, hits: tuple[int, ...]) -> BaseGeometry:
        if len(hits) == 1:
            return self._geoms[hits[0]]
        cached = self._union_cache.get(hits)
        if cached is None:
            cached = unary_union([self._geoms[i] for i in hits])
            if len(self._union_cache) < UNION_CACHE_MAX:
                self._union_cache[hits] = cached
        return cached

    def clip_line(self, line: LineString) -> tuple[list[LineString], list[LineString]]:
        """Split *line* into (inside, outside) relative to the indexed areas."""
        found = self._tree.query(line, predicate="intersects")
        if len(found) == 0:
            return [], [line]
        hits = tuple(sorted(int(i) for i in found))
        for i in hits:
            if self._prep[i].contains(line):
                return [line], []
        local = self._local_union(hits)
        ice = [
            p
            for p in _line_parts(line.intersection(local))
            if isinstance(p, LineString) and not p.is_empty
        ]
        land = [
            p
            for p in _line_parts(line.difference(local))
            if isinstance(p, LineString) and not p.is_empty
        ]
        return ice, land
