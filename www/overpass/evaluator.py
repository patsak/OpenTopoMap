"""Runs a parsed Overpass QL script against a GeoDesk GOL.

Overpass QL is a data flow over named sets of OSM elements, and so is this:
every statement reads sets, writes one, and ``out`` copies a set to the
response. An element is a GeoDesk feature plus the type Overpass would give it
(``node``/``way``/``relation``/``area``).

Where the work goes. A query with no input set is a GeoDesk query: the element
type and the tag filters become a GOQL selector, and the spatial filters
(bbox, around, poly, area) become GeoDesk's own spatial filters. Everything
else - input sets, ids, recursion - produces the candidates in Python, and the
filters are then tested one element at a time.

GOQL is only ever a prefilter. It is typed where Overpass is not: a value that
looks like a number is stored as a number in a GOL, so ``[admin_level="4"]``
and ``[admin_level~"^4$"]`` match nothing in GOQL while Overpass, comparing
strings, matches every admin_level=4. GOQL's quoted strings are also globs
(``"pu*"`` matches "pub"). ``[k]`` in GOQL means "k is there and is not no",
where Overpass's ``[shop]`` takes shop=no as well. And GeoDesk 2.3 gets some
combinations wrong outright: a ``[!k]`` on a key rare enough to be missing
from the GOL's global string table, next to any other clause, drops features
that have neither key. So the selector carries one positive clause - an
equality, else "the key is there" written as ``[k], [k=no]`` - and every tag
filter, that one included, is checked again here on the tag's string form,
which is where the Overpass semantics live.

One hard rule about GeoDesk 2.3: a query iterator abandoned before it is
exhausted crashes the process (a segfault in the native extension, not an
exception). Every GeoDesk collection is therefore read whole with ``list()``,
and nothing here breaks out of a loop over one.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from geodesk import Box, Coordinate, Features, distance, to_mercator
import numpy as np
import shapely
from shapely import STRtree
from shapely.geometry import LineString, Point, Polygon

from overpass.syntax import (
    COUNT_TYPES,
    DEFAULT_SET,
    AreaFilter,
    AroundFilter,
    Binary,
    BboxFilter,
    Call,
    Expr,
    Filter,
    ForEach,
    GlobalBboxFilter,
    IdFilter,
    IfFilter,
    IsIn,
    Literal,
    MapToArea,
    Out,
    PivotFilter,
    PolyFilter,
    Query,
    Recurse,
    RecurseFilter,
    SetCopy,
    Settings,
    Statement,
    TagFilter,
    TagValue,
    Ternary,
    Unary,
    Union,
)

# Overpass derives an area's id from the element it was built from.
AREA_FROM_RELATION = 3_600_000_000
AREA_FROM_WAY = 2_400_000_000

KIND_ORDER = {"node": 0, "way": 1, "relation": 2, "area": 3}

QUERY_KINDS = {
    "node": frozenset({"node"}),
    "way": frozenset({"way"}),
    "relation": frozenset({"relation"}),
    "nwr": frozenset({"node", "way", "relation"}),
    "nw": frozenset({"node", "way"}),
    "nr": frozenset({"node", "relation"}),
    "wr": frozenset({"way", "relation"}),
    "area": frozenset({"area"}),
}

# GOQL type letters per Overpass type. A closed way or a multipolygon is "a" in
# GeoDesk, not "w"/"r", so ways and relations are asked for together with
# areas and the kind is checked afterwards.
GOQL_TYPES = {
    "node": "n",
    "way": "wa",
    "relation": "ra",
    "nwr": "*",
    "nw": "nwa",
    "nr": "nra",
    "wr": "wra",
    "area": "a",
}

_NUMBER = re.compile(r"\s*[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?\s*")
# Values safe to hand GOQL as a quoted string: no quote or backslash to escape,
# no glob character, and not number-shaped (those are stored as numbers).
_GOQL_UNSAFE = re.compile(r"[\"\\*?\n]")

# Up to this many centres an around is one GeoDesk query per centre; past it,
# one query over their extent and an indexed test (Evaluator.world_query).
AROUND_QUERIES_MAX = 32

# How often the deadline is looked at while walking elements.
_TICK_EVERY = 1000


class QueryRuntimeError(Exception):
    """Overpass's "runtime error": the response still goes out, with a remark."""


