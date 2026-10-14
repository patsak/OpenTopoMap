"""What every engine takes and gives back.

An engine finds the edge sequence of each leg; turning it into a route -
geometry, distance, time, climb, the trail/off-trail/ford breakdown - is the
same for all of them (:mod:`routing.route`), read from the graph the engine
routes on (``Engine.graph``: edge_coords, edge_len, edge_kind, edge_terrain).
routing.graphstore picks the engine (OTM_ROUTING_ENGINE) from ENGINES there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from routing.config import PackParams

# A point further than this from the nearest usable node is not on the map.
MAX_SNAP_M = 500.0


class RouteError(Exception):
    """A request that cannot be routed, with what to tell the caller."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Options:
    allow_offroad: bool = True
    # A backpack this heavy, weighed with these coefficients (routing.pack).
    pack_kg: float = 0.0
    pack: PackParams = field(default_factory=PackParams)
    # Passes (routing.passlimit): none harder than max_pass_grade (a
    # model.GRADES index), at most max_top_passes of exactly that grade.
    max_pass_grade: int | None = None
    max_top_passes: int | None = None
    allow_unrated: bool = True


@dataclass
class Snap:
    lon: float
    lat: float
    node: int
    distance_m: float


@dataclass
class Leg:
    # (edge index, walked from edge_u to edge_v)
    edges: list[tuple[int, bool]] = field(default_factory=list)


@dataclass
class Path:
    snaps: list[Snap]
    legs: list[Leg]
    passes: list[int] = field(default_factory=list)  # indices into the graph's pass table


class Engine:
    name = "base"
    graph: object

    def route(self, points: list[tuple[float, float]], options: Options) -> Path:
        raise NotImplementedError

    def edge_time(self, path: Path, options: Options) -> Callable[[int, bool], float]:
        """Seconds for each step of *path* (the backpack included), keyed by
        (edge, forward)."""
        raise NotImplementedError
