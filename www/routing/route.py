"""An engine's edge sequence -> the GeoJSON the service answers with.

    FeatureCollection
      [0] LineString, 3D (lon, lat, ele): the whole route, with distance_m,
          duration_s (with the backpack, pack_kg), ascent_m, descent_m, legs[]
          and waypoints[]
      [1..] LineString per stretch of the same kind of going: trail
          (an OSM way), offroad (with its terrain) or ford - so a client can
          draw the off-trail part dashed and the fords marked

Off trail - along a valley floor, a pass descent, a crest, across the
lattice, and the connectors between them - the drawn line is smoothed
(Chaikin corner cutting, SMOOTH_ROUNDS times): the D8 staircase, the
lattice's 60-degree zigzag and the hops between lines leave corners no one
walks. Trails and fords are drawn as mapped. Distances and times are
the edges' own, smoothing changes only the geometry.
"""

from __future__ import annotations

from typing import Callable

import numpy as np

from routing import model
from routing.engine.base import Path
from routing.package import Package


# The bank is deliberately offset only 10 m from water. Corner cutting it can
# draw the answer through the lake even though every graph edge stays dry.
SMOOTH_KINDS = frozenset({model.THALWEG, model.PASS, model.RIDGE, model.CONNECTOR, model.LATTICE})
SMOOTH_ROUNDS = 3
LATTICE_JITTER_MIN_M = 50.0
LATTICE_JITTER_MAX_M = 150.0
LATTICE_JITTER_WINDOW_M = 450.0


def _chaikin(xyz: np.ndarray, rounds: int) -> tuple[np.ndarray, np.ndarray]:
    """*xyz* with its corners cut, ends kept; and for every point its place
    along the original as a fractional vertex index."""
    t = np.arange(len(xyz), dtype=np.float64)
    return _chaikin_with_t(xyz, t, rounds)


def _chaikin_with_t(xyz: np.ndarray, t: np.ndarray, rounds: int) -> tuple[np.ndarray, np.ndarray]:
    """Chaikin corner cutting while preserving an existing source parameter."""
    for _ in range(rounds):
        if len(xyz) < 3:
            break
        a, b = xyz[:-1], xyz[1:]
        ta, tb = t[:-1], t[1:]
        q, r = 0.75 * a + 0.25 * b, 0.25 * a + 0.75 * b
        tq, tr = 0.75 * ta + 0.25 * tb, 0.25 * ta + 0.75 * tb
        mid = np.empty((2 * len(a), xyz.shape[1]))
        mid[0::2], mid[1::2] = q, r
        tmid = np.empty(2 * len(a))
        tmid[0::2], tmid[1::2] = tq, tr
        # the ends stay where they are: the first and last cut points go
        xyz = np.vstack([xyz[:1], mid[1:-1], xyz[-1:]])
        t = np.concatenate([t[:1], tmid[1:-1], t[-1:]])
    return xyz, t


def _point_segment_distance(point: np.ndarray, a: np.ndarray, b: np.ndarray) -> float:
    d = b - a
    dd = float(np.dot(d, d))
    if dd <= 1e-12:
        return float(np.linalg.norm(point - a))
    u = float(np.clip(np.dot(point - a, d) / dd, 0.0, 1.0))
    return float(np.linalg.norm(point - (a + u * d)))


