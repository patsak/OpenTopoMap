"""The response body: Overpass JSON, OSM XML or CSV.

Field names, nesting and order follow what overpass-api.de sends, because the
clients (overpass-turbo, osmtogeojson, JOSM, scripts written against the real
thing) parse it rather than read it. What a GOL cannot supply is left out
rather than invented: there is no version, timestamp, changeset or user, so
``out meta`` prints what ``out body`` does.
"""

from __future__ import annotations

import json
from xml.sax.saxutils import quoteattr

from overpass.evaluator import Element, OutBlock, Result
from overpass.syntax import Out, Settings

GENERATOR = "Overpass API 0.7.62 (OpenTopoMap GeoDesk prototype)"
COPYRIGHT = (
    "The data included in this document is from www.openstreetmap.org. "
    "The data is made available under ODbL."
)

MIMETYPES = {
    "json": "application/json",
    "xml": "application/osm3s+xml",
    "csv": "text/csv",
}

# Verbosities that print coordinates, way nodes and relation members.
_STRUCTURE = ("skel", "body", "meta")
_TAGS = ("body", "tags", "meta")
_CSV_TYPE_CODES = {"node": 1, "way": 2, "relation": 3, "area": 4}


def render(settings: Settings, result: Result, *, timestamp: str) -> tuple[str, str]:
    fmt = settings.output
    if fmt == "json":
        body = _json(result, timestamp)
    elif fmt == "csv":
        body = _csv(settings, result)
    else:
        body = _xml(result, timestamp)
    return body, MIMETYPES[fmt]


# --- shared pieces --------------------------------------------------------------


def _coord(value: float) -> float:
    # A GOL keeps coordinates as 32-bit Mercator integers; seven decimals is
    # what OSM stores and what Overpass prints.
    return round(value, 7)


def _bounds(feature) -> dict[str, float]:
    b = feature.bounds
    return {
        "minlat": _coord(b.south),
        "minlon": _coord(b.west),
        "maxlat": _coord(b.north),
        "maxlon": _coord(b.east),
    }


def _center(feature) -> dict[str, float]:
    # Overpass's center is the middle of the bounding box, not a centroid.
    b = feature.bounds
    return {"lat": _coord((b.south + b.north) / 2), "lon": _coord((b.west + b.east) / 2)}


def _members(feature) -> list:
    return list(feature.members)


def _nodes(feature) -> list:
    return list(feature.nodes)


# --- JSON -------------------------------------------------------------------------


def _json(result: Result, timestamp: str) -> str:
    elements = []
    for block in result.blocks:
        if block.counts is not None:
            elements.append(_json_count(block.counts))
        else:
            elements.extend(_json_element(e, block.out) for e in block.elements)
    doc = {
        "version": 0.6,
        "generator": GENERATOR,
        "osm3s": {"timestamp_osm_base": timestamp, "copyright": COPYRIGHT},
        "elements": elements,
    }
    if result.remark:
        doc["remark"] = result.remark
    return json.dumps(doc, ensure_ascii=False, indent=2)


def _json_count(counts: dict[str, int]) -> dict:
    total = sum(counts.values())
    return {
        "type": "count",
        "id": 0,
        "tags": {
            "nodes": str(counts["node"]),
            "ways": str(counts["way"]),
            "relations": str(counts["relation"]),
            "areas": str(counts["area"]),
            "total": str(total),
        },
    }


def _json_element(e: Element, out: Out) -> dict:
    d: dict = {"type": e.kind, "id": e.id}
    f = e.feature
    v, g = out.verbosity, out.geometry
    if e.kind == "node":
        if v in _STRUCTURE or g:
            d["lat"] = _coord(f.lat)
            d["lon"] = _coord(f.lon)
    elif e.kind in ("way", "relation"):
        if g == "center":
            d["center"] = _center(f)
        elif g in ("bb", "geom"):
            d["bounds"] = _bounds(f)
        if e.kind == "way":
            nodes = _nodes(f) if v in _STRUCTURE or g == "geom" else []
            if v in _STRUCTURE:
                d["nodes"] = [n.id for n in nodes]
            if g == "geom":
                d["geometry"] = [{"lat": _coord(n.lat), "lon": _coord(n.lon)} for n in nodes]
        elif v in _STRUCTURE:
            d["members"] = [_json_member(m, g == "geom") for m in _members(f)]
    if v in _TAGS and e.tags:
        d["tags"] = e.tags
    return d


def _json_member(m, geom: bool) -> dict:
    d = {"type": m.osm_type, "ref": m.id, "role": m.role}
    if geom:
        if m.is_node:
            d["lat"] = _coord(m.lat)
            d["lon"] = _coord(m.lon)
        elif m.is_way:
            d["geometry"] = [{"lat": _coord(n.lat), "lon": _coord(n.lon)} for n in _nodes(m)]
    return d


# --- XML --------------------------------------------------------------------------


