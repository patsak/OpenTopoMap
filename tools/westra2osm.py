#!/usr/bin/env python3
"""Find Westra catalogue passes that OSM lacks, or has without a grade.

    1. download a region of the Westra pass catalogue (westra.ru/passes) as KML:
       classificator.php?place=<id>&type=new&export=kml
    2. read the passes OSM already has around them (mountain_pass=yes,
       natural=saddle) and the natural=ridge/arete ways from the extracts
       datasvc keeps current (www/datasvc/config.yaml's regions in the
       geofabrik cache, or --pbf), with osmium tags-filter + extract
    3. a Westra pass matches an OSM one if they are within --radius metres and
       their names agree once "пер.", "перевал", case, ё, hyphens and word
       order are dropped and Westra's abbreviations are spelled out ("З." is
       "Западный", "Б." is "Большой"), with a fuzzy ratio for the rest of the
       spelling; "б/н" meets an unnamed OSM pass
    4. one OSM pass is one Westra pass: when several Westra passes claim the
       same OSM node, the closest name match (else the closest) keeps it
    5. write an .osm file for JOSM (open it, review, upload) with the edits:
       every OSM pass Westra matched gets the tags it lacks (westra_ref at
       least, until it has one), every pass that did not match is created.
       Geofabrik extracts keep object versions, which
       JOSM needs to upload a modified object; an extract a day old shows up
       as a conflict on upload, not as a lost edit:

       update  the matched OSM pass gets the tags it lacks (action=modify):
               matched      a name match within --radius, already graded
               no_grade     a name match within --radius without rtsa_scale
               far          a pass of the same name --radius..1 km off -
                            Westra coordinates are often a few hundred
                            metres from the saddle
               close        no name match, but a pass of the same rtsa_scale
                            closer than --close (40 m) - the same saddle,
                            whatever either calls it; the Westra name goes to
                            alt_name
               osm_unnamed  no name match, but an unnamed pass within --radius
               one that already has all of them is left out
       create  a new node (negative id), put into the nearest natural=ridge or
               arete within --snap metres as a new vertex of that way - or
               the untagged vertex it falls on - else left loose at the
               Westra point

    The only tags ever written: mountain_pass=yes, name, alt_name (Westra's
    other names, ";"-separated), ele, rtsa_scale, westra_ref (the catalogue
    number). Outside Russia name:ru and alt_name:ru go in place of name and
    alt_name, so the local name stays (see Countries); a pass no extract
    covers is dropped, as there is no OSM to check it against.

    ./westra2osm.py --list                     # region ids, as in place=<id>
    ./westra2osm.py --place 65 -o altai.osm
    ./westra2osm.py --place 65 --place 384 --radius 150 --snap 50 -o passes.osm

Names lose "пер." and get Westra's abbreviations spelled out ("Айташ З." is
"Айташ Западный", "Б.Кулагаш" is "Большой Кулагаш"); a bare height ("пер.
2460") is no name. Check it in JOSM before uploading: the nearest point of a
ridge is not its lowest one.

Westra summits (вер., пик, г.) are left out: OSM has no grade tag for them.
So are passes with "траверс" in the name - a route over a summit, not a saddle.

Standard library and the osmium command.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import sys
import subprocess
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

WESTRA = "https://westra.ru/passes/"
KML_URL = WESTRA + "classificator.php?place={place}&type=new&export=kml"
UA = "OpenTopoMap tools/westra2osm.py"
KML = "{http://earth.google.com/kml/2.1}"

PASS_PREFIXES = ("пер.",)
NEAREST_M = 1000
NAME_RATIO = 0.8


# ----------------------------------------------------------------- download
def fetch(url: str, data: bytes | None = None, timeout: int = 300) -> bytes:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def list_places() -> list[tuple[str, str, int]]:
    """(id, name, level) for every region of the catalogue, in tree order."""
    page = fetch(WESTRA).decode("cp1251", errors="replace")
    m = re.search(r"data:(\[\{.*?\}),?\]\}\);", page, re.S)
    if not m:
        sys.exit("westra.ru/passes: region list not found, the page has changed")
    return [(d["id"], d["value"], int(d["lev"])) for d in json.loads(m.group(1) + "]")]


# ---------------------------------------------------------------- Westra KML
def _rows(description: str) -> dict[str, str]:
    """The <th>/<td> table of a placemark's balloon, as plain text."""
    rows = {}
    for th, td in re.findall(r"<th>([^<]*)</th><td>(.*?)</td>", description, re.S):
        td = re.sub(r"<br\s*/?>", "; ", td)
        rows[th] = html.unescape(re.sub(r"<[^>]+>", "", td)).replace("\xa0", " ").strip()
    return rows


