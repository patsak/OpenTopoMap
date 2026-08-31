"""Real OSM XML fixtures for crevasse hatch tests — not mocks."""

from __future__ import annotations

from pathlib import Path

# ~800 m glacier around 43.30°N 42.40°E (Elbrus-ish), crevasse ~400 m inset.
_GLACIER = [
    (42.4000, 43.3000),
    (42.4100, 43.3000),
    (42.4100, 43.3070),
    (42.4000, 43.3070),
]
_CREVASSE_INSIDE = [
    (42.4025, 43.3018),
    (42.4075, 43.3018),
    (42.4075, 43.3052),
    (42.4025, 43.3052),
]
_CREVASSE_OUTSIDE = [
    (42.4120, 43.3018),
    (42.4170, 43.3018),
    (42.4170, 43.3052),
    (42.4120, 43.3052),
]


def _ring_nodes(start_id: int, ring: list[tuple[float, float]]) -> tuple[str, list[int]]:
    nodes = []
    refs: list[int] = []
    for i, (lon, lat) in enumerate(ring):
        nid = start_id + i
        refs.append(nid)
        nodes.append(f'  <node id="{nid}" lat="{lat:.7f}" lon="{lon:.7f}"/>')
    refs.append(refs[0])
    return "\n".join(nodes), refs


def _way(way_id: int, refs: list[int], tags: dict[str, str]) -> str:
    nd = "\n".join(f'    <nd ref="{ref}"/>' for ref in refs)
    tag = "\n".join(f'    <tag k="{k}" v="{v}"/>' for k, v in tags.items())
    return f'  <way id="{way_id}">\n{nd}\n{tag}\n  </way>'


def glacierScene(
    path: Path,
    *,
    direction: str | None = "E",
    crevasse: str | None = "inside",
) -> Path:
    """Write a tiny OSM file: glacier plus optional crevasse.

    *crevasse*: ``"inside"``, ``"outside"``, or ``None`` to omit the crevasse.
    """
    chunks = ['<?xml version="1.0" encoding="UTF-8"?>', '<osm version="0.6" generator="crevasseaide">']
    g_nodes, g_refs = _ring_nodes(1, _GLACIER)
    chunks.append(g_nodes)
    glacier_tags = {"natural": "glacier"}
    if direction is not None:
        glacier_tags["direction"] = direction
    chunks.append(_way(10, g_refs, glacier_tags))
    if crevasse == "inside":
        c_nodes, c_refs = _ring_nodes(20, _CREVASSE_INSIDE)
        chunks.append(c_nodes)
        chunks.append(_way(11, c_refs, {"natural": "crevasse"}))
    elif crevasse == "outside":
        c_nodes, c_refs = _ring_nodes(20, _CREVASSE_OUTSIDE)
        chunks.append(c_nodes)
        chunks.append(_way(11, c_refs, {"natural": "crevasse"}))
    chunks.append("</osm>")
    path.write_text("\n".join(chunks) + "\n", encoding="utf-8")
    return path
