"""The small-memory engine: A* over a compiled graph read through mmap.

routing.compile lays a graph out as .npy arrays - CSR arcs with their costs,
nodes in snapping-cell order. Here they are opened with mmap_mode="r", so the
process holds only what a search touches plus its own working arrays (a
float32 and an int32 per node of the graph, a byte for "settled"): about 80
MB per search on the Caucasus, whatever the size of the files.

The search is A* in numba. The backpack and trails-only are applied while
arcs are relaxed - a load reweighs every arc it meets (routing.pack's
formulas, folded by routing.compile into one number an arc: weight + kg/10 x
pack_w), so a heavy pack changes the route, not just its time.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import numpy as np
from numba import njit

from routing import model
from routing.compile import ARC_HAS_PASS
from routing.cost import V_MAX_MS
from routing.engine.base import MAX_SNAP_M, Engine, Leg, Options, Path as RoutePath, RouteError, Snap
from routing.grid import Projector

log = logging.getLogger(__name__)

PREFER_TRAIL_M = 50.0
# a node of a big component is preferred if at most this much further than
# the nearest node: off a ledge, not across a cliff or a river
MAIN_SLACK_M = 200.0
MAX_SETTLED = 30_000_000
USABLE, ON_TRAIL, MAIN = 1, 2, 4


@njit(cache=True, nogil=True)
def _push(hk, hn, size, key, node):
    if size == hk.shape[0]:
        nk = np.empty(hk.shape[0] * 2, np.float32)
        nn = np.empty(hn.shape[0] * 2, np.int32)
        nk[:size] = hk
        nn[:size] = hn
        hk, hn = nk, nn
    i = size
    hk[i] = key
    hn[i] = node
    while i > 0:
        p = (i - 1) >> 1
        if hk[p] <= hk[i]:
            break
        hk[p], hk[i] = hk[i], hk[p]
        hn[p], hn[i] = hn[i], hn[p]
        i = p
    return hk, hn, size + 1


@njit(cache=True, nogil=True)
def _pop(hk, hn, size):
    node = hn[0]
    size -= 1
    hk[0] = hk[size]
    hn[0] = hn[size]
    i = 0
    while True:
        left = 2 * i + 1
        if left >= size:
            break
        c = left
        if left + 1 < size and hk[left + 1] < hk[left]:
            c = left + 1
        if hk[i] <= hk[c]:
            break
        hk[c], hk[i] = hk[i], hk[c]
        hn[c], hn[i] = hn[i], hn[c]
        i = c
    return node, size


@njit(cache=True, nogil=True)
def _arc_pass(pass_arc, pass_need, a):
    """The pass arc *a* needs (its kind has ARC_HAS_PASS set), -1 for none."""
    lo, hi = 0, pass_arc.shape[0]
    while lo < hi:
        mid = (lo + hi) >> 1
        if pass_arc[mid] < a:
            lo = mid + 1
        else:
            hi = mid
    return pass_need[lo] if lo < pass_arc.shape[0] and pass_arc[lo] == a else -1


@njit(cache=True, nogil=True)
def _astar(indptr, head, a_weight, a_pack_w, a_kind, pass_arc, pass_need, allowed,
           nx, ny, src, dst, offroad, k, inv_vmax, max_settled):
    n = indptr.shape[0] - 1
    g = np.full(n, np.inf, np.float32)
    parent = np.full(n, -1, np.int32)
    closed = np.zeros(n, np.uint8)
    hk = np.empty(1 << 16, np.float32)
    hn = np.empty(1 << 16, np.int32)
    size = 0
    tx = nx[dst]
    ty = ny[dst]
    g[src] = 0.0
    hk, hn, size = _push(hk, hn, size, np.float32(0.0), np.int32(src))
    settled = 0
    while size > 0:
        u, size = _pop(hk, hn, size)
        if closed[u]:
            continue
        closed[u] = 1
        settled += 1
        if u == dst or settled > max_settled:
            break
        gu = g[u]
        for a in range(indptr[u], indptr[u + 1]):
            kind = a_kind[a]
            if not offroad and (kind & 0x7F) != 0:  # 0 = osm
                continue
            if kind & 0x80:
                p = _arc_pass(pass_arc, pass_need, a)
                if p >= 0 and not allowed[p]:  # a pass the route may not cross
                    continue
            v = head[a]
            if closed[v]:
                continue
            # the backpack, folded in by routing.compile: linear in the load
            ng = gu + a_weight[a] + k * a_pack_w[a]
            if ng < g[v]:
                g[v] = ng
                parent[v] = a
                dx = nx[v] - tx
                dy = ny[v] - ty
                hk, hn, size = _push(hk, hn, size, np.float32(ng + math.sqrt(dx * dx + dy * dy) * inv_vmax),
                                     np.int32(v))
    if not closed[dst]:
        return np.zeros(0, np.int64), settled
    path = []
    v = dst
    while v != src:
        a = parent[v]
        path.append(a)
        # the arc's tail: the node whose arc range holds it
        lo = 0
        hi = n
        while hi - lo > 1:
            mid = (lo + hi) >> 1
            if indptr[mid] <= a:
                lo = mid
            else:
                hi = mid
        v = lo
    out = np.empty(len(path), np.int64)
    for i in range(len(path)):
        out[i] = path[len(path) - 1 - i]
    return out, settled


class DiskGraph:
    """A compiled graph, memory-mapped; reads like a package where
    routing.route needs it to."""

    ARRAYS = ("node_x", "node_y", "node_ele", "node_kind", "node_flags",
              "cell_start", "arc_indptr", "arc_head", "arc_ref", "arc_kind", "arc_time", "arc_weight",
              "arc_pack_t", "arc_pack_w", "pass_arc", "pass_need", "pass_over",
              "pass_osm", "pass_grade", "pass_lon", "pass_lat", "edge_u", "edge_v", "edge_len",
              "edge_kind", "edge_terrain", "geom_off", "geom_lon", "geom_lat", "geom_ele")

    def __init__(self, path: Path):
        self.path = path
        self.meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
        if self.meta.get("schema") != model.SCHEMA:
            from routing.package import PackageError

            raise PackageError(f"{path} was compiled from schema {self.meta.get('schema')}, "
                               f"this code reads {model.SCHEMA}; rebuild and recompile it")
        for name in self.ARRAYS:
            setattr(self, name, np.load(path / f"{name}.npy", mmap_mode="r"))
        self.projector = Projector(self.epsg)
        # lon/lat of a node, worked out when asked: not stored (routing.compile)
        self.node_lon = _Degrees(self, 0)
        self.node_lat = _Degrees(self, 1)

    def passes_over(self, arcs: np.ndarray) -> np.ndarray:
        """The pass each of *arcs* goes over, -1 for none."""
        arcs = np.asarray(arcs, dtype=np.int64)
        out = np.full(len(arcs), -1, dtype=np.int32)
        flagged = (np.asarray(self.arc_kind[arcs]) & ARC_HAS_PASS) > 0
        if flagged.any():
            at = np.searchsorted(self.pass_arc, arcs[flagged])
            out[flagged] = np.asarray(self.pass_over)[at]
        return out

    @property
    def epsg(self) -> int:
        return int(self.meta["epsg"])

    @property
    def node_count(self) -> int:
        return len(self.node_x)

    def edge_coords(self, i: int, forward: bool = True) -> np.ndarray:
        lo, hi = int(self.geom_off[i]), int(self.geom_off[i + 1])
        if hi > lo:
            coords = np.column_stack([self.geom_lon[lo:hi], self.geom_lat[lo:hi], self.geom_ele[lo:hi]])
        else:
            ends = [int(self.edge_u[i]), int(self.edge_v[i])]
            lon, lat = self.projector.to_lonlat(np.asarray(self.node_x[ends], np.float64),
                                                np.asarray(self.node_y[ends], np.float64))
            coords = np.column_stack([lon, lat, np.asarray(self.node_ele[ends], np.float64)])
        return coords if forward else coords[::-1]


class _Degrees:
    """graph.node_lon / node_lat: indexed like an array, projected from node_x/y."""

    def __init__(self, graph: DiskGraph, axis: int):
        self.graph, self.axis = graph, axis

    def __getitem__(self, idx):
        g = self.graph
        x = np.asarray(g.node_x[idx], np.float64)
        y = np.asarray(g.node_y[idx], np.float64)
        out = g.projector.to_lonlat(np.atleast_1d(x), np.atleast_1d(y))[self.axis]
        return float(out[0]) if np.ndim(x) == 0 else np.asarray(out)

    def __len__(self) -> int:
        return len(self.graph.node_x)


class DiskEngine(Engine):
    name = "disk"

    def __init__(self, graph: DiskGraph):
        self.graph = graph
        self.projector = Projector(graph.epsg)
        snap = graph.meta["snap"]
        self._x0, self._y0, self._cell = snap["x0"], snap["y0"], snap["cell"]
        self._ncx, self._ncy = snap["ncx"], snap["ncy"]

    # -------------------------------------------------------------- snapping

    def _candidates(self, x: float, y: float, radius: float) -> np.ndarray:
        g = self.graph
        cx = int((x - self._x0) // self._cell)
        cy = int((y - self._y0) // self._cell)
        reach = int(math.ceil(radius / self._cell))
        ranges = []
        for yy in range(max(0, cy - reach), min(self._ncy, cy + reach + 1)):
            lo_c = yy * self._ncx + max(0, cx - reach)
            hi_c = yy * self._ncx + min(self._ncx - 1, cx + reach)
            if hi_c >= lo_c:
                ranges.append((int(g.cell_start[lo_c]), int(g.cell_start[hi_c + 1])))
        if not ranges:
            return np.zeros(0, np.int64)
        return np.concatenate([np.arange(a, b) for a, b in ranges])

    def snap(self, lon: float, lat: float, allow_offroad: bool) -> Snap:
        g = self.graph
        x, y = self.projector.to_xy([lon], [lat])
        x, y = float(x[0]), float(y[0])
        cand = self._candidates(x, y, MAX_SNAP_M)
        if cand.size:
            flags = np.asarray(g.node_flags[cand])
            d = np.hypot(np.asarray(g.node_x[cand]) - x, np.asarray(g.node_y[cand]) - y)
            usable = (flags & USABLE) > 0
            # a node of a big component if there is one in reach: the nearest
            # node may sit on an island of the graph (a ledge among cliffs)
            nearest = d[usable].min() if usable.any() else np.inf
            main = usable & ((flags & MAIN) > 0) & (d <= min(MAX_SNAP_M, nearest + MAIN_SLACK_M))
            if main.any():
                usable = main
            trail = usable & ((flags & ON_TRAIL) > 0)
            best = None
            if trail.any():
                j = np.argmin(np.where(trail, d, np.inf))
                best = (float(d[j]), int(cand[j]))
            if allow_offroad and usable.any():
                j = np.argmin(np.where(usable, d, np.inf))
                if best is None or (best[0] > PREFER_TRAIL_M and d[j] < best[0]):
                    best = (float(d[j]), int(cand[j]))
            if best is not None and best[0] <= MAX_SNAP_M:
                return Snap(lon, lat, best[1], best[0])
        where = "a trail" if not allow_offroad else "the graph"
        raise RouteError(f"point {lon:.5f},{lat:.5f} is more than {MAX_SNAP_M:.0f} m from {where}")

    # --------------------------------------------------------------- routing

    def _search(self, src: int, dst: int, options: Options, allowed: np.ndarray) -> np.ndarray:
        g = self.graph
        if allowed.size == 0:
            allowed = np.ones(1, dtype=bool)  # numba wants a typed array even with no passes
        arcs, settled = _astar(
            g.arc_indptr, g.arc_head, g.arc_weight, g.arc_pack_w, g.arc_kind, g.pass_arc, g.pass_need,
            allowed, g.node_x, g.node_y, src, dst, options.allow_offroad,
            float(options.pack_kg) / 10.0, 1.0 / V_MAX_MS, MAX_SETTLED,
        )
        log.debug("A* %s -> %s: %s nodes settled", src, dst, settled)
        return arcs

    def route(self, points: list[tuple[float, float]], options: Options) -> RoutePath:
        from routing import passlimit

        snaps = [self.snap(lon, lat, options.allow_offroad) for lon, lat in points]

        def legs_with(allowed: np.ndarray) -> RoutePath:
            legs = []
            for k, (a, b) in enumerate(zip(snaps[:-1], snaps[1:])):
                if a.node == b.node:
                    legs.append(Leg())
                    continue
                arcs = self._search(a.node, b.node, options, allowed)
                if arcs.size == 0:
                    raise RouteError(
                        f"no way from point {k + 1} to point {k + 2}: they are cut off from each "
                        "other (a river too big to ford, a cliff or a canyon, a pass the limits "
                        "rule out, or no trail when off-trail walking is off)"
                    )
                refs = np.asarray(self.graph.arc_ref[arcs])
                leg = Leg(edges=[(int(r // 2), bool(r % 2 == 0)) for r in refs])
                leg.arcs = arcs  # type: ignore[attr-defined]
                legs.append(leg)
            arcs_all = [leg.arcs for leg in legs if hasattr(leg, "arcs")]
            steps = self.graph.passes_over(np.concatenate(arcs_all)) if arcs_all else np.zeros(0, np.int32)
            return RoutePath(snaps=snaps, legs=legs, passes=passlimit.passes_on(steps))

        return passlimit.route(legs_with, lambda path: self.weight(path, options),
                               np.asarray(self.graph.pass_grade), options)

    def weight(self, path: RoutePath, options: Options) -> float:
        """What *path* costs the search (the backpack included)."""
        g = self.graph
        arcs = [leg.arcs for leg in path.legs if hasattr(leg, "arcs")]
        if not arcs:
            return 0.0
        a = np.concatenate(arcs)
        k = float(options.pack_kg) / 10.0
        return float((np.asarray(g.arc_weight[a], np.float64) + k * np.asarray(g.arc_pack_w[a], np.float64)).sum())

    def edge_time(self, path: RoutePath, options: Options):
        """Seconds for each step of *path*, with the backpack: a callable for
        routing.route, keyed by (edge, forward)."""
        g = self.graph
        k = float(options.pack_kg) / 10.0
        seconds: dict[tuple[int, bool], float] = {}
        for leg in path.legs:
            a = getattr(leg, "arcs", np.zeros(0, np.int64))
            t = np.asarray(g.arc_time[a], np.float64) + k * np.asarray(g.arc_pack_t[a], np.float64)
            for step, sec in zip(leg.edges, t.tolist()):
                seconds[step] = sec
        return lambda edge, forward: seconds[(edge, forward)]