def westra_passes(place: str) -> list[dict]:
    root = ET.fromstring(fetch(KML_URL.format(place=place)))
    out = []

    def walk(el, folders):
        for child in el:
            if child.tag == KML + "Folder":
                walk(child, folders + [child.findtext(KML + "name", "")])
            elif child.tag == KML + "Placemark":
                p = placemark(child, folders)
                if p:
                    out.append(p)

    def placemark(pm, folders):
        name = (pm.findtext(KML + "name") or "").strip()
        coords = pm.findtext(f"{KML}Point/{KML}coordinates")
        # "пер. Вершина 3592 (траверс)" is a route over a summit, not a saddle
        if not name.startswith(PASS_PREFIXES) or "траверс" in name.lower() or not coords:
            return None
        lon, lat = (float(v) for v in coords.strip().split(",")[:2])
        rows = _rows(pm.findtext(KML + "description") or "")
        number = rows.get("Номер", "")
        return {
            "westra_id": int(number) if number.isdigit() else None,
            "name": name,
            "other_names": rows.get("Другие названия", ""),
            "grade": rows.get("Категория сложности", ""),
            "ele": rows.get("Высота", ""),
            "slopes": rows.get("Характер склонов", ""),
            "connects": rows.get("Соединяет", ""),
            "region": " / ".join(f for f in folders if f),
            "url": f"{WESTRA}Passes/{number}" if number else "",
            "lon": lon,
            "lat": lat,
        }

    doc = root.find(KML + "Document")
    walk(doc if doc is not None else root, [])
    return out


# ---------------------------------------------------------------- OSM (PBF)
def config_pbfs() -> list[Path]:
    """The extracts datasvc keeps current: www/datasvc/config.yaml's regions in
    the geofabrik cache of the shared data dir (OTM_DATA_DIR, else
    www/mapsvc/data)."""
    repo = Path(__file__).resolve().parent.parent
    data_dir = Path(os.environ.get("OTM_DATA_DIR", "").strip() or repo / "www/mapsvc/data")
    config = (repo / "www/datasvc/config.yaml").read_text()
    section = re.search(r"^regions:\n((?:\s*(?:-.*|#.*)?\n)+)", config, re.M)
    regions = re.findall(r"^\s*-\s*(\S+)", section.group(1), re.M) if section else []
    return [data_dir / "geofabrik-cache" / f"{r.rsplit('/', 1)[-1]}-latest.osm.pbf"
            for r in regions]


# ------------------------------------------------------------------ country
def in_polygon(lat: float, lon: float, geom: dict) -> bool:
    """Point in a GeoJSON (Multi)Polygon, even-odd over all its rings."""
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    inside = False
    for poly in polys:
        for ring in poly:
            for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
                if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
                    inside = not inside
    return inside


def _bbox(geom: dict) -> tuple[float, float, float, float]:
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    xs = [x for poly in polys for x, _ in poly[0]]
    ys = [y for poly in polys for _, y in poly[0]]
    return min(ys), min(xs), max(ys), max(xs)


def _country_border(pbf: Path) -> list[dict]:
    """The exact admin_level=2 polygon of the country an extract is cut for.

    Only the extract's own country closes: a neighbour's relation is cut at
    the extract's edge, so osmium cannot build it.
    """
    out = []
    with tempfile.TemporaryDirectory(prefix="westra2osm-") as tmp:
        subprocess.run(["osmium", "tags-filter", "-O", "-o", f"{tmp}/b.osm.pbf", str(pbf),
                        "r/admin_level=2"], check=True)
        subprocess.run(["osmium", "export", "-O", "-f", "geojsonseq", "-o", f"{tmp}/b.json",
                        f"{tmp}/b.osm.pbf"], check=True, stderr=subprocess.DEVNULL)
        with open(f"{tmp}/b.json") as f:
            for line in f:
                d = json.loads(line.lstrip("\x1e"))
                props = d["properties"]
                if (d["geometry"]["type"] in ("Polygon", "MultiPolygon")
                        and props.get("boundary") == "administrative"
                        and (props.get("ISO3166-1") or props.get("ISO3166-1:alpha2"))):
                    out.append(d["geometry"])
    return out