class Element:
    __slots__ = ("kind", "id", "feature", "_tags")

    def __init__(self, kind: str, id: int, feature):
        self.kind = kind
        self.id = id
        self.feature = feature
        self._tags: dict[str, str] | None = None

    @property
    def key(self) -> tuple[str, int]:
        return (self.kind, self.id)

    @property
    def sort_key(self) -> tuple[int, int]:
        return (KIND_ORDER[self.kind], self.id)

    @property
    def tags(self) -> dict[str, str]:
        """Tags as Overpass has them: every value a string, as written in OSM."""
        if self._tags is None:
            self._tags = _tags_of(self.feature)
        return self._tags

    def __repr__(self) -> str:
        return f"{self.kind}/{self.id}"


ElementSet = dict[tuple[str, int], Element]


def _tags_of(feature) -> dict[str, str]:
    """A feature's tags with every value a string.

    GeoDesk hands numbers back as int/float; Feature.str gives the stored
    text. dict() first and a fix-up of the few non-strings after, because a
    scan builds this millions of times.
    """
    tags = dict(feature.tags)
    for k, v in tags.items():
        if v.__class__ is not str:
            return {k: v if v.__class__ is str else feature.str(k) for k, v in tags.items()}
    return tags


def element(feature) -> Element:
    if feature.is_node:
        kind = "node"
    elif feature.is_way:
        kind = "way"
    else:
        kind = "relation"
    return Element(kind, feature.id, feature)


def area_element(feature) -> Element:
    offset = AREA_FROM_WAY if feature.is_way else AREA_FROM_RELATION
    return Element("area", feature.id + offset, feature)


def sorted_elements(elements: ElementSet) -> list[Element]:
    return sorted(elements.values(), key=lambda e: e.sort_key)


@dataclass
class OutBlock:
    out: Out
    elements: list[Element] = field(default_factory=list)
    # out count: per kind; None for every other verbosity.
    counts: dict[str, int] | None = None


@dataclass
class Result:
    blocks: list[OutBlock]
    remark: str | None = None


def run(world: Features, settings: Settings, statements: list[Statement], *, max_elements: int) -> Result:
    evaluator = Evaluator(world, settings, max_elements=max_elements)
    try:
        evaluator.execute_all(statements)
    except QueryRuntimeError as exc:
        return Result(blocks=evaluator.blocks, remark=f"runtime error: {exc}")
    return Result(blocks=evaluator.blocks)


