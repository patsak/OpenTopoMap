"""The parsed form of an Overpass QL script.

Plain dataclasses, nothing executable: ``overpass.parser`` builds them and
``overpass.evaluator`` walks them. Every statement keeps the line it started
on, because Overpass reports both parse and runtime errors by line and clients
(overpass-turbo, JOSM) show that number to the user.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_SET = "_"


# --- settings -----------------------------------------------------------------


@dataclass
class CsvFormat:
    fields: list[str]
    header: bool = True
    separator: str = "\t"


@dataclass
class Settings:
    output: str = "xml"  # xml | json | csv
    csv: CsvFormat | None = None
    timeout: int = 180
    maxsize: int | None = None
    # (south, west, north, east), the order Overpass itself uses everywhere.
    bbox: tuple[float, float, float, float] | None = None


# --- query filters ------------------------------------------------------------


@dataclass
class TagFilter:
    """One ``[...]`` clause.

    op is one of ``exists``, ``not_exists``, ``=``, ``!=``, ``~``, ``!~`` and
    ``key~`` (a regex on the key and one on the value: ``[~"^addr:"~"."]``).
    """

    op: str
    key: str
    value: str | None = None
    ignore_case: bool = False


@dataclass
class BboxFilter:
    south: float
    west: float
    north: float
    east: float


@dataclass
class GlobalBboxFilter:
    """``(bbox)``: the ``[bbox:...]`` setting, resolved when the query runs."""


@dataclass
class IdFilter:
    ids: list[int]


@dataclass
class AroundFilter:
    radius: float
    # Either a set whose elements are the centres, or a point / polyline as
    # (lat, lon) pairs.
    input_set: str | None = None
    points: list[tuple[float, float]] = field(default_factory=list)


@dataclass
class PolyFilter:
    points: list[tuple[float, float]]  # (lat, lon)


@dataclass
class AreaFilter:
    input_set: str | None = None
    area_id: int | None = None


@dataclass
class PivotFilter:
    input_set: str


@dataclass
class RecurseFilter:
    """``(w.a)``, ``(r.a:"role")``, ``(bn.a)``, ``(bw.a)``, ``(br.a)``."""

    kind: str  # w | r | bn | bw | br
    input_set: str
    role: str | None = None


@dataclass
class IfFilter:
    expr: "Expr"


Filter = (
    TagFilter
    | BboxFilter
    | GlobalBboxFilter
    | IdFilter
    | AroundFilter
    | PolyFilter
    | AreaFilter
    | PivotFilter
    | RecurseFilter
    | IfFilter
)


# --- evaluator expressions (if:) ----------------------------------------------


# The functions an ``if:`` may call, with their arity. The parser refuses the
# rest as a static error - the same answer Overpass gives for an unknown name -
# so a typo never reaches the evaluator as a silently false condition.
# count(<these>): the elements of a type in the set _ (Overpass's aggregate).
COUNT_TYPES = {
    "nodes": ("node",),
    "ways": ("way",),
    "relations": ("relation",),
    "areas": ("area",),
    "deriveds": (),
    "nwr": ("node", "way", "relation"),
    "nw": ("node", "way"),
    "wr": ("way", "relation"),
    "nr": ("node", "relation"),
}

FUNCTIONS = {
    "id": 0,
    "type": 0,
    "is_tag": 1,
    "count_tags": 0,
    "count_members": 0,
    "is_closed": 0,
    "length": 0,
    "lat": 0,
    "lon": 0,
    "number": 1,
    "is_number": 1,
    "suffix": 1,
    "abs": 1,
    "min": 2,
    "max": 2,
}


@dataclass
class Literal:
    value: str


@dataclass
class TagValue:
    key: str


@dataclass
class Call:
    name: str
    args: list["Expr"]


@dataclass
class Unary:
    op: str
    operand: "Expr"


@dataclass
class Binary:
    op: str
    left: "Expr"
    right: "Expr"


@dataclass
class Ternary:
    cond: "Expr"
    then: "Expr"
    otherwise: "Expr"


Expr = Literal | TagValue | Call | Unary | Binary | Ternary


# --- statements ---------------------------------------------------------------


@dataclass
class Query:
    """``node.a.b[amenity=pub](around:100,...)->.x;``"""

    line: int
    element_type: str  # node | way | relation | nwr | nw | nr | wr | area
    input_sets: list[str]
    filters: list[Filter]
    into: str = DEFAULT_SET


@dataclass
class Union:
    line: int
    # (negated, statement): a ``- stmt;`` member is subtracted from the result.
    members: list[tuple[bool, "Statement"]]
    into: str = DEFAULT_SET


@dataclass
class SetCopy:
    """``.a;`` or ``.a->.b;``"""

    line: int
    input_set: str
    into: str = DEFAULT_SET


@dataclass
class Recurse:
    line: int
    op: str  # > | >> | < | <<
    input_set: str = DEFAULT_SET
    into: str = DEFAULT_SET


@dataclass
class Out:
    line: int
    input_set: str = DEFAULT_SET
    verbosity: str = "body"  # ids | skel | body | tags | meta | count
    geometry: str | None = None  # geom | bb | center
    limit: int | None = None


@dataclass
class ForEach:
    line: int
    body: list["Statement"]
    input_set: str = DEFAULT_SET
    loop_set: str = DEFAULT_SET


@dataclass
class MapToArea:
    line: int
    input_set: str = DEFAULT_SET
    into: str = DEFAULT_SET


@dataclass
class IsIn:
    line: int
    # A coordinate, or (when None) the nodes of input_set.
    point: tuple[float, float] | None = None
    input_set: str = DEFAULT_SET
    into: str = DEFAULT_SET


Statement = Query | Union | SetCopy | Recurse | Out | ForEach | MapToArea | IsIn


@dataclass
class Script:
    settings: Settings
    statements: list[Statement]
