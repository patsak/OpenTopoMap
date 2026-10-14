"""Align DEM thalweg geometry with OSM waterways and deduplicate crossing lines."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

from routing.build.rivers import RiverSize
from routing.config import Config

log = logging.getLogger(__name__)

PARALLEL_MIN_DOT = 0.65
MEAN_DIST_FRAC = 0.85


@dataclass
class StreamMatchResult:
    """Per-chain geometry overrides and a deduplicated crossing list."""

    chain_xy: dict[int, np.ndarray] = field(default_factory=dict)
    tributary_end: dict[int, tuple[float, float]] = field(default_factory=dict)


def _chain_length_xy(xy: np.ndarray) -> float:
    return float(np.hypot(*np.diff(xy, axis=0).T).sum()) if len(xy) >= 2 else 0.0


def _align_parallel_chain(xy: np.ndarray, osm: LineString, max_mean_m: float) -> np.ndarray | None:
    """Replace *xy* with samples along *osm* between monotonic endpoint projections."""
    if len(xy) < 2 or osm.length < 10:
        return None
    dists = np.array([osm.distance(Point(p)) for p in xy])
    if float(dists.mean()) > max_mean_m or float(np.percentile(dists, 90)) > max_mean_m * 1.4:
        return None
    d_chain = xy[-1] - xy[0]
    d_chain /= max(np.linalg.norm(d_chain), 1e-9)
    coords = np.asarray(osm.coords)
    d_osm = coords[-1] - coords[0]
    d_osm /= max(np.linalg.norm(d_osm), 1e-9)
    if abs(float(np.dot(d_chain, d_osm))) < PARALLEL_MIN_DOT:
        return None
    t0 = osm.project(Point(xy[0]))
    t1 = osm.project(Point(xy[-1]))
    if abs(t1 - t0) < 15.0:
        return None
    n = len(xy)
    ts = np.linspace(t0, t1, n)
    out = np.array([osm.interpolate(t).coords[0] for t in ts], dtype=np.float64)
    return out


def _tributary_junction(xy: np.ndarray, osm: LineString, max_m: float) -> tuple[float, float] | None:
    """If *xy* meets *osm* roughly at right angles near its end, snap the end to the river."""
    if len(xy) < 3:
        return None
    end, prev = xy[-1], xy[-2]
    d_in = end - prev
    d_in /= max(np.linalg.norm(d_in), 1e-9)
    near = osm.interpolate(osm.project(Point(end)))
    if Point(end).distance(near) > max_m:
        return None
    coords = np.asarray(osm.coords)
    d_osm = coords[-1] - coords[0]
    d_osm /= max(np.linalg.norm(d_osm), 1e-9)
    if abs(float(np.dot(d_in, d_osm))) > 0.55:
        return None
    return float(near.x), float(near.y)


def match_chains(
    chains: list[np.ndarray],
    chain_xy_raw: dict[int, np.ndarray],
    osm_lines: list[tuple[LineString, dict]],
    rivers: RiverSize,
    cfg: Config,
    _ford_free: float,
    _ford_max_catch: float,
    _ford_max_width: float,
    match_m: float,
) -> StreamMatchResult:
    """Conservative OSM alignment for wadable thalweg runs."""
    result = StreamMatchResult()
    if not chains or not osm_lines:
        return result
    # Match against a whole mapped way, not independently against its 100 m
    # sizing pieces. On a bend adjacent pieces are close enough that choosing
    # the wrong one makes the two bank lines cut across the river.
    osm_candidates = [
        (line, float(rivers.of_line(np.asarray(line.coords))))
        for line, _tags in osm_lines
        if not line.is_empty and len(line.coords) >= 2
    ]
    if not osm_candidates:
        return result
    tree = STRtree(np.asarray([line for line, _ in osm_candidates], dtype=object))
    max_mean = match_m * MEAN_DIST_FRAC
    matched_osm: set[int] = set()
    for idx, chain in enumerate(chains):
        xy = chain_xy_raw[idx]
        if _chain_length_xy(xy) < 20:
            continue
        line = LineString(xy)
        hits = tree.query(line, predicate="dwithin", distance=match_m)
        best = None
        best_score = float("inf")
        for j in hits:
            osm, catch = osm_candidates[int(j)]
            aligned = _align_parallel_chain(xy, osm, max_mean)
            if aligned is None:
                junction = _tributary_junction(xy, osm, match_m)
                if junction is not None:
                    trimmed = xy.copy()
                    trimmed[-1] = junction
                    result.tributary_end[int(chain[-1])] = junction
                    result.chain_xy[idx] = trimmed
                continue
            score = float(osm.distance(line)) + abs(
                rivers.at(*xy[-1])[0] - catch) * 0.01
            if score < best_score:
                best_score = score
                best = (int(j), aligned)
        if best is not None:
            j, aligned = best
            result.chain_xy[idx] = aligned
            matched_osm.add(j)
    log.info("stream match: %s chains aligned, %s tributary ends, %s OSM pieces matched",
             len(result.chain_xy), len(result.tributary_end), len(matched_osm))
    return result


def canonical_crossings(
    chains: list[np.ndarray],
    chain_xy: dict[int, np.ndarray],
    wet_catch: dict[int, float],
    osm_wadable: list[tuple[LineString, float]],
    _matched_chain_indices: set[int],
    match_m: float,
) -> list[tuple[LineString, float]]:
    """One crossing line per wadable thalweg; unmatched OSM only when not redundant."""
    out: list[tuple[LineString, float]] = []
    for idx, catch in wet_catch.items():
        xy = chain_xy.get(idx)
        if xy is None:
            continue
        out.append((LineString(xy), catch))
    if not osm_wadable:
        return out
    dem_lines = np.asarray([line for line, _ in out], dtype=object)
    dem_catch = np.array([c for _, c in out])
    tree = STRtree(dem_lines) if len(dem_lines) else None
    for piece, catch in osm_wadable:
        if tree is not None:
            near = tree.query(piece, predicate="dwithin", distance=match_m)
            if len(near) and np.any((dem_catch[near] > catch / 2) & (dem_catch[near] < catch * 2)):
                continue
        out.append((piece, catch))
    return out


def chain_xy_map(
    chains: list[np.ndarray],
    raw_fn,
    match: StreamMatchResult,
) -> dict[int, np.ndarray]:
    """Final polyline per chain index."""
    out: dict[int, np.ndarray] = {}
    for i, chain in enumerate(chains):
        out[i] = match.chain_xy.get(i, raw_fn(chain))
    return out