class Evaluator:
    def __init__(self, world: Features, settings: Settings, *, max_elements: int):
        self.world = world
        self.settings = settings
        self.max_elements = max_elements
        self.deadline = time.monotonic() + settings.timeout
        self.sets: dict[str, ElementSet] = {DEFAULT_SET: {}}
        self.blocks: list[OutBlock] = []
        self.line = 0
        self.statement_name = ""
        self._ticks = 0

    # --- bookkeeping --------------------------------------------------------

    def tick(self) -> None:
        self._ticks += 1
        if self._ticks % _TICK_EVERY == 0:
            self.check_deadline()

    def check_deadline(self) -> None:
        if time.monotonic() > self.deadline:
            raise QueryRuntimeError(
                f'Query timed out in "{self.statement_name}" at line {self.line} '
                f"after {self.settings.timeout} seconds."
            )

    def store(self, name: str, elements: ElementSet) -> None:
        self.check_size(elements)
        self.sets[name] = elements

    def check_size(self, elements: ElementSet) -> None:
        if len(elements) > self.max_elements:
            raise QueryRuntimeError(
                f'Query ran out of memory in "{self.statement_name}" at line {self.line}: '
                f"more than {self.max_elements} elements in one set."
            )

    def get(self, name: str) -> ElementSet:
        # An undefined set is an empty one, as in Overpass.
        return self.sets.get(name, {})

    # --- statements ---------------------------------------------------------

    def execute_all(self, statements: list[Statement]) -> None:
        for statement in statements:
            self.execute(statement)

    def execute(self, st: Statement) -> None:
        self.line = st.line
        self.statement_name = _statement_name(st)
        self.check_deadline()
        if isinstance(st, Query):
            self.store(st.into, self.query(st))
        elif isinstance(st, Union):
            self.union(st)
        elif isinstance(st, SetCopy):
            self.store(st.into, dict(self.get(st.input_set)))
        elif isinstance(st, Recurse):
            self.store(st.into, self.recurse(st.op, self.get(st.input_set)))
        elif isinstance(st, Out):
            self.out(st)
        elif isinstance(st, ForEach):
            for e in sorted_elements(self.get(st.input_set)):
                self.store(st.loop_set, {e.key: e})
                self.execute_all(st.body)
        elif isinstance(st, MapToArea):
            self.store(st.into, self.map_to_area(self.get(st.input_set)))
        elif isinstance(st, IsIn):
            self.store(st.into, self.is_in(st))
        else:  # pragma: no cover - the parser produces nothing else
            raise TypeError(st)

    def union(self, st: Union) -> None:
        result: ElementSet = {}
        for negated, member in st.members:
            self.execute(member)
            produced = self.get(getattr(member, "into", DEFAULT_SET)) if _produces_set(member) else {}
            if negated:
                result = {k: e for k, e in result.items() if k not in produced}
            else:
                result.update(produced)
        self.line = st.line
        self.statement_name = "union"
        self.store(st.into, result)

    def out(self, st: Out) -> None:
        elements = self.get(st.input_set)
        if st.verbosity == "count":
            counts = {kind: 0 for kind in KIND_ORDER}
            for e in elements.values():
                counts[e.kind] += 1
            self.blocks.append(OutBlock(out=st, counts=counts))
            return
        ordered = sorted_elements(elements)
        if st.limit is not None:
            ordered = ordered[: st.limit]
        self.blocks.append(OutBlock(out=st, elements=ordered))

    def map_to_area(self, elements: ElementSet) -> ElementSet:
        out: ElementSet = {}
        for e in elements.values():
            if e.kind in ("way", "relation") and e.feature.is_area:
                a = area_element(e.feature)
                out[a.key] = a
        return out

    def is_in(self, st: IsIn) -> ElementSet:
        if st.point is not None:
            lat, lon = st.point
            points = [Coordinate(lon=lon, lat=lat)]
        else:
            points = [Coordinate(e.feature.x, e.feature.y) for e in self.get(st.input_set).values() if e.kind == "node"]
        out: ElementSet = {}
        areas = self.world("a")
        for p in points:
            self.tick()
            for f in list(areas.containing(p)):
                a = area_element(f)
                out[a.key] = a
        return out

    # --- recursion ------------------------------------------------------------

    def recurse(self, op: str, elements: ElementSet) -> ElementSet:
        if op in (">", ">>"):
            return self.down(elements.values(), recursive=op == ">>")
        return self.up(elements.values(), recursive=op == "<<")

    def down(self, elements, *, recursive: bool) -> ElementSet:
        """``>``: the nodes of ways, the node and way members of relations and
        the nodes of those ways. ``>>`` also descends into member relations."""
        out: ElementSet = {}
        stack = [e for e in elements if e.kind in ("way", "relation")]
        visited: set[int] = set()
        while stack:
            e = stack.pop()
            if e.kind == "way":
                self.add_way_nodes(e, out)
                continue
            if e.id in visited:
                continue
            visited.add(e.id)
            for m in list(e.feature.members):
                self.tick()
                me = element(m)
                if me.kind == "relation":
                    if recursive:
                        out[me.key] = me
                        stack.append(me)
                    continue
                out[me.key] = me
                if me.kind == "way":
                    self.add_way_nodes(me, out)
        return out

    def add_way_nodes(self, way: Element, out: ElementSet) -> None:
        for n in list(way.feature.nodes):
            self.tick()
            ne = element(n)
            out[ne.key] = ne

    def up(self, elements, *, recursive: bool) -> ElementSet:
        """``<``: ways holding the nodes, relations holding the nodes or ways,
        and relations holding the ways found. ``<<`` follows relations up to
        the top."""
        elements = [e for e in elements if e.kind != "area"]
        out: ElementSet = {}
        ways: list[Element] = []
        for e in elements:
            if e.kind == "relation" and not recursive:
                continue
            for p in list(e.feature.parents):
                self.tick()
                pe = element(p)
                if pe.key in out:
                    continue
                out[pe.key] = pe
                if pe.kind == "way":
                    ways.append(pe)
        frontier = ways + ([pe for pe in out.values() if pe.kind == "relation"] if recursive else [])
        while frontier:
            e = frontier.pop()
            for p in list(e.feature.parents):
                self.tick()
                pe = element(p)
                if pe.kind != "relation" or pe.key in out:
                    continue
                out[pe.key] = pe
                if recursive:
                    frontier.append(pe)
        return out

    # --- queries ------------------------------------------------------------

    def query(self, q: Query) -> ElementSet:
        kinds = QUERY_KINDS[q.element_type]
        filters = list(q.filters)
        has_bbox = any(isinstance(f, (BboxFilter, GlobalBboxFilter)) for f in filters)
        has_id = any(isinstance(f, IdFilter) for f in filters)
        if self.settings.bbox and not has_bbox and not has_id:
            # [bbox:] applies to every query that does not bring its own.
            filters.append(GlobalBboxFilter())
        filters = [self.resolve_bbox(f) for f in filters]

        sources: list[ElementSet] = [self.get(name) for name in q.input_sets]
        rest: list[Filter] = []
        for f in filters:
            if isinstance(f, IdFilter):
                sources.append(self.by_id(kinds, f.ids))
            elif isinstance(f, RecurseFilter):
                sources.append(self.recurse_filter(f, kinds))
            elif isinstance(f, PivotFilter):
                sources.append(self.pivot(f, kinds))
            else:
                rest.append(f)

        tag_filters = [f for f in rest if isinstance(f, TagFilter)]
        if_filters = [f for f in rest if isinstance(f, IfFilter)]
        matchers = _tag_matchers(tag_filters)
        if sources:
            passed: list[Element] = []
            for e in _intersect(sources).values():
                self.tick()
                if e.kind in kinds and all(m(e.tags) for m in matchers):
                    passed.append(e)
            spatial = [f for f in rest if not isinstance(f, (TagFilter, IfFilter))]
        else:
            passed, spatial = self.world_query(q.element_type, kinds, rest, tag_filters, matchers)
        # Spatial filters go over the survivors all at once (see
        # spatial_filter); only the if: conditions are left per element.
        for f in spatial:
            self.check_deadline()
            passed = self.spatial_filter(f, passed)
        out: ElementSet = {}
        for e in passed:
            self.tick()
            if all(_truthy(self.evaluate(f.expr, e)) for f in if_filters):
                out[e.key] = e
        return out

    def resolve_bbox(self, f: Filter) -> Filter:
        if not isinstance(f, GlobalBboxFilter):
            return f
        if self.settings.bbox is None:
            raise QueryRuntimeError(f"(bbox) at line {self.line} needs a [bbox:...] setting.")
        south, west, north, east = self.settings.bbox
        return BboxFilter(south=south, west=west, north=north, east=east)

    def world_query(
        self,
        element_type: str,
        kinds,
        filters: list[Filter],
        tag_filters: list[TagFilter],
        matchers: list[Callable[[dict[str, str]], bool]],
    ) -> tuple[list[Element], list[Filter]]:
        """The elements from the GOL that pass *tag_filters*.

        Returned with the spatial filters still to be tested: GeoDesk's bbox
        filter goes by bounding boxes, so a way whose box touches the bbox but
        whose line does not is let through and only the exact test drops it.
        around, poly and area are exact in GeoDesk, except an around over many
        centres, which goes by their extent.

        Tags are tested as the features stream in, so only the matches are
        held: a key regex over a whole country, which GOQL cannot narrow,
        walks every node there and keeps a handful.
        """
        selector = _goql_selector(GOQL_TYPES[element_type], [f for f in filters if isinstance(f, TagFilter)])
        queries = [self.world(selector)]
        recheck: list[Filter] = []
        for f in filters:
            if isinstance(f, BboxFilter):
                box = Box(west=f.west, south=f.south, east=f.east, north=f.north)
                queries = [q(box) for q in queries]
                recheck.append(f)
            elif isinstance(f, AroundFilter):
                centres = self.around_centres(f)
                if len(centres) <= AROUND_QUERIES_MAX:
                    queries = [q.around(c, meters=f.radius) for q in queries for c in centres]
                else:
                    # Thousands of centres would be thousands of GeoDesk
                    # queries. One over their common extent instead, and the
                    # exact distance test afterwards, against an index of them.
                    box = _extent(centres).buffer(meters=f.radius)
                    queries = [q(box) for q in queries]
                    recheck.append(f)
            elif isinstance(f, PolyFilter):
                poly = _mercator_polygon(f.points)
                queries = [q.intersecting(poly) for q in queries]
            elif isinstance(f, AreaFilter):
                areas = self.area_features(f)
                queries = [q.intersecting(a) for q in queries for a in areas]

        out: ElementSet = {}
        stop: Exception | None = None
        for q in queries:
            self.check_deadline()
            # Read to the end whatever happens: a GeoDesk iterator abandoned
            # half-way crashes the process. A timeout, a set grown too big or
            # an error only stop the work; the loop drains, then it is raised.
            # A whole-country scan passes millions of features through here,
            # most of them to be rejected: the tags are tested on a plain
            # dict first, and an Element is only made for a match.
            seen = 0
            for feature in q:
                if stop is not None:
                    continue
                try:
                    seen += 1
                    if seen % _TICK_EVERY == 0:
                        self.check_deadline()
                    tags = _tags_of(feature)
                    if not all(m(tags) for m in matchers):
                        continue
                    e = area_element(feature) if element_type == "area" else element(feature)
                    if e.kind in kinds and e.key not in out:
                        e._tags = tags
                        out[e.key] = e
                        if len(out) > self.max_elements:
                            self.check_size(out)
                except Exception as exc:  # noqa: BLE001 - re-raised after the drain
                    stop = exc
            if stop is not None:
                raise stop
        return list(out.values()), recheck

    def by_id(self, kinds, ids: list[int]) -> ElementSet:
        out: ElementSet = {}
        for i in ids:
            for kind in kinds:
                e = self.lookup(kind, i)
                if e is not None:
                    out[e.key] = e
        return out

    def lookup(self, kind: str, id: int) -> Element | None:
        """One element by type and id. Untagged nodes are not features in a GOL
        and cannot be found this way - only through the ways holding them."""
        try:
            if kind == "node":
                f = self.world.node(id)
            elif kind == "way":
                f = self.world.way(id)
            elif kind == "relation":
                f = self.world.relation(id)
            else:
                if id >= AREA_FROM_RELATION:
                    f = self.world.relation(id - AREA_FROM_RELATION)
                elif id >= AREA_FROM_WAY:
                    f = self.world.way(id - AREA_FROM_WAY)
                else:
                    return None
                return area_element(f) if f is not None and f.is_area else None
        except Exception:  # noqa: BLE001 - GeoDesk raises on ids it has no tile for
            return None
        return element(f) if f is not None else None

    def recurse_filter(self, f: RecurseFilter, kinds) -> ElementSet:
        source = self.get(f.input_set).values()
        out: ElementSet = {}
        if f.kind == "w":
            for e in source:
                if e.kind == "way":
                    self.add_way_nodes(e, out)
        elif f.kind == "r":
            for e in source:
                if e.kind != "relation":
                    continue
                for m in list(e.feature.members):
                    self.tick()
                    if f.role is None or m.role == f.role:
                        me = element(m)
                        out[me.key] = me
        else:
            child_kind = {"bn": "node", "bw": "way", "br": "relation"}[f.kind]
            for e in source:
                if e.kind != child_kind:
                    continue
                for p in list(e.feature.parents):
                    self.tick()
                    pe = element(p)
                    if f.role is not None and not _has_member(pe, e, f.role):
                        continue
                    out[pe.key] = pe
        return {k: e for k, e in out.items() if e.kind in kinds}

    def pivot(self, f: PivotFilter, kinds) -> ElementSet:
        out: ElementSet = {}
        for e in self.get(f.input_set).values():
            if e.kind != "area":
                continue
            pe = element(e.feature)
            if pe.kind in kinds:
                out[pe.key] = pe
        return out

    # --- filters, one element at a time --------------------------------------

    def spatial_filter(self, f: Filter, elements: list[Element]) -> list[Element]:
        """The elements that pass a spatial filter, decided for all of them at once.

        All at once because a query like ``node.h(around.w:100)`` puts a
        region's road nodes - over a million - against a set of centres, and
        per element the Python around each test (a shapely point, an index
        lookup) costs far more than the geometry: 18 s against well under one
        in bulk. Here the elements become one array of shapes and every test
        is one vectorised shapely call; only the few pairs that come close
        are measured exactly.
        """
        if not elements:
            return elements
        geoms = _geometries(elements)
        if isinstance(f, BboxFilter):
            mask = shapely.intersects(Box(west=f.west, south=f.south, east=f.east, north=f.north).shape, geoms)
        elif isinstance(f, PolyFilter):
            mask = shapely.intersects(_mercator_polygon(f.points), geoms)
        elif isinstance(f, AreaFilter):
            mask = np.zeros(len(geoms), dtype=bool)
            for area in self.area_features(f):
                shape = area.shape
                shapely.prepare(shape)
                mask |= shapely.intersects(shape, geoms)
        elif isinstance(f, AroundFilter):
            mask = self.around_mask(f, geoms)
        else:  # pragma: no cover
            raise TypeError(f)
        return [e for e, keep in zip(elements, mask) if keep]

    def around_mask(self, f: AroundFilter, geoms: np.ndarray) -> np.ndarray:
        mask = np.zeros(len(geoms), dtype=bool)
        centres = [_as_geometry(c) for c in self.around_centres(f)]
        if not centres:
            return mask
        tree = STRtree(centres)
        # The index is in Mercator, which stretches with latitude: search
        # with the radius as it is at the most poleward point involved, plus
        # a margin, and let GeoDesk's distance in metres decide the pairs
        # that turn up.
        extent = _extent(list(geoms) + centres)
        reach = to_mercator(meters=f.radius, lat=max(abs(extent.south), abs(extent.north))) * 1.05
        near, centre = tree.query(geoms, predicate="dwithin", distance=reach)
        for i, j in zip(near, centre):
            if not mask[i] and distance(geoms[i], centres[j], units="meters") <= f.radius:
                mask[i] = True
        return mask

    def around_centres(self, f: AroundFilter) -> list:
        """What around measures from, in the forms GeoDesk takes."""
        if f.input_set is None:
            if len(f.points) == 1:
                lat, lon = f.points[0]
                return [Coordinate(lon=lon, lat=lat)]
            return [to_mercator(LineString([(lon, lat) for lat, lon in f.points]))]
        out = []
        for e in self.get(f.input_set).values():
            if e.kind == "node":
                out.append(Coordinate(e.feature.x, e.feature.y))
            else:
                out.append(e.feature.shape)
        return out

    def area_features(self, f: AreaFilter) -> list:
        if f.area_id is not None:
            a = self.lookup("area", f.area_id)
            return [a.feature] if a else []
        return [e.feature for e in self.get(f.input_set).values() if e.kind == "area"]

    # --- if: expressions ------------------------------------------------------

    def evaluate(self, expr: Expr, e: Element) -> str:
        if isinstance(expr, Literal):
            return expr.value
        if isinstance(expr, TagValue):
            return e.tags.get(expr.key, "")
        if isinstance(expr, Ternary):
            branch = expr.then if _truthy(self.evaluate(expr.cond, e)) else expr.otherwise
            return self.evaluate(branch, e)
        if isinstance(expr, Unary):
            value = self.evaluate(expr.operand, e)
            if expr.op == "!":
                return _bool(not _truthy(value))
            n = _number(value)
            return _fmt(-n) if n is not None else "NaN"
        if isinstance(expr, Binary):
            return self.binary(expr, e)
        if isinstance(expr, Call):
            return self.call(expr, e)
        raise TypeError(expr)  # pragma: no cover

    def binary(self, expr: Binary, e: Element) -> str:
        op = expr.op
        left = self.evaluate(expr.left, e)
        if op == "||":
            return _bool(_truthy(left) or _truthy(self.evaluate(expr.right, e)))
        if op == "&&":
            return _bool(_truthy(left) and _truthy(self.evaluate(expr.right, e)))
        right = self.evaluate(expr.right, e)
        a, b = _number(left), _number(right)
        both = a is not None and b is not None
        if op in ("==", "!="):
            same = a == b if both else left == right
            return _bool(same if op == "==" else not same)
        if op in ("<", "<=", ">", ">="):
            x, y = (a, b) if both else (left, right)
            return _bool({"<": x < y, "<=": x <= y, ">": x > y, ">=": x >= y}[op])
        if op == "+" and not both:
            # Overpass concatenates when either side is not a number.
            return left + right
        if not both:
            return "NaN"
        if op == "+":
            return _fmt(a + b)
        if op == "-":
            return _fmt(a - b)
        if op == "*":
            return _fmt(a * b)
        return _fmt(a / b) if b else "NaN"

    def call(self, expr: Call, e: Element) -> str:
        name = expr.name
        args = [self.evaluate(a, e) for a in expr.args]
        f = e.feature
        if name == "count":
            kinds = COUNT_TYPES[args[0]]
            return str(sum(1 for el in self.get(DEFAULT_SET).values() if el.kind in kinds))
        if name == "id":
            return str(e.id)
        if name == "type":
            return e.kind
        if name == "is_tag":
            return _bool(args[0] in e.tags)
        if name == "count_tags":
            return str(len(e.tags))
        if name == "count_members":
            return str(len(list(f.members))) if e.kind == "relation" else "0"
        if name == "is_closed":
            if e.kind != "way":
                return "0"
            nodes = list(f.nodes)
            return _bool(len(nodes) > 1 and nodes[0].id == nodes[-1].id)
        if name == "length":
            return _fmt(f.length) if e.kind in ("way", "relation") else "0"
        if name in ("lat", "lon"):
            if e.kind == "node":
                return _fmt(f.lat if name == "lat" else f.lon)
            b = f.bounds
            return _fmt((b.south + b.north) / 2 if name == "lat" else (b.west + b.east) / 2)
        if name == "number":
            n = _number(args[0])
            return _fmt(n) if n is not None else "NaN"
        if name == "is_number":
            return _bool(_number(args[0]) is not None)
        if name == "suffix":
            m = _NUMBER.match(args[0])
            return args[0][m.end():] if m else ""
        nums = [_number(a) for a in args]
        if any(n is None for n in nums):
            return "NaN"
        if name == "abs":
            return _fmt(abs(nums[0]))
        if name == "min":
            return _fmt(min(nums))
        if name == "max":
            return _fmt(max(nums))
        raise TypeError(name)  # pragma: no cover - the parser checks names


