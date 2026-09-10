"""Ridge lines (khrebtovka, the Russian topo convention) for the contours overlay.

OsmAnd approach (see OsmAnd-resources default/topo.render.xml): ridges are NOT
generated from DEM. The app draws OSM ways tagged natural=ridge / natural=arete
(manually mapped crest lines). This module extracts those ways from an OSM PBF
and writes a lightweight OSM XML for the contours-hike overlay.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from pathlib import Path

from npyosmium import FileProcessor
from npyosmium.osm import NODE, WAY
from shapely.geometry import LineString

log = logging.getLogger(__name__)

RIDGE_TAGS = frozenset({"ridge", "arete"})
MIN_NODES = 2


def extract_osm_ridges(pbf: Path) -> list[tuple[LineString, dict[str, str]]]:
    out: list[tuple[LineString, dict[str, str]]] = []
    skipped_no_loc = 0
    for w in FileProcessor(str(pbf), entities=NODE | WAY).with_locations():
        if not w.is_way():
            continue
        nat = w.tags.get("natural")
        if nat not in RIDGE_TAGS:
            continue
        coords: list[tuple[float, float]] = []
        for n in w.nodes:
            if not n.location.valid():
                skipped_no_loc += 1
                coords = []
                break
            coords.append((n.location.lon, n.location.lat))
        if len(coords) < MIN_NODES:
            continue
        line = LineString(coords)
        if line.is_empty or line.length <= 0:
            continue
        tags = {"contour": "ridge", "natural": nat}
        # Moraine crests get their own symbol in the main map, so the overlay has
        # to know about them and skip drawing a generic ridge line on top.
        if w.tags.get("geological") == "moraine":
            tags["geological"] = "moraine"
        name = w.tags.get("name")
        if name:
            tags["name"] = name
        out.append((line, tags))
    log.info("OSM ridges: %s ways (skipped incomplete: %s)", len(out), skipped_no_loc)
    return out


def write_ridges_osm(path: Path, items: list[tuple[LineString, dict[str, str]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    osm = ET.Element("osm", version="0.6", generator="otm-ridges")
    node_id = 0
    way_id = 0
    for line, tags in items:
        refs: list[int] = []
        for lon, lat in line.coords:
            node_id -= 1
            refs.append(node_id)
            ET.SubElement(osm, "node", id=str(node_id), lat=f"{lat:.7f}", lon=f"{lon:.7f}")
        way_id -= 1
        way = ET.SubElement(osm, "way", id=str(way_id))
        for ref in refs:
            ET.SubElement(way, "nd", ref=str(ref))
        for k, v in tags.items():
            ET.SubElement(way, "tag", k=k, v=v)
    ET.ElementTree(osm).write(path, encoding="UTF-8", xml_declaration=True)


def build_ridges(pbf: Path, output: Path) -> Path | None:
    items = extract_osm_ridges(pbf)
    if not items:
        output.unlink(missing_ok=True)
        return None
    write_ridges_osm(output, items)
    log.info(
        "Ridges: %s ways, total length %.2f° → %s",
        len(items),
        sum(line.length for line, _ in items),
        output,
    )
    return output