class Countries:
    """Which passes are in Russia, which are abroad, and which our extracts do
    not cover at all.

    Outside Russia a pass gets name:ru instead of name, so the local name
    stays. Russia is Geofabrik's polygon of it, which reaches a few km past
    the border, minus the exact border of each non-Russian extract's country
    (Kazakhstan; built by osmium, when it closes inside the extract) and
    minus every other country's Geofabrik polygon - so a point in Russia's
    and in a neighbour's (the Georgian or Mongolian side of a ridge) counts
    as abroad, keeping whatever name is there. A pass no extract covers is dropped: there is no
    OSM to check it against, so it would only make a duplicate.
    """

    def __init__(self, pbfs: list[Path]):
        index = json.loads((pbfs[0].parent / "index-v1.json").read_text())
        regions = {f["properties"]["id"]: f for f in index["features"] if f.get("geometry")}
        ids = [re.sub(r"(-latest)?\.osm\.pbf$", "", p.name) for p in pbfs]
        unknown = [i for i in ids if i not in regions]
        if unknown:
            print(f"not in the Geofabrik index, coverage not checked: {', '.join(unknown)}",
                  file=sys.stderr)
        self.cover = None if unknown else [regions[i]["geometry"] for i in ids]

        def russian(i: str) -> bool:
            while i:
                if i == "russia":
                    return True
                i = regions[i]["properties"].get("parent") if i in regions else None
            return False

        self.exact, exact_ids = [], set()
        for pbf, i in zip(pbfs, ids):
            if i in regions and not russian(i):
                border = _country_border(pbf)
                if border:
                    self.exact += border
                    exact_ids.add(i)
                else:  # an extract cut through its own border: Geofabrik's polygon
                    print(f"{i}: the country border does not close in its extract, "
                          "using the Geofabrik polygon", file=sys.stderr)
        self.russia = [regions["russia"]["geometry"]]
        # Countries and their parts, not continents; the ones with an exact
        # border above are left to it.
        self.neighbours = [f["geometry"] for i, f in regions.items()
                           if f["properties"].get("parent") and not russian(i)
                           and i not in exact_ids]
        self.boxes = {id(g): _bbox(g) for g in
                      self.exact + self.neighbours + self.russia + (self.cover or [])}

    def _in(self, lat: float, lon: float, geoms: list[dict]) -> bool:
        for g in geoms:
            s, w, n, e = self.boxes[id(g)]
            if s <= lat <= n and w <= lon <= e and in_polygon(lat, lon, g):
                return True
        return False

    def __call__(self, lat: float, lon: float) -> str:
        """"ru", "abroad", or "" where no extract covers the point."""
        if self.cover is not None and not self._in(lat, lon, self.cover):
            return ""
        if (not self._in(lat, lon, self.russia) or self._in(lat, lon, self.exact)
                or self._in(lat, lon, self.neighbours)):
            return "abroad"
        return "ru"


def load_osm(pbfs: list[Path], bbox: tuple[float, float, float, float],
             ) -> tuple[dict[int, ET.Element], dict[int, ET.Element]]:
    """Passes, saddles and ridges in bbox with their nodes, as .osm XML
    elements by id: (nodes, ways).

    Geofabrik extracts carry version and timestamp, which is all JOSM needs to
    upload a modified object. An object on the border of two extracts comes
    from the one that has its newer version.
    """
    s, w, n, e = bbox
    nodes: dict[int, ET.Element] = {}
    ways: dict[int, ET.Element] = {}
    with tempfile.TemporaryDirectory(prefix="westra2osm-") as tmp:
        for i, pbf in enumerate(pbfs):
            filtered, cut = f"{tmp}/{i}.osm.pbf", f"{tmp}/{i}.osm"
            subprocess.run(["osmium", "tags-filter", "-O", "-o", filtered, str(pbf),
                            "nw/mountain_pass=yes", "nw/natural=saddle", "nw/westra_ref",
                            *(f"nw/{k}" for k in WESTRA_LINK_KEYS),
                            "w/natural=ridge,arete"], check=True)
            subprocess.run(["osmium", "extract", "-O", "-s", "complete_ways",
                            "-b", f"{w},{s},{e},{n}", "-o", cut, filtered], check=True)
            for el in ET.parse(cut).getroot():
                into = {"node": nodes, "way": ways}.get(el.tag)
                if into is None:
                    continue
                oid = int(el.get("id"))
                if oid not in into or int(el.get("version", 0)) > int(into[oid].get("version", 0)):
                    into[oid] = el
    return nodes, ways


def _tags(el: ET.Element) -> dict[str, str]:
    return {t.get("k"): t.get("v") for t in el.findall("tag")}


def _where(node: ET.Element) -> tuple[float, float]:
    return float(node.get("lat")), float(node.get("lon"))


def osm_passes(nodes: dict, ways: dict) -> list[dict]:
    """mountain_pass=yes, natural=saddle and anything with a westra_ref or a
    Westra link (summits too: the catalogue numbers them alike), a way at its
    bbox centre."""
    out = []
    for kind, els in (("n", nodes), ("w", ways)):
        for oid, el in els.items():
            tags = _tags(el)
            if (tags.get("mountain_pass") != "yes" and tags.get("natural") != "saddle"
                    and not westra_refs(tags)):
                continue
            if kind == "n":
                lat, lon = _where(el)
            else:
                pts = [_where(nodes[r]) for r in (int(nd.get("ref")) for nd in el.findall("nd"))
                       if r in nodes]
                if not pts:
                    continue
                lat = (min(p[0] for p in pts) + max(p[0] for p in pts)) / 2
                lon = (min(p[1] for p in pts) + max(p[1] for p in pts)) / 2
            is_pass = tags.get("mountain_pass") == "yes" or tags.get("natural") == "saddle"
            out.append({"id": f"{kind}{oid}", "lat": lat, "lon": lon, "tags": tags,
                        "is_pass": is_pass})
    return out