# --- helpers ------------------------------------------------------------------


def _statement_name(st: Statement) -> str:
    if isinstance(st, Query):
        return "query"
    return {
        Union: "union",
        SetCopy: "item",
        Recurse: "recurse",
        Out: "print",
        ForEach: "foreach",
        MapToArea: "map-to-area",
        IsIn: "is-in",
    }[type(st)]


def _produces_set(st: Statement) -> bool:
    return not isinstance(st, (Out, ForEach))


def _intersect(sources: list[ElementSet]) -> ElementSet:
    first, *others = sources
    return {k: e for k, e in first.items() if all(k in s for s in others)}


def _has_member(parent: Element, child: Element, role: str) -> bool:
    if parent.kind != "relation":
        return False
    return any(m.id == child.id and m.osm_type == child.kind and m.role == role for m in list(parent.feature.members))


# Key patterns that match every key: [~".*"~"…"] asks about values only.
_ANY_KEY = {"", ".", ".*", ".+", "^.*", ".*$", "^.*$", "^.+$"}


def _tag_matchers(filters: list[TagFilter]) -> list[Callable[[dict[str, str]], bool]]:
    """One test per filter, the ones likeliest to reject first.

    A positive filter ([k], [k=v], a regex) is what narrows a query; the
    negations ([!k], [k!=v], [k!~re]) let nearly everything through. Run the
    negations first and a scan pays for all of them on every feature only to
    reject it at the last test - seven of them in a common shape of query.
    """
    order = {"=": 0, "exists": 1, "~": 2, "key~": 3, "!=": 4, "not_exists": 4, "!~": 5}
    return [_tag_matcher(f) for f in sorted(filters, key=lambda f: order[f.op])]


