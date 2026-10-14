"""Limiting the passes a route may cross, by their rtsa_scale category.

    max_pass_grade   no pass harder than this (model.GRADES index)
    max_top_passes   at most this many passes of exactly that category
    allow_unrated    whether passes without a category may be crossed

Going over a pass is going over the ridge within ~200 m of its point
(routing.build.graph._cross_pass), on any edge however gentle: the category
is for what the DEM does not show. The first limit is a plain filter: an edge
that goes over a pass too hard, or needs one (its descents, zone edges only
its zone makes walkable - routing.build.pipeline edge_pass), is not searched.
The second is not something one shortest-path search can keep track of, so it
is met greedily: route; while more top-category passes are crossed than allowed,
forbid the one whose loss costs the route least, and route again.
"""

from __future__ import annotations

from typing import Callable

import numpy as np

from routing import model
from routing.engine.base import Options, Path, RouteError

MAX_ROUNDS = 8


def allowed_mask(pass_grade: np.ndarray, options: Options) -> np.ndarray:
    """Per pass: may the route cross it at all."""
    grade = np.asarray(pass_grade)
    allowed = np.ones(len(grade), dtype=bool)
    unrated = grade == model.UNRATED
    if options.max_pass_grade is not None:
        allowed &= unrated | (grade <= options.max_pass_grade)
    if not options.allow_unrated:
        allowed &= ~unrated
    return allowed


def passes_on(edge_pass_of_steps: np.ndarray) -> list[int]:
    """Distinct passes along a route, in the order first met."""
    seen: list[int] = []
    for p in np.asarray(edge_pass_of_steps).tolist():
        if p >= 0 and p not in seen:
            seen.append(p)
    return seen


def route(route_fn: Callable[[np.ndarray], Path], weight_fn: Callable[[Path], float],
          pass_grade: np.ndarray, options: Options) -> Path:
    """The cheapest route route_fn finds within the limits.

    *route_fn(allowed)* routes with the passes of *allowed* open and fills in
    ``Path.passes``; *weight_fn* tells what a route costs.
    """
    allowed = allowed_mask(pass_grade, options)
    path = route_fn(allowed)
    top, limit = options.max_pass_grade, options.max_top_passes
    if top is None or limit is None:
        return path
    for _ in range(MAX_ROUNDS):
        used = [p for p in path.passes if pass_grade[p] == top]
        if len(used) <= limit:
            return path
        best = None
        for p in used:
            trial = allowed.copy()
            trial[p] = False
            try:
                alt = route_fn(trial)
            except RouteError:
                continue
            cost = weight_fn(alt)
            if best is None or cost < best[0]:
                best = (cost, alt, trial)
        if best is None:
            break
        _, path, allowed = best
    grade = model.GRADES[top]
    raise RouteError(f"no route crosses at most {limit} pass(es) of category {grade} "
                     f"and none harder; allow more, or a harder category")