def _remove_short_detours(xyz: np.ndarray, t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Shortcut lattice bumps 50--150 m wide within a 450 m path window.

    This changes only the drawn geometry. The graph edges still determine
    distance, time and barriers; endpoints of a lattice run are never moved.
    """
    if len(xyz) < 3:
        return xyz, t
    xy = xyz[:, :2]
    keep = [0]
    i = 0
    while i < len(xy) - 2:
        best = None
        path_m = 0.0
        for k in range(i + 1, len(xy)):
            path_m += float(np.linalg.norm(xy[k] - xy[k - 1]))
            if path_m > LATTICE_JITTER_WINDOW_M:
                break
            if k < i + 2:
                continue
            chord_m = float(np.linalg.norm(xy[k] - xy[i]))
            deviation = max(_point_segment_distance(xy[m], xy[i], xy[k]) for m in range(i + 1, k))
            saving = path_m - chord_m
            if LATTICE_JITTER_MIN_M <= deviation <= LATTICE_JITTER_MAX_M and saving >= 25.0:
                best = k
        if best is None:
            i += 1
            keep.append(i)
        else:
            i = best
            keep.append(i)
    if keep[-1] != len(xyz) - 1:
        keep.append(len(xyz) - 1)
    keep = np.unique(np.asarray(keep, dtype=np.int64))
    return xyz[keep], t[keep]


def smooth_steps(lines: list[np.ndarray], smooth: list[bool], rounds: int = SMOOTH_ROUNDS,
                 lattice: list[bool] | None = None) -> list[np.ndarray]:
    """Each step's (lon, lat, ele) polyline, every run of consecutive *smooth*
    steps smoothed as one line: the run's ends stay, the joins inside it move
    with the curve, and consecutive steps still share their joining point.
    Short local detours are removed only from runs marked *lattice*."""
    if lattice is None:
        lattice = [False] * len(lines)
    out = list(lines)
    i = 0
    while i < len(lines):
        if not smooth[i]:
            i += 1
            continue
        j = i
        while j < len(lines) and smooth[j]:
            j += 1
        run = [lines[i]] + [line[1:] for line in lines[i + 1:j]]
        bounds = np.cumsum([0] + [len(line) - 1 for line in lines[i:j]])
        joined = np.vstack(run)
        # cut corners in metres, not degrees
        k = np.cos(np.radians(float(joined[:, 1].mean())))
        scale = np.array([111_320.0 * k, 111_320.0, 1.0])
        flat = joined * scale
        t = np.arange(len(joined), dtype=np.float64)
        a = i
        while a < j:
            if not lattice[a]:
                a += 1
                continue
            b = a + 1
            while b < j and lattice[b]:
                b += 1
            lo, hi = bounds[a - i], bounds[b - i]
            at = np.flatnonzero((t >= lo) & (t <= hi))
            if len(at) >= 3:
                reduced, reduced_t = _remove_short_detours(flat[at], t[at])
                flat = np.vstack([flat[:at[0]], reduced, flat[at[-1] + 1:]])
                t = np.concatenate([t[:at[0]], reduced_t, t[at[-1] + 1:]])
            a = b
        cut, t = _chaikin_with_t(flat, t, rounds)
        cut = cut / scale
        for n in range(j - i):
            lo, hi = bounds[n], bounds[n + 1]
            ends = [np.interp(lo, t, cut[:, c]) for c in range(3)], [np.interp(hi, t, cut[:, c]) for c in range(3)]
            inner = cut[(t > lo) & (t < hi)]
            out[i + n] = np.vstack([ends[0], inner, ends[1]])
        i = j
    return out


def going(kind: int) -> str:
    if kind == model.OSM:
        return "trail"
    if kind == model.FORD:
        return "ford"
    return "offroad"


def _climb(ele: np.ndarray) -> tuple[float, float]:
    if len(ele) < 2:
        return 0.0, 0.0
    d = np.diff(ele.astype(np.float64))
    return float(d[d > 0].sum()), float(-d[d < 0].sum())


def to_geojson(pkg: Package, path: Path, area: str, pack_kg: float = 0.0,
               times: tuple[np.ndarray, np.ndarray] | None = None,
               edge_time: Callable[[int, bool], float] | None = None) -> dict:
    """*pkg*: a graph package or a compiled graph (routing.engine.disk) - what
    the route is read from. The seconds of each step come from *edge_time*
    (an engine's, with the backpack), else *times* (both-way arrays), else the
    package's own times."""
    if edge_time is None:
        time_fwd, time_bwd = times if times is not None else (pkg.time_fwd, pkg.time_bwd)

        def edge_time(i: int, forward: bool) -> float:
            return float(time_fwd[i] if forward else time_bwd[i])
    coords: list[np.ndarray] = []
    stretches: list[dict] = []
    legs = []
    total_len = total_time = 0.0

    def append(chunk: np.ndarray) -> None:
        if coords and len(coords[-1]) and np.allclose(coords[-1][-1, :2], chunk[0, :2]):
            chunk = chunk[1:]
        if len(chunk):
            coords.append(chunk)

    # A waypoint is a hard boundary: smoothing across it can cut the corner
    # away from the requested point, especially where the next leg doubles
    # back along the only approach to a mountain lake.
    drawn_legs = []
    for leg in path.legs:
        steps = leg.edges
        drawn_legs.append(iter(smooth_steps(
            [pkg.edge_coords(i, forward) for i, forward in steps],
            [int(pkg.edge_kind[i]) in SMOOTH_KINDS for i, _ in steps],
            lattice=[int(pkg.edge_kind[i]) == model.LATTICE for i, _ in steps],
        )))
    for leg, drawn in zip(path.legs, drawn_legs):
        leg_len = leg_time = 0.0
        for i, forward in leg.edges:
            line = next(drawn)
            append(line)
            length = float(pkg.edge_len[i])
            seconds = edge_time(i, forward)
            leg_len += length
            leg_time += seconds
            kind = going(int(pkg.edge_kind[i]))
            terrain = model.TERRAINS[int(pkg.edge_terrain[i])] if kind == "offroad" else None
            last = stretches[-1] if stretches else None
            if last and last["going"] == kind and last["terrain"] == terrain:
                if not np.allclose(last["coords"][-1][-1, :2], line[0, :2]):
                    line = np.vstack([last["coords"][-1][-1:], line])
                last["coords"].append(line[1:])
                last["distance_m"] += length
                last["duration_s"] += seconds
            else:
                stretches.append({"going": kind, "terrain": terrain, "coords": [line],
                                  "distance_m": length, "duration_s": seconds})
        legs.append({"distance_m": round(leg_len, 1), "duration_s": round(leg_time, 1)})
        total_len += leg_len
        total_time += leg_time

    if coords:
        line = np.vstack(coords)
    else:  # every point snapped to the same node
        node = path.snaps[0].node
        line = np.array([[pkg.node_lon[node], pkg.node_lat[node], pkg.node_ele[node]]] * 2)
    ascent, descent = _climb(line[:, 2])

    def rounded(a: np.ndarray) -> list:
        return [[round(float(x), 6), round(float(y), 6), round(float(z), 1)] for x, y, z in a]

    waypoints = [
        {
            "input": [s.lon, s.lat],
            "snapped": [round(float(pkg.node_lon[s.node]), 6), round(float(pkg.node_lat[s.node]), 6)],
            "snap_distance_m": round(s.distance_m, 1),
        }
        for s in path.snaps
    ]
    offroad = sum(s["distance_m"] for s in stretches if s["going"] == "offroad")
    features = [{
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": rounded(line)},
        "properties": {
            "area": area,
            "pack_kg": pack_kg,
            "distance_m": round(total_len, 1),
            "duration_s": round(total_time, 1),
            "ascent_m": round(ascent, 1),
            "descent_m": round(descent, 1),
            "offroad_m": round(offroad, 1),
            "legs": legs,
            "waypoints": waypoints,
            "passes": [
                {"osm_id": int(pkg.pass_osm[i]), "name": (pkg.meta.get("pass_names") or [""] * (i + 1))[i],
                 "grade": model.GRADES[int(pkg.pass_grade[i])] if int(pkg.pass_grade[i]) < model.UNRATED else None,
                 "lon": round(float(pkg.pass_lon[i]), 6), "lat": round(float(pkg.pass_lat[i]), 6)}
                for i in path.passes
            ],
        },
    }]
    for s in stretches:
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": rounded(np.vstack(s["coords"]))},
            "properties": {
                "going": s["going"],
                "terrain": s["terrain"],
                "distance_m": round(s["distance_m"], 1),
                "duration_s": round(s["duration_s"], 1),
            },
        })
    return {"type": "FeatureCollection", "features": features}
