"""The vocabulary the build, the graph package and the engines share.

Edge kinds and terrain classes are stored as small integers in the package
(``graph.npz``); these tables are the only place the numbers are given names,
so a package and the code reading it agree as long as they agree on
:data:`SCHEMA`.
"""

from __future__ import annotations

# Bumped whenever the package layout or the meaning of a stored value changes.
# A server refuses a package with another schema instead of misreading it.
SCHEMA = 8  # 8: OSM-aligned thalweg banks keep their ends on the mapped river

# ---------------------------------------------------------------- edge kinds
# osm        a routable OSM way, between two consecutive OSM nodes
# thalweg    a line of falling water (valley floor) traced on the DEM
# pass       the fall line from a mountain_pass down into a valley, either side
# lattice    the sparse triangular grid that keeps any two points connected
# connector  a short link between the lines above, the lattice and OSM nodes
# bank       the near bank of a river too big to ford, or of a lake
# ford       a marked ford across a river too big to ford anywhere else
# ridge      a crest: the divide between two valleys, traced on the DEM
KINDS = ("osm", "thalweg", "pass", "lattice", "connector", "bank", "ford", "ridge")
KIND = {name: code for code, name in enumerate(KINDS)}
OSM = KIND["osm"]
THALWEG = KIND["thalweg"]
PASS = KIND["pass"]
LATTICE = KIND["lattice"]
CONNECTOR = KIND["connector"]
BANK = KIND["bank"]
FORD = KIND["ford"]
RIDGE = KIND["ridge"]

# Edges walked along a line of the terrain - down a fall line, along a crest:
# the slope either side does not slow them, only the gradient along the line
# does (see routing.cost).
FALL_LINE_KINDS = (THALWEG, PASS, RIDGE)

# ----------------------------------------------------------- terrain classes
# Ordered by burn priority, lowest first: where OSM polygons overlap, the
# class later in this tuple wins (a scrub polygon inside a wood is scrub).
TERRAINS = (
    "unknown",
    "grass",
    "bare_rock",
    "scree",
    "heath",
    "wood",
    "scrub",
    "wetland",
    "glacier",
    "water",
    "path",  # never burnt: what an OSM way is walked on
)
TERRAIN = {name: code for code, name in enumerate(TERRAINS)}

# OSM tags -> terrain class. Checked in order; the first match wins.
TERRAIN_TAGS: tuple[tuple[str, str, str], ...] = (
    ("natural", "water", "water"),
    ("waterway", "riverbank", "water"),
    ("landuse", "reservoir", "water"),
    ("natural", "glacier", "glacier"),
    ("natural", "wetland", "wetland"),
    ("natural", "scrub", "scrub"),
    ("natural", "wood", "wood"),
    ("landuse", "forest", "wood"),
    ("natural", "heath", "heath"),
    ("natural", "scree", "scree"),
    ("natural", "shingle", "scree"),
    ("natural", "bare_rock", "bare_rock"),
    ("natural", "grassland", "grass"),
    ("natural", "fell", "grass"),
    ("natural", "tundra", "grass"),
    ("landuse", "meadow", "grass"),
    ("landuse", "grass", "grass"),
)


def terrain_for(tags) -> str | None:
    for key, value, terrain in TERRAIN_TAGS:
        if tags.get(key) == value:
            return terrain
    return None


# ------------------------------------------------------------- pass grades
# rtsa_scale (https://wiki.openstreetmap.org/wiki/RU:Key:rtsa_scale): the
# Russian pass categories, easiest first. "*" (the upper end of a category)
# counts as the category; a pass without the tag is UNRATED.
GRADES = ("н/к", "1А", "1Б", "2А", "2Б", "3А", "3Б")
UNRATED = len(GRADES)
NO_PASS = 255


def pass_grade(value: str | None) -> int:
    """rtsa_scale -> 0 (н/к) .. 6 (3Б), UNRATED for anything else."""
    if not value:
        return UNRATED
    text = value.strip().lower().replace("*", "").replace(" ", "")
    text = text.translate(str.maketrans({"a": "а", "b": "б"}))  # latin 1A/2B as cyrillic
    if text in ("н/к", "нк", "n/k", "nk", "н.к.", "0"):
        return 0
    for i, grade in enumerate(GRADES):
        if text == grade.lower():
            return i
    return UNRATED