def osm_ridges(nodes: dict, ways: dict) -> list[dict]:
    """natural=ridge/arete ways as {id, coords: [(lat, lon), ...]}."""
    out = []
    for oid, el in ways.items():
        if _tags(el).get("natural") not in ("ridge", "arete"):
            continue
        refs = [int(nd.get("ref")) for nd in el.findall("nd")]
        if len(refs) >= 2 and all(r in nodes for r in refs):
            out.append({"id": f"w{oid}", "coords": [_where(nodes[r]) for r in refs]})
    return out


# ------------------------------------------------------------------ matching
_TYPE_WORDS = {"пер", "перевал", "седловина", "седло", "траверс", "вершина", "вер",
               "пик", "гора", "г", "pass", "col", "saddle"}
_UNNAMED = {"б/н", "бн", "безымянный", "безымянная", "безымянное"}

# Westra abbreviates the qualifiers OSM spells out, and the two disagree on
# gender ("Западное плато" / "Западный"), so both sides become one stem.
_STEMS = {
    "запад": ("з", "зап"), "восток": ("в", "вост"), "север": ("с", "сев"),
    "юг": ("ю", "юж"), "центр": ("ц", "центр", "центральн", "ср", "средн"),
    "больш": ("б", "бол"), "мал": ("м", "мал"), "ложн": ("л", "ложн"),
    "верхн": ("верх",), "нижн": ("ниж",),
}
_ABBR = {a: stem for stem, abbrs in _STEMS.items() for a in abbrs}
_PREFIX = (("западн", "запад"), ("восточн", "восток"), ("северн", "север"),
           ("южн", "юг"), ("центральн", "центр"), ("средн", "центр"), ("больш", "больш"),
           ("мал", "мал"), ("ложн", "ложн"), ("верхн", "верхн"), ("нижн", "нижн"))


def _word(w: str) -> str:
    if w in _ABBR:
        return _ABBR[w]
    for prefix, stem in _PREFIX:
        if w.startswith(prefix) and len(w) <= len(prefix) + 3:
            return stem
    return w


def norm(name: str) -> str:
    """A name reduced to sorted words: "пер. Б.Кулагаш" == "Большой Кулагаш",
    "№3" == "N3"."""
    name = name.lower().replace("ё", "е").replace("№", " n")
    name = re.sub(r"\b[nн]\.?\s*(?=\d)", "n ", name)  # №3 == N 3 == N3 == Н.3
    name = re.sub(r"[^\w/]+", " ", name)
    words = [w for w in name.split() if w not in _TYPE_WORDS]
    if not words or " ".join(words) in _UNNAMED:
        return ""
    return " ".join(sorted(_word(w) for w in words))


WESTRA_LINK_KEYS = ("website", "url", "contact:website")
_WESTRA_LINK = re.compile(r"westra\.ru/passes/Passes/(\d+)", re.I)


def westra_refs(tags: dict) -> set[str]:
    """Catalogue numbers an OSM object names: westra_ref, or a link to the
    pass's page (website=https://westra.ru/passes/Passes/6748)."""
    refs = {r.strip() for r in tags.get("westra_ref", "").split(";") if r.strip()}
    for k in WESTRA_LINK_KEYS:
        refs.update(_WESTRA_LINK.findall(tags.get(k, "")))
    return refs


def unnamed(name: str) -> bool:
    return norm(name) == ""


def westra_names(p: dict) -> set[str]:
    """The name, its parts in and out of brackets, and the other names."""
    raw = [p["name"], re.sub(r"\(.*?\)", "", p["name"])]
    raw += re.findall(r"\((.*?)\)", p["name"])
    raw += re.split(r"[,;]", p["other_names"])
    return {n for n in map(norm, raw) if n}


OSM_NAME_KEYS = ("name", "name:ru", "alt_name", "old_name", "official_name", "loc_name",
                 "short_name", "name:en")


def osm_names(tags: dict) -> set[str]:
    raw = []
    for k in OSM_NAME_KEYS:
        raw += tags.get(k, "").split(";")
    return {n for n in map(norm, raw) if n}


def same_name(a: set[str], b: set[str]) -> bool:
    for x in a:
        for y in b:
            if x == y:
                return True
            # hyphens and spaces differ more than letters do: Казакталман / Казак-Талман
            xs, ys = x.replace(" ", ""), y.replace(" ", "")
            if xs == ys or (min(len(xs), len(ys)) >= 5
                            and SequenceMatcher(None, xs, ys).ratio() >= NAME_RATIO):
                return True
    return False