def _tag_matcher(f: TagFilter) -> Callable[[dict[str, str]], bool]:
    """A filter as a ready test on an element's tags.

    Built once per query, not per element: a scan asks it millions of times,
    and deciding the operator and finding the compiled regex each time cost
    as much as the test.
    """
    key, value, op = f.key, f.value, f.op
    if op == "exists":
        return lambda tags: key in tags
    if op == "not_exists":
        return lambda tags: key not in tags
    if op == "=":
        return lambda tags: tags.get(key) == value
    if op == "!=":
        return lambda tags: tags.get(key) != value
    flags = re.IGNORECASE if f.ignore_case else 0
    if op == "~":
        search = re.compile(value, flags).search
        return lambda tags: key in tags and search(tags[key]) is not None
    if op == "!~":
        search = re.compile(value, flags).search
        return lambda tags: key not in tags or search(tags[key]) is None
    # key~: some tag whose key matches the first regex and value the second.
    value_search = re.compile(value, flags).search
    if key in _ANY_KEY:
        return lambda tags: any(value_search(v) is not None for v in tags.values())
    key_search = re.compile(key, flags).search
    return lambda tags: any(key_search(k) is not None and value_search(v) is not None for k, v in tags.items())


def _goql_selector(types: str, filters: list[TagFilter]) -> str:
    """The GOQL query that narrows the candidates for *filters*.

    One clause at most, and never stricter than the filters: an equality on a
    plain string value if there is one, else a key that must be there. The
    exact test happens in _tag_matcher regardless; GeoDesk's index on
    the one key is where the speed comes from.
    """
    usable = [f for f in filters if not _GOQL_UNSAFE.search(f.key)]
    for f in usable:
        if f.op == "=" and f.value and not _GOQL_UNSAFE.search(f.value) and not _NUMBER.fullmatch(f.value):
            return f'{types}["{f.key}"="{f.value}"]'
    for f in usable:
        if f.op in ("exists", "=", "~"):
            # GOQL's [k] leaves out k=no, which Overpass's [k] keeps.
            return f'{types}["{f.key}"], {types}["{f.key}"="no"]'
    return types


