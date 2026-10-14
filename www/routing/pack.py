"""Walking with a backpack: edge times and weights for a given load.

A load changes how long every piece of an edge takes, and changes it more
where the walk is hard already. Per 10 kg (config ``pack``) a piece takes
longer by

    flat                       on any ground: slight, a good trail barely notices
  + climb   x gradient /10%    climbing: the most
  + descent x gradient /10%    descending: a little less
  + rough   x roughness        hard ground: 1 - the factor it slows the walk by
                               (terrain, side slope, a difficult trail)

The build stores each edge's time split along those lines (``climb_*``,
``descent_*``, ``rough_*``: time-weighted sums over its pieces), so the
whole thing is linear and is evaluated per request with no rebuild - by the
disk engine's A* as it relaxes each arc (the same formulas, in numba), and
here for the rest:

    time(kg) = time + kg/10 x (flat x time + climb x C + descent x D + rough x R)

The routing weight scales with the time of its own edge; the seconds of weight
that are not walking (wading, stepping off a trail) stay as they were. The
steep-descent weight (``steep_*``, routing.cost) grows on top of that, by
steep_descent_per_10kg of itself per 10 kg: a heavy pack down a long steep
slope is what wrecks knees and ankles.

    weight(kg) = (weight - extra) x time(kg) / time + extra + kg/10 x steep_descent x S

A load only ever adds, so the weight never drops below the unloaded time and the A*
heuristic stays admissible.
"""

from __future__ import annotations

import numpy as np

from routing.config import PackParams
from routing.package import Package


def per_10kg_time(t, climb, descent, rough, params: PackParams) -> np.ndarray:
    """Seconds 10 kg add: time(kg) = t + kg/10 x this."""
    t = np.asarray(t)
    return (params.flat_per_10kg * np.where(np.isfinite(t), t, 0.0)
            + params.climb_per_10kg * np.asarray(climb)
            + params.descent_per_10kg * np.asarray(descent)
            + params.rough_per_10kg * np.asarray(rough))


def per_10kg_weight(w, t, pack_t, extra, steep, params: PackParams) -> np.ndarray:
    """Weight 10 kg add: weight(kg) = w + kg/10 x this. The walking part of
    the weight (w - extra) scales with the time, the steep-descent part grows
    on top; *pack_t* is per_10kg_time()."""
    t = np.asarray(t)
    with np.errstate(invalid="ignore", divide="ignore"):
        share = np.where((t > 0) & np.isfinite(t), np.asarray(pack_t) / np.where(t > 0, t, 1), 0.0)
    return (np.asarray(w) - np.asarray(extra)) * share + params.steep_descent_per_10kg * np.asarray(steep)


def loaded_time(t, climb, descent, rough, params: PackParams, kg: float) -> np.ndarray:
    """Seconds carrying *kg*, from the unloaded seconds *t* and their split."""
    t = np.asarray(t, dtype=np.float64)
    if kg <= 0:
        return t
    return t + kg / 10.0 * per_10kg_time(t, climb, descent, rough, params)


def loaded_weight(w, t, t_loaded, extra, steep, params: PackParams, kg: float) -> np.ndarray:
    """Routing weight carrying *kg*: *w* the unloaded weight, *t* and
    *t_loaded* the time without and with the load, *extra* the seconds of
    weight that are not walking, *steep* the steep-descent part."""
    w = np.asarray(w, dtype=np.float64)
    if kg <= 0:
        return w
    t = np.asarray(t, dtype=np.float64)
    pack_t = (np.asarray(t_loaded, np.float64) - t) * 10.0 / kg
    return w + kg / 10.0 * per_10kg_weight(w, t, pack_t, extra, steep, params)


def times(pkg: Package, params: PackParams, kg: float) -> tuple[np.ndarray, np.ndarray]:
    """Both-way edge times of a package, in seconds, carrying *kg*."""
    if kg <= 0:
        return pkg.time_fwd, pkg.time_bwd
    return (loaded_time(pkg.time_fwd, pkg.climb_fwd, pkg.descent_fwd, pkg.rough_fwd, params, kg),
            loaded_time(pkg.time_bwd, pkg.climb_bwd, pkg.descent_bwd, pkg.rough_bwd, params, kg))


def weights(pkg: Package, params: PackParams, kg: float,
            loaded: tuple[np.ndarray, np.ndarray] | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Both-way routing weights of a package carrying *kg* (*loaded*: times() already done)."""
    if kg <= 0:
        return pkg.weight_fwd, pkg.weight_bwd
    tf, tb = loaded or times(pkg, params, kg)
    return (loaded_weight(pkg.weight_fwd, pkg.time_fwd, tf, pkg.edge_extra, pkg.steep_fwd, params, kg),
            loaded_weight(pkg.weight_bwd, pkg.time_bwd, tb, pkg.edge_extra, pkg.steep_bwd, params, kg))
