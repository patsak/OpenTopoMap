"""Everything the graph build needs from the area's OSM extract, in one pass.

Geometry stays in lon/lat here; the build projects it onto the working grid.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from npyosmium import FileProcessor
from npyosmium.geom import WKBFactory
from shapely import wkb
from shapely.geometry import LineString
from shapely.geometry.base import BaseGeometry
from shapely.validation import make_valid

from routing import model

log = logging.getLogger(__name__)

# Ways a hiker may walk. motorway/trunk only with an explicit foot=yes.
FOOT_HIGHWAYS = frozenset({
    "path", "footway", "track", "bridleway", "steps", "pedestrian", "cycleway",
    "living_street", "residential", "unclassified", "service", "road",
    "tertiary", "tertiary_link", "secondary", "secondary_link", "primary", "primary_link",
})
FOOT_YES = frozenset({"yes", "designated", "permissive", "official"})
FOOT_NO = frozenset({"no", "private", "use_sidepath"})
WATERWAYS = frozenset({"river", "stream", "canal"})
CANYON_NATURAL = frozenset({"gorge", "canyon"})
CANYON_VALLEY = frozenset({"canyon", "gorge"})
FORD_VALUES = frozenset({"yes", "stepping_stones"})

WAY_TAGS = ("highway", "sac_scale", "trail_visibility", "foot", "access", "ford", "bridge")


@dataclass
class Way:
    id: int
    nodes: np.ndarray  # int64 OSM node ids
    lon: np.ndarray
    lat: np.ndarray
    tags: dict[str, str]


@dataclass
class Point:
    id: int
    lon: float
    lat: float
    tags: dict[str, str] = field(default_factory=dict)


@dataclass
class OsmData:
    routable: list[Way] = field(default_factory=list)
    waterways: list[tuple[LineString, dict[str, str]]] = field(default_factory=list)
    barrier_lines: list[tuple[LineString, str]] = field(default_factory=list)
    canyon_lines: list[LineString] = field(default_factory=list)
    terrain_areas: dict[str, list[BaseGeometry]] = field(default_factory=dict)
    cliff_areas: list[BaseGeometry] = field(default_factory=list)
    canyon_areas: list[BaseGeometry] = field(default_factory=list)
    # natural=crevasse: crevasse fields (areas) and single crevasses (lines)
    crevasse_areas: list[BaseGeometry] = field(default_factory=list)
    crevasse_lines: list[LineString] = field(default_factory=list)
    passes: list[Point] = field(default_factory=list)
    fords: list[Point] = field(default_factory=list)
    waterfalls: list[Point] = field(default_factory=list)

    def summary(self) -> str:
        areas = ", ".join(f"{k} {len(v)}" for k, v in sorted(self.terrain_areas.items()))
        return (
            f"{len(self.routable)} routable ways, {len(self.waterways)} waterways, "
            f"{len(self.barrier_lines)} barrier lines, {len(self.passes)} passes, "
            f"{len(self.fords)} fords, {len(self.waterfalls)} waterfalls, "
            f"{len(self.cliff_areas)} cliff areas, "
            f"{len(self.crevasse_areas) + len(self.crevasse_lines)} crevasse fields, "
            f"{len(self.canyon_areas) + len(self.canyon_lines)} canyons; areas: {areas or 'none'}"
        )


def is_foot_routable(tags) -> bool:
    highway = tags.get("highway")
    if highway is None or tags.get("area") == "yes":
        return False
    foot = tags.get("foot")
    if foot in FOOT_NO:
        return False
    if highway in ("motorway", "motorway_link", "trunk", "trunk_link"):
        return foot in FOOT_YES
    if highway not in FOOT_HIGHWAYS:
        return False
    if tags.get("access") in ("no", "private") and foot not in FOOT_YES:
        return False
    return True


def is_canyon(tags) -> bool:
    natural = tags.get("natural")
    return natural in CANYON_NATURAL or (natural == "valley" and tags.get("valley") in CANYON_VALLEY)


def _way_coords(way) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    ids, lon, lat = [], [], []
    for n in way.nodes:
        if not n.location.valid():
            return None
        ids.append(n.ref)
        lon.append(n.location.lon)
        lat.append(n.location.lat)
    if len(ids) < 2:
        return None
    return np.asarray(ids, np.int64), np.asarray(lon), np.asarray(lat)


def read(pbf: Path, barrier_pairs: tuple[tuple[str, str], ...]) -> OsmData:
    data = OsmData()
    factory = WKBFactory()
    broken = 0
    for obj in FileProcessor(str(pbf)).with_areas().with_locations():
        tags = obj.tags
        if obj.is_node():
            if tags.get("mountain_pass") == "yes":
                data.passes.append(_point(obj, ("name", "ele", "rtsa_scale")))
            if tags.get("ford") in FORD_VALUES:
                data.fords.append(_point(obj, ("ford",)))
            if tags.get("waterway") == "waterfall":
                data.waterfalls.append(_point(obj, ()))
        elif obj.is_way():
            closed = obj.is_closed()
            wanted = (
                is_foot_routable(tags)
                or tags.get("waterway") in WATERWAYS
                or (not closed and is_canyon(tags))
                or (not closed and tags.get("natural") == "crevasse")
                or any(tags.get(k) == v for k, v in barrier_pairs)
            )
            if not wanted:
                continue
            coords = _way_coords(obj)
            if coords is None:
                continue
            ids, lon, lat = coords
            if is_foot_routable(tags):
                data.routable.append(
                    Way(obj.id, ids, lon, lat, {k: tags[k] for k in WAY_TAGS if k in tags})
                )
            line = LineString(np.column_stack([lon, lat]))
            if tags.get("waterway") in WATERWAYS:
                data.waterways.append((line, {k: tags[k] for k in ("waterway", "width", "name") if k in tags}))
            if not closed and is_canyon(tags):
                data.canyon_lines.append(line)
            if not closed and tags.get("natural") == "crevasse":
                data.crevasse_lines.append(line)
            for key, value in barrier_pairs:
                # a closed natural=cliff is an area, handled below
                if tags.get(key) == value and not (closed and key == "natural"):
                    data.barrier_lines.append((line, f"{key}={value}"))
        elif obj.is_area():
            terrain = model.terrain_for(tags)
            cliff = tags.get("natural") == "cliff"
            canyon = is_canyon(tags)
            crevasse = tags.get("natural") == "crevasse"
            if tags.get("water") in ("river", "canal") or tags.get("waterway") == "riverbank":
                terrain = "water"
            if terrain is None and not cliff and not canyon and not crevasse:
                continue
            try:
                geom = wkb.loads(factory.create_multipolygon(obj), hex=True)
            except (RuntimeError, ValueError, TypeError):
                broken += 1
                continue
            if geom.is_empty:
                continue
            if not geom.is_valid:
                geom = make_valid(geom)
            if terrain is not None:
                data.terrain_areas.setdefault(terrain, []).append(geom)
            if cliff:
                data.cliff_areas.append(geom)
            if crevasse:
                data.crevasse_areas.append(geom)
            if canyon:
                data.canyon_areas.append(geom)
    if broken:
        log.warning("%s area(s) with broken geometry skipped", broken)
    log.info("OSM: %s", data.summary())
    return data


def _point(obj, keys) -> Point:
    return Point(obj.id, obj.location.lon, obj.location.lat,
                 {k: obj.tags[k] for k in keys if k in obj.tags})


def parse_width(value: str | None) -> float | None:
    """OSM width in metres; '5 m', '5', '5.5' - anything else is unknown."""
    if not value:
        return None
    text = value.strip().lower().replace(",", ".")
    for suffix in ("m", "meters", "metres"):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
    try:
        width = float(text)
    except ValueError:
        return None
    return width if width > 0 else None