def _xml(result: Result, timestamp: str) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<osm version="0.6" generator={quoteattr(GENERATOR)}>',
        f"<note>{COPYRIGHT}</note>",
        f"<meta osm_base={quoteattr(timestamp)}/>",
        "",
    ]
    for block in result.blocks:
        if block.counts is not None:
            lines.extend(_xml_count(block.counts))
        else:
            for e in block.elements:
                lines.extend(_xml_element(e, block.out))
    if result.remark:
        lines.append(f"<remark> {_text(result.remark)} </remark>")
    lines.append("</osm>")
    return "\n".join(lines) + "\n"


def _text(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _attrs(**values) -> str:
    return " ".join(f"{k}={quoteattr(str(v))}" for k, v in values.items())


def _xml_count(counts: dict[str, int]) -> list[str]:
    tags = _json_count(counts)["tags"]
    return ['  <count id="0">'] + [f"    <tag {_attrs(k=k, v=v)}/>" for k, v in tags.items()] + ["  </count>"]


def _xml_element(e: Element, out: Out) -> list[str]:
    f = e.feature
    v, g = out.verbosity, out.geometry
    head = {"id": e.id}
    children: list[str] = []
    if e.kind == "node":
        if v in _STRUCTURE or g:
            head["lat"] = f"{f.lat:.7f}"
            head["lon"] = f"{f.lon:.7f}"
    elif e.kind in ("way", "relation"):
        if g == "center":
            children.append(f"    <center {_attrs(**_center(f))}/>")
        elif g in ("bb", "geom"):
            children.append(f"    <bounds {_attrs(**_bounds(f))}/>")
        if e.kind == "way" and v in _STRUCTURE:
            for n in _nodes(f):
                coords = {"lat": f"{n.lat:.7f}", "lon": f"{n.lon:.7f}"} if g == "geom" else {}
                children.append(f"    <nd {_attrs(ref=n.id, **coords)}/>")
        elif e.kind == "relation" and v in _STRUCTURE:
            for m in _members(f):
                attrs = {"type": m.osm_type, "ref": m.id, "role": m.role}
                if g == "geom" and m.is_node:
                    attrs.update(lat=f"{m.lat:.7f}", lon=f"{m.lon:.7f}")
                if g == "geom" and m.is_way:
                    children.append(f"    <member {_attrs(**attrs)}>")
                    children.extend(f'      <nd lat="{n.lat:.7f}" lon="{n.lon:.7f}"/>' for n in _nodes(m))
                    children.append("    </member>")
                else:
                    children.append(f"    <member {_attrs(**attrs)}/>")
    if v in _TAGS:
        children.extend(f"    <tag {_attrs(k=k, v=val)}/>" for k, val in e.tags.items())
    opening = f"  <{e.kind} {_attrs(**head)}"
    if not children:
        return [opening + "/>"]
    return [opening + ">", *children, f"  </{e.kind}>"]


# --- CSV --------------------------------------------------------------------------


def _csv(settings: Settings, result: Result) -> str:
    fmt = settings.csv
    sep = fmt.separator
    rows = []
    if fmt.header:
        rows.append(sep.join("@" + name[2:] if name.startswith("::") else name for name in fmt.fields))
    for block in result.blocks:
        if block.counts is not None:
            rows.append(sep.join(_csv_count_field(name, block.counts) for name in fmt.fields))
            continue
        for e in block.elements:
            rows.append(sep.join(_csv_field(name, e, block.out) for name in fmt.fields))
    return "\n".join(rows) + "\n"


def _csv_field(name: str, e: Element, out: Out) -> str:
    if not name.startswith("::"):
        return e.tags.get(name, "")
    special = name[2:]
    if special == "id":
        return str(e.id)
    if special == "type":
        return e.kind
    if special == "otype":
        return str(_CSV_TYPE_CODES[e.kind])
    if special in ("lat", "lon"):
        if e.kind == "node":
            return f"{getattr(e.feature, special):.7f}"
        if out.geometry == "center" and e.kind in ("way", "relation"):
            return f"{_center(e.feature)[special]:.7f}"
        return ""
    # ::user, ::uid, ::version, ::timestamp, ::changeset: not in a GOL.
    return ""


def _csv_count_field(name: str, counts: dict[str, int]) -> str:
    keys = {
        "::count": sum(counts.values()),
        "::count:nodes": counts["node"],
        "::count:ways": counts["way"],
        "::count:relations": counts["relation"],
        "::count:areas": counts["area"],
    }
    return str(keys[name]) if name in keys else ""


def error_page(messages: list[str]) -> str:
    """What Overpass answers a 400 with: an HTML page, one paragraph per error."""
    items = "\n".join(
        f'<p><strong style="color:#FF0000">Error</strong>: {_text(m)} </p>' for m in messages
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN"
    "http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">
<head>
  <meta http-equiv="content-type" content="text/html; charset=utf-8" lang="en"/>
  <title>OSM3S Response</title>
</head>
<body>

<p>{COPYRIGHT}</p>
{items}

</body>
</html>
"""