def metres(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(a))


def rtsa_scale(grade: str) -> str:
    """Westra grade -> rtsa_scale, or "" when it is not a plain grade.

    "1б*" -> "1Б*", "нк" -> "н/к", "1А-1Б" stays a range; seasonal grades
    ("1Б зимой") and question marks are left for a human.
    """
    g = grade.strip().upper().replace("Н/К", "н/к").replace("НК", "н/к")
    one = r"(н/к|[123][АБ]\*?)"
    return g if re.fullmatch(rf"{one}(-{one})?", g) else ""


def match(passes: list[dict], osm: list[dict], radius: float, close: float) -> list[dict]:
    # A coarse grid, so each pass looks only at the OSM passes near it.
    cell = NEAREST_M / 111_000 * 2
    grid: dict[tuple[int, int], list[dict]] = {}
    for o in osm:
        grid.setdefault((int(o["lat"] // cell), int(o["lon"] // cell)), []).append(o)

    # westra_ref says outright which catalogue pass an OSM object is
    by_ref: dict[str, dict] = {}
    for o in osm:
        for ref in westra_refs(o["tags"]):
            by_ref.setdefault(ref, o)

    report = []
    for p in passes:
        ref = str(p["westra_id"]) if p["westra_id"] else ""
        if ref in by_ref:
            o = by_ref[ref]
            hit = (metres(p["lat"], p["lon"], o["lat"], o["lon"]), o)
            reason = "matched" if o["tags"].get("rtsa_scale") else "no_grade"
            report.append(dict(p, reason=reason, by_ref=True, _osm=hit))
            continue
        i, j = int(p["lat"] // cell), int(p["lon"] // cell)
        near = []
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for o in grid.get((i + di, j + dj), ()):
                    d = metres(p["lat"], p["lon"], o["lat"], o["lon"])
                    if d <= NEAREST_M:
                        near.append((d, o))
        # a summit or ridge with a westra_ref is there only for by_ref above
        near = sorted((t for t in near if t[1]["is_pass"]), key=lambda t: t[0])

        names = westra_names(p)
        grade = rtsa_scale(p["grade"])
        hit = near_by = far = blank = other = None
        for d, o in near:
            on = osm_names(o["tags"])
            # this close and of the same grade it is the same saddle, whatever
            # either calls it
            same_spot = d < close and grade and rtsa_scale(o["tags"].get("rtsa_scale", "")) == grade
            if westra_refs(o["tags"]):
                # another catalogue pass's: the same name there, or the same
                # spot, means Westra lists that pass twice
                if same_spot or (d <= radius and names and same_name(names, on)):
                    other = other or (d, o)
                continue
            if same_spot:
                near_by = near_by or (d, o)
            if same_name(names, on) if names else not on:
                if d <= radius:
                    hit = (d, o)
                    break
                far = far or (d, o)
            elif not on and d <= radius:
                blank = blank or (d, o)

        if hit:
            graded = hit[1]["tags"].get("rtsa_scale")
            feature = dict(p, reason="matched" if graded else "no_grade", _osm=hit)
        elif near_by:
            feature = dict(p, reason="close", _osm=near_by)
        elif far:
            feature = dict(p, reason="far", _osm=far)
        elif blank:
            feature = dict(p, reason="osm_unnamed", _osm=blank)
        elif other:
            feature = dict(p, reason="duplicate")
        else:
            feature = dict(p, reason="missing", _near=near[0] if near else None)
        report.append(feature)

    # One OSM pass is one Westra pass. Several Westra passes around one unnamed
    # node (Туманный Зап./Центр./Сев.) leave it to the closest; the others are
    # missing. westra_ref outranks a name match, which outranks an unnamed
    # node; a second Westra record matching the same OSM pass by name is the
    # catalogue listing one pass twice - "duplicate", dropped, not created
    # next to it.
    rank = {"matched": 0, "no_grade": 0, "close": 1, "far": 2, "osm_unnamed": 3}
    order = lambda f: (not f.get("by_ref"), rank[f["reason"]], f["_osm"][0])  # noqa: E731
    claims: dict[str, list[dict]] = {}
    for f in report:
        if "_osm" in f:
            claims.setdefault(f["_osm"][1]["id"], []).append(f)
    for fs in claims.values():
        fs.sort(key=order)
        for f in fs[1:]:
            if f["reason"] == "osm_unnamed":
                f["_near"] = f.pop("_osm")
                f["reason"] = "missing"
            else:
                f["reason"] = "duplicate"
    return report


def project(lat: float, lon: float, a: tuple, b: tuple) -> tuple[float, float, float, float]:
    """The point of segment a-b closest to (lat, lon): (lat, lon, metres, t along a-b)."""
    kx = math.cos(math.radians(lat)) * 111_320.0  # metres per degree of longitude here
    ky = 110_574.0
    ax, ay = (a[1] - lon) * kx, (a[0] - lat) * ky
    bx, by = (b[1] - lon) * kx, (b[0] - lat) * ky
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
    px, py = ax + t * dx, ay + t * dy
    return lat + py / ky, lon + px / kx, math.hypot(px, py), t


def snap_to_ridge(lat: float, lon: float, ridges: list[dict], grid: dict, cell: float,
                  limit: float):
    """The closest point of a ridge within limit metres: (lat, lon, metres, way, segment)."""
    best = None
    i, j = int(lat // cell), int(lon // cell)
    seen = set()
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            for r, k in grid.get((i + di, j + dj), ()):
                if (r, k) in seen:
                    continue
                seen.add((r, k))
                plat, plon, d, _ = project(lat, lon, *ridges[r]["coords"][k:k + 2])
                if d <= limit and (best is None or d < best[2]):
                    best = (plat, plon, d, ridges[r]["id"], k)
    return best


def ridge_grid(ridges: list[dict], cell: float) -> dict:
    """Every ridge segment under each grid cell its bbox touches."""
    grid: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for r, ridge in enumerate(ridges):
        c = ridge["coords"]
        for k in range(len(c) - 1):
            (alat, alon), (blat, blon) = c[k], c[k + 1]
            for i in range(int(min(alat, blat) // cell), int(max(alat, blat) // cell) + 1):
                for j in range(int(min(alon, blon) // cell), int(max(alon, blon) // cell) + 1):
                    grid.setdefault((i, j), []).append((r, k))
    return grid


def position(report: list[dict], ridges: list[dict], snap: float) -> list[dict]:
    """Where each feature goes: onto its OSM pass, or onto the nearest ridge.

    An OSM pass that already has every tag Westra could add drops out.
    """
    cell = max(snap, 1) / 110_000
    grid = ridge_grid(ridges, cell)
    for f in report:
        f["westra_lon"], f["westra_lat"] = f["lon"], f["lat"]
        if "_osm" in f:
            d, o = f.pop("_osm")
            f.update(action="update", osm_id=o["id"], osm_name=o["tags"].get("name", ""),
                     osm_m=round(d), lat=o["lat"], lon=o["lon"])
            # a Westra traverse whose number OSM put on a summit: found, left be
            f["tags"] = ({k: v for k, v in proposed_tags(f, o["tags"]).items()
                          if not o["tags"].get(k)} if o["is_pass"] else {})
            continue
        near = f.pop("_near", None)
        if near:
            d, o = near
            f.update(nearest_osm_id=o["id"], nearest_osm_name=o["tags"].get("name", ""),
                     nearest_osm_m=round(d))
        f["action"] = "create"
        f["tags"] = proposed_tags(f)
        hit = snap_to_ridge(f["lat"], f["lon"], ridges, grid, cell, snap)
        if hit:
            f["lat"], f["lon"], d, f["ridge_id"], f["ridge_segment"] = hit
            f["snap_m"] = round(d)
    return [f for f in report if f["tags"]]


# ------------------------------------------------------------------- output
# Westra's abbreviations spelled out for the OSM name. Single letters only at
# the end ("Айташ З."), where they cannot be someone's initial; "Б." at the
# start is always Большой in the catalogue.
_SPELL_END = {"з": "Западный", "зап": "Западный", "в": "Восточный", "вост": "Восточный",
              "с": "Северный", "сев": "Северный", "ю": "Южный", "юж": "Южный",
              "ц": "Центральный", "центр": "Центральный", "ср": "Средний",
              "средн": "Средний", "ложн": "Ложный", "л": "Ложный", "б": "Большой",
              "м": "Малый"}
_SPELL_ANY = {"б": "Большой", "бол": "Большой", "мал": "Малый", "верхн": "Верхний",
              "нижн": "Нижний", "средн": "Средний", "ложн": "Ложный"}


def osm_name(westra: str) -> str:
    """"пер. Б.Кулагаш" -> "Большой Кулагаш"; "" for б/н and bare heights."""
    name = re.sub(r"\s*\(.*?\)\s*", " ", westra).strip()
    name = re.sub(r"^пер\.\s*", "", name)
    if unnamed(name) or re.fullmatch(r"[\d.,]+", name):
        return ""
    name = re.sub(r"^([Бб])\.\s*", "Большой ", name)
    name = re.sub(r"(?<![\w.])(\w+)\.(?=\s|$)",
                  lambda m: _SPELL_ANY.get(m.group(1).lower(), m.group(0)), name)
    m = re.search(r"\s(\w+)\.?$", name)
    if m and m.group(1).lower() in _SPELL_END and (m.group(0).endswith(".") or len(m.group(1)) == 1):
        name = name[:m.start()] + " " + _SPELL_END[m.group(1).lower()]
    return re.sub(r"\s+", " ", name).strip()


def other_names(p: dict) -> list[str]:
    """Westra's "Другие названия", spelled out like the name: "Луноход,
    Мунадак" -> ["Луноход", "Мунадак"]. Heights ("4400"), old list numbers
    ("№135"), guesses ("?") and descriptions ("в хр. Рухумеэр") are no names."""
    out = []
    for raw in re.split(r"[,;]", p["other_names"]):
        raw = raw.strip()
        if (raw and "?" not in raw and not raw[0].islower()
                and not re.fullmatch(r"[№#N]\s*\d+", raw)):
            name = osm_name(raw)
            if name:
                out.append(name)
    return out


def proposed_tags(p: dict, osm_tags: dict | None = None) -> dict:
    """The only tags this tool ever adds: mountain_pass, name, alt_name, ele,
    rtsa_scale, westra_ref - name:ru and alt_name:ru abroad.

    For an existing pass (osm_tags) the Westra name goes to alt_name when the
    pass already has a name of its own, and alt_name leaves out the names OSM
    already has anywhere (name, alt_name, old_name, ...).
    """
    abroad = p["country"] != "ru"
    name_key, alt_key = ("name:ru", "alt_name:ru") if abroad else ("name", "alt_name")
    tags = {"mountain_pass": "yes"}
    if p["westra_id"]:
        tags["westra_ref"] = str(p["westra_id"])
    names = ([osm_name(p["name"])] if osm_name(p["name"]) else []) + other_names(p)
    seen = osm_names(osm_tags or {})
    if names and not (osm_tags or {}).get(name_key):
        tags[name_key] = names[0]
        seen.add(norm(names[0]))
    alt = []
    for n in names:
        if norm(n) not in seen:
            seen.add(norm(n))
            alt.append(n)
    if alt:
        tags[alt_key] = ";".join(alt)
    if p["ele"].isdigit():
        tags["ele"] = p["ele"]
    if rtsa_scale(p["grade"]):
        tags["rtsa_scale"] = rtsa_scale(p["grade"])
    return tags


# ------------------------------------------------------------------- JOSM
def _add_tags(el: ET.Element, tags: dict) -> bool:
    """Add the tags the element does not have yet; True if it changed."""
    have = _tags(el)
    new = {k: v for k, v in tags.items() if not have.get(k)}
    for k, v in new.items():
        ET.SubElement(el, "tag", k=k, v=v)
    if new:
        el.set("action", "modify")
    return bool(new)


def josm(report: list[dict], nodes: dict, ways: dict) -> tuple[ET.Element, Counter]:
    """An .osm file for JOSM: tags added to the OSM objects, and new pass
    nodes, put into their ridge ways as vertices. Edits nodes and ways in place."""
    stats: Counter = Counter()

    def where(ref: int) -> tuple[float, float]:
        return _where(nodes[ref])

    for f in report:
        if f["action"] == "update":
            el = (nodes if f["osm_id"][0] == "n" else ways)[int(f["osm_id"][1:])]
            _add_tags(el, f["tags"])
            stats["update"] += 1
    creates = [f for f in report if f["action"] == "create"]

    inserts: dict[int, list] = {}
    new_nodes: list[ET.Element] = []

    def new_node(lat: float, lon: float, tags: dict) -> int:
        nid = -1 - len(new_nodes)
        node = ET.Element("node", id=str(nid), action="modify", visible="true",
                          lat=f"{lat:.7f}", lon=f"{lon:.7f}")
        for k, v in tags.items():
            ET.SubElement(node, "tag", k=k, v=v)
        new_nodes.append(node)
        return nid

    for f in creates:
        way = ways.get(int(f["ridge_id"][1:])) if "ridge_id" in f else None
        best = None
        if way is not None:
            refs = [int(nd.get("ref")) for nd in way.findall("nd")]
            for k in range(len(refs) - 1):
                if refs[k] in nodes and refs[k + 1] in nodes:
                    hit = project(f["lat"], f["lon"], where(refs[k]), where(refs[k + 1]))
                    if best is None or hit[2] < best[0][2]:
                        best = (hit, k, refs)
        if best is None or best[0][2] > 5:
            stats["create: no ridge within --snap, at the Westra point"] += 1
            new_node(f["lat"], f["lon"], f["tags"])
            continue
        (lat, lon, _, t), k, refs = best
        # right on an untagged vertex: that vertex becomes the pass
        vertex = next((r for r in (refs[k], refs[k + 1])
                       if metres(lat, lon, *where(r)) < 2 and not _tags(nodes[r])), None)
        if vertex:
            _add_tags(nodes[vertex], f["tags"])
            stats["create: tagged an existing ridge vertex"] += 1
        else:
            inserts.setdefault(int(way.get("id")), []).append((k, t, new_node(lat, lon, f["tags"])))
            stats["create: new vertex of a ridge"] += 1

    for wid, ins in inserts.items():
        way = ways[wid]
        after: dict[int, list[int]] = {}
        for k, _, nid in sorted(ins):
            after.setdefault(k, []).append(nid)
        children = list(way)
        for c in children:
            way.remove(c)
        k = 0
        for c in children:
            way.append(c)
            if c.tag == "nd":
                for nid in after.get(k, ()):
                    ET.SubElement(way, "nd", ref=str(nid))
                k += 1
        way.set("action", "modify")

    root = ET.Element("osm", version="0.6", generator=UA, upload="true")
    root.extend(new_nodes)
    # unchanged nodes go in only as the vertices of a changed way
    used = {int(nd.get("ref")) for w in ways.values() if w.get("action") for nd in w.findall("nd")}
    root.extend(n for nid, n in sorted(nodes.items()) if n.get("action") or nid in used)
    root.extend(w for _, w in sorted(ways.items()) if w.get("action"))
    return root, stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--list", action="store_true", help="print the catalogue's regions and exit")
    ap.add_argument("--place", action="append", default=[],
                    help="Westra region id (place=<id> in the site's links); repeatable")
    ap.add_argument("--radius", type=float, default=200,
                    help="how far an OSM pass may be from the Westra point, metres (200)")
    ap.add_argument("--close", type=float, default=40,
                    help="an OSM pass of the same grade closer than this is the same pass "
                         "whatever its name, metres (40)")
    ap.add_argument("--snap", type=float, default=100,
                    help="how far a missing pass may be moved onto a natural=ridge, metres (100)")
    ap.add_argument("--pbf", action="append", type=Path, default=[],
                    help="OSM extract to read; repeatable (default: the regions of "
                         "www/datasvc/config.yaml in the geofabrik cache)")
    ap.add_argument("-o", "--output", default="westra_passes.osm")
    args = ap.parse_args()

    if args.list:
        for pid, name, lev in list_places():
            print(f"{pid:>5}  {'  ' * lev}{name}")
        return
    if not args.place:
        ap.error("--place is required (see --list)")

    passes: dict = {}
    for place in args.place:
        got = westra_passes(place)
        print(f"place={place}: {len(got)} passes with coordinates", file=sys.stderr)
        for p in got:
            passes[p["westra_id"] or (p["name"], p["lon"], p["lat"])] = p
    passes = list(passes.values())
    if not passes:
        sys.exit("no passes with coordinates")

    pbfs = args.pbf or config_pbfs()
    for pbf in pbfs:
        if not pbf.is_file():
            sys.exit(f"{pbf}: no such extract (datasvc has not synced it yet? see --pbf)")
    country = Countries(pbfs)
    for p in passes:
        p["country"] = country(p["lat"], p["lon"])
    where = Counter(p["country"] for p in passes)
    print(f"{where['ru']} passes in Russia, {where['abroad']} abroad (name:ru), "
          f"{where['']} outside the extracts (dropped)", file=sys.stderr)
    passes = [p for p in passes if p["country"]]
    if not passes:
        sys.exit("no passes inside the extracts")
    pad = NEAREST_M / 111_000
    bbox = (min(p["lat"] for p in passes) - pad, min(p["lon"] for p in passes) - pad * 2,
            max(p["lat"] for p in passes) + pad, max(p["lon"] for p in passes) + pad * 2)
    nodes, ways = load_osm(pbfs, bbox)
    osm = osm_passes(nodes, ways)
    ridges = osm_ridges(nodes, ways)
    print(f"{len(pbfs)} extracts: {len(osm)} OSM passes, {len(ridges)} natural=ridge/arete "
          "ways in the bbox", file=sys.stderr)

    report = match(passes, osm, args.radius, args.close)
    dupes = sum(f["reason"] == "duplicate" for f in report)
    by_ref = sum(bool(f.get("by_ref")) for f in report)
    report = [f for f in report if f["reason"] != "duplicate"]
    report = position(report, ridges, args.snap)
    reasons = Counter(p["reason"] for p in report)
    print(f"{by_ref} found by westra_ref; {dupes} listed twice in Westra (dropped); "
          f"{len(passes) - dupes - len(report)} matched and complete; "
          + ", ".join(f"{n} {r}" for r, n in reasons.most_common()), file=sys.stderr)

    root, stats = josm(report, nodes, ways)
    for what, n in sorted(stats.items()):
        print(f"  {what}: {n}", file=sys.stderr)
    ET.indent(root, space=" ")
    ET.ElementTree(root).write(args.output, encoding="utf-8", xml_declaration=True)
    print(f"-> {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