def _geometry(e: Element):
    """The element's shape in GeoDesk's Mercator coordinates."""
    f = e.feature
    if e.kind == "node":
        return Point(f.x, f.y)
    return f.shape


def _as_geometry(centre):
    """A Coordinate as a shapely point; shapes pass through."""
    if isinstance(centre, Coordinate):
        return Point(centre.x, centre.y)
    return centre


def _extent(shapes: list) -> Box:
    minx, miny, maxx, maxy = shapely.total_bounds([_as_geometry(c) for c in shapes])
    return Box(minx=int(minx), miny=int(miny), maxx=int(maxx), maxy=int(maxy))


def _geometries(elements: list[Element]) -> np.ndarray:
    """The elements' shapes in GeoDesk's Mercator, as one array: nodes made in
    a single shapely.points call, which is what makes a million of them cheap."""
    out = np.empty(len(elements), dtype=object)
    nodes = [i for i, e in enumerate(elements) if e.kind == "node"]
    if nodes:
        xs = np.fromiter((elements[i].feature.x for i in nodes), dtype=np.float64, count=len(nodes))
        ys = np.fromiter((elements[i].feature.y for i in nodes), dtype=np.float64, count=len(nodes))
        out[nodes] = shapely.points(xs, ys)
    for i, e in enumerate(elements):
        if e.kind != "node":
            out[i] = e.feature.shape
    return out


def _mercator_polygon(points: list[tuple[float, float]]):
    return to_mercator(Polygon([(lon, lat) for lat, lon in points]))


def _number(value: str) -> float | None:
    if _NUMBER.fullmatch(value):
        return float(value)
    return None


def _fmt(n: float) -> str:
    if math.isnan(n) or math.isinf(n):
        return "NaN"
    if n == int(n) and abs(n) < 1e15:
        return str(int(n))
    return f"{n:.15g}"


def _bool(b: bool) -> str:
    return "1" if b else "0"


def _truthy(value: str) -> bool:
    n = _number(value)
    if n is not None:
        return n != 0
    return value != ""
