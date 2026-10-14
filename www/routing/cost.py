"""How long a hiker takes along an edge, each way.

The build costs every edge once, both ways; routing.compile hands the
numbers to the engine as they are. An edge is cut into pieces
of at most ``sample_step_m``; on each piece

    v = tobler(g) * f_terrain * f_side

where ``g`` is the gradient along the direction of travel (so up and down
differ), ``f_terrain`` the class of the ground (or of the OSM way), and
``f_side`` the slowdown of walking across a slope - the part of the ground's
gradient perpendicular to the direction of travel. Ground steeper than
``max_slope_deg`` stops an off-trail edge, and a fall line (thalweg) steeper
than that along itself - except on a pass descent and inside a pass zone,
where steepness closes nothing and the going is never slower than
``steep_min_speed_kmh``. A fall line runs down the valley floor by
construction, so only its own gradient counts.

The routing weight is the time times the kind's penalty (>= 1), and an
off-trail piece traversing steep ground weighs more again: x (1 +
penalty_per_10deg per 10 degrees of side slope above penalty_from_deg). The
time itself is what the route reports; the weight is what it is chosen by,
and never less than the time, which keeps the A* heuristic admissible.

A crest (ridge kind) is walked along its own gradient like a fall line, but
the sharper it is the slower and the dearer (config ridge): how sharp is the
gentler of its two flanks, the slope ridge.flank_m out either side square
to the piece; past ridge.knife_deg it is a knife edge, walkable but
ridge.knife_penalty times dearer again.

Going down a long steep slope straight is worse than on a slant, more so with a
pack: off trail and outside pass zones, a piece descending more steeply than
descent.from_deg on a slope that drops descent.min_drop_m or more weighs extra, kept apart
(``steep_*``) so routing.pack can make it heavier per kg carried.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from routing import model
from routing.config import Config
from routing.grid import Grid

# Tobler's hiking function peaks at 6 km/h on a 5% descent; nothing is faster,
# which is what keeps the A* heuristic (distance / this) admissible.
V_MAX_KMH = 6.0
V_MAX_MS = V_MAX_KMH / 3.6

CHUNK_EDGES = 200_000


def tobler_kmh(grade):
    return V_MAX_KMH * np.exp(-3.5 * np.abs(np.asarray(grade) + 0.05))


@dataclass
class Rasters:
    """What the sampler reads, all on one grid."""

    grid: Grid
    dem: np.ndarray  # float32, NaN = no data
    gx: np.ndarray  # dz/dx (east), float32
    gy: np.ndarray  # dz/dy (north), float32
    terrain: np.ndarray  # uint8 model.TERRAIN codes
    crevasse: np.ndarray | None = None  # uint8 1 = a mapped crevasse field
    # uint8 1 = on a slope steeper than descent.from_deg dropping at least
    # descent.min_drop_m (long_steep()); None: no steep-descent weight
    long_steep: np.ndarray | None = None


def long_steep(dem: np.ndarray, slope_deg: np.ndarray, from_deg: float, min_drop_m: float) -> np.ndarray:
    """Cells of the slopes steeper than *from_deg* that drop *min_drop_m* or
    more: each patch of such ground (8-connected) whose height spans that much.
    A terrace bank or a moraine wall is steep for a few dozen metres - gone
    straight down, it is over before it matters."""
    from scipy import ndimage

    steep = np.nan_to_num(slope_deg) > from_deg
    labels, n = ndimage.label(steep, structure=np.ones((3, 3), dtype=bool))
    if n == 0:
        return np.zeros(dem.shape, dtype=np.uint8)
    z = np.nan_to_num(dem, nan=0.0)
    flat = labels.ravel()
    on = flat > 0
    top = np.full(n + 1, -np.inf)
    low = np.full(n + 1, np.inf)
    np.maximum.at(top, flat[on], z.ravel()[on])
    np.minimum.at(low, flat[on], z.ravel()[on])
    tall = (top - low) >= min_drop_m
    tall[0] = False
    return tall[labels].astype(np.uint8)


@dataclass
class EdgeCosts:
    length: np.ndarray
    time_fwd: np.ndarray  # seconds, inf = cannot be walked this way
    time_bwd: np.ndarray
    terrain: np.ndarray  # dominant class by length
    weight_fwd: np.ndarray  # time with the traverse penalty, before the kind's
    weight_bwd: np.ndarray
    # the steep-descent part of the weight, included in weight_*: seconds
    steep_fwd: np.ndarray
    steep_bwd: np.ndarray
    # For the backpack (routing.pack), each direction's time split by what a
    # load makes harder: sum of piece time x climb (in 10% units), x descent,
    # and x roughness (1 - the ground's speed factor).
    # whether steepness would have closed the edge but for a pass zone's waiver
    waived: np.ndarray
    climb_fwd: np.ndarray
    climb_bwd: np.ndarray
    descent_fwd: np.ndarray
    descent_bwd: np.ndarray
    rough_fwd: np.ndarray
    rough_bwd: np.ndarray
    ele_from: np.ndarray
    ele_to: np.ndarray


class CostModel:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.step = cfg.graph.sample_step_m
        self.terrain_factor = np.array(
            [cfg.terrain_speed.get(name, cfg.terrain_speed["unknown"]) for name in model.TERRAINS],
            dtype=np.float64,
        )
        self.max_slope_tan = np.tan(np.radians(cfg.obstacles.max_slope_deg))
        self.steep_min_ms = cfg.graph.steep_min_speed_kmh / 3.6
        side = cfg.side_slope
        self._side_x = np.array([side.free_deg, 35.0])
        self._side_f = np.array([1.0, side.factor_at_35])
        self.traverse_from = side.penalty_from_deg
        self.traverse_per_deg = side.penalty_per_10deg / 10.0
        self.crevasse = cfg.crevasse
        ridge = cfg.ridge
        self.ridge = ridge
        self._ridge_x = np.array([ridge.free_deg, 40.0])
        self._ridge_f = np.array([1.0, ridge.factor_at_40])
        self.descent_from_tan = np.tan(np.radians(cfg.descent.from_deg))
        self.descent_from = cfg.descent.from_deg
        self.descent_per_deg = cfg.descent.penalty_per_10deg / 10.0
        self.penalty = np.ones(len(model.KINDS))
        # a connector is off-trail going like the lattice: were it cheaper, a
        # route would hop lattice -> line node -> lattice instead
        self.penalty[model.LATTICE] = self.penalty[model.CONNECTOR] = cfg.graph.lattice_penalty
        self.penalty[model.BANK] = cfg.graph.bank_penalty

    # ------------------------------------------------------------- factors

    def side_factor(self, cross_deg) -> np.ndarray:
        return np.interp(cross_deg, self._side_x, self._side_f)

    def traverse_factor(self, cross_deg) -> np.ndarray:
        """Weight multiplier for walking across a slope of *cross_deg*."""
        return 1.0 + self.traverse_per_deg * np.maximum(np.asarray(cross_deg) - self.traverse_from, 0.0)

    def glacier_crevasse_factor(self, steep_deg) -> np.ndarray:
        """Weight multiplier of off-trail glacier as steep as *steep_deg*."""
        c = self.crevasse
        share = np.clip((np.asarray(steep_deg) - c.glacier_from_deg) / (c.glacier_full_deg - c.glacier_from_deg), 0, 1)
        return 1.0 + c.glacier_penalty * share

    def ridge_flank_deg(self, rasters: Rasters, pm: np.ndarray, unit: np.ndarray) -> np.ndarray:
        """How sharp a crest is at *pm*, walked along *unit*: the slope of the
        ground ridge.flank_m and 1.5 x that to either side, square to the
        walk, averaged per side; the gentler side counts. Slopes, not a drop
        from the line: a divide traced on the cell edges may run a cell off
        the very crest. Not nearer: the cell of the crest itself reads flat."""
        grid, d = rasters.grid, self.ridge.flank_m
        normal = np.column_stack([-unit[:, 1], unit[:, 0]])
        sides = []
        for sign in (1.0, -1.0):
            tans = []
            for k in (1.0, 1.5):
                px = pm[:, 0] + sign * k * d * normal[:, 0]
                py = pm[:, 1] + sign * k * d * normal[:, 1]
                gx = np.nan_to_num(grid.nearest(rasters.gx, px, py, fill=0.0))
                gy = np.nan_to_num(grid.nearest(rasters.gy, px, py, fill=0.0))
                tans.append(np.hypot(gx, gy))
            sides.append((tans[0] + tans[1]) / 2)
        return np.degrees(np.arctan(np.minimum(sides[0], sides[1])))

    def ridge_factor(self, flank_deg) -> np.ndarray:
        """Speed factor of a crest as sharp as *flank_deg* (clamped at 40 degrees' value)."""
        return np.interp(flank_deg, self._ridge_x, self._ridge_f)

    def ridge_penalty(self, flank_deg) -> np.ndarray:
        """Weight multiplier of a crest as sharp as *flank_deg*: growing past
        free_deg, and knife_penalty times that on a knife edge."""
        r = self.ridge
        flank_deg = np.asarray(flank_deg)
        grow = 1.0 + r.penalty_per_10deg / 10.0 * np.maximum(flank_deg - r.free_deg, 0.0)
        return grow * np.where(flank_deg > r.knife_deg, r.knife_penalty, 1.0)

    def steep_descent_share(self, g) -> np.ndarray:
        """Extra weight per second of walking down gradient *g* (< 0 descends)."""
        deg = np.degrees(np.arctan(np.maximum(-np.asarray(g, dtype=np.float64), 0.0)))
        return self.descent_per_deg * np.maximum(deg - self.descent_from, 0.0)

    def osm_factor(self, tags) -> float:
        speed = self.cfg.osm_speed
        factor = speed.get("highway", {}).get(tags.get("highway", ""), 1.0)
        for key in ("sac_scale", "trail_visibility"):
            value = tags.get(key)
            if value is not None:
                factor *= speed.get(key, {}).get(value, 1.0)
        return factor

    def speed_ms(self, grade, factor) -> np.ndarray:
        return tobler_kmh(grade) / 3.6 * factor

    # ------------------------------------------------------------- sampling

    def edge_costs(
        self,
        rasters: Rasters,
        geom_xy: np.ndarray,
        geom_off: np.ndarray,
        kinds: np.ndarray,
        way_factor: np.ndarray,
        steep_ok: np.ndarray | None = None,
    ) -> EdgeCosts:
        """Times both ways for every edge, its polyline given as
        ``geom_xy[geom_off[i]:geom_off[i+1]]``. *way_factor* is the OSM speed
        factor of an osm edge (ignored for the others); *steep_ok* marks the
        off-trail edges inside a pass zone, where steepness closes nothing."""
        n = len(kinds)
        if steep_ok is None:
            steep_ok = np.zeros(n, dtype=bool)
        out = EdgeCosts(
            length=np.zeros(n),
            time_fwd=np.zeros(n),
            time_bwd=np.zeros(n),
            terrain=np.zeros(n, dtype=np.uint8),
            weight_fwd=np.zeros(n),
            weight_bwd=np.zeros(n),
            steep_fwd=np.zeros(n),
            steep_bwd=np.zeros(n),
            waived=np.zeros(n, dtype=bool),
            climb_fwd=np.zeros(n),
            climb_bwd=np.zeros(n),
            descent_fwd=np.zeros(n),
            descent_bwd=np.zeros(n),
            rough_fwd=np.zeros(n),
            rough_bwd=np.zeros(n),
            ele_from=np.zeros(n, dtype=np.float32),
            ele_to=np.zeros(n, dtype=np.float32),
        )
        for start in range(0, n, CHUNK_EDGES):
            stop = min(n, start + CHUNK_EDGES)
            lo, hi = geom_off[start], geom_off[stop]
            self._chunk(
                rasters,
                geom_xy[lo:hi],
                geom_off[start : stop + 1] - lo,
                kinds[start:stop],
                way_factor[start:stop],
                steep_ok[start:stop],
                out,
                start,
            )
        return out

    def _chunk(self, rasters, xy, off, kinds, way_factor, steep_ok, out: EdgeCosts, base: int) -> None:
        grid = rasters.grid
        n = len(kinds)
        nverts = np.diff(off)
        # segments: every vertex but the last of its edge starts one
        is_last = np.zeros(len(xy), dtype=bool)
        is_last[off[1:] - 1] = True
        seg_start = np.flatnonzero(~is_last)
        seg_edge = np.repeat(np.arange(n), nverts - 1)
        a = xy[seg_start]
        b = xy[seg_start + 1]
        d = b - a
        seg_len = np.hypot(d[:, 0], d[:, 1])
        nsub = np.maximum(1, np.ceil(seg_len / self.step).astype(np.int64))

        # sub-pieces of each segment, k = 0..nsub-1
        sub_seg = np.repeat(np.arange(len(seg_start)), nsub)
        first = np.cumsum(nsub) - nsub
        k = np.arange(len(sub_seg)) - first[sub_seg]
        t0 = k / nsub[sub_seg]
        t1 = (k + 1) / nsub[sub_seg]
        sa = a[sub_seg]
        sd = d[sub_seg]
        p0 = sa + sd * t0[:, None]
        p1 = sa + sd * t1[:, None]
        pm = (p0 + p1) / 2
        ds = seg_len[sub_seg] / nsub[sub_seg]
        sub_edge = seg_edge[sub_seg]
        sub_kind = kinds[sub_edge]

        z0 = grid.bilinear(rasters.dem, p0[:, 0], p0[:, 1])
        z1 = grid.bilinear(rasters.dem, p1[:, 0], p1[:, 1])
        with np.errstate(invalid="ignore", divide="ignore"):
            g = np.where(ds > 0, (z1 - z0) / ds, 0.0)
        g = np.nan_to_num(g, nan=0.0)  # off the DEM: walk it as flat

        gx = grid.nearest(rasters.gx, pm[:, 0], pm[:, 1], fill=0.0)
        gy = grid.nearest(rasters.gy, pm[:, 0], pm[:, 1], fill=0.0)
        gx = np.nan_to_num(gx)
        gy = np.nan_to_num(gy)
        with np.errstate(invalid="ignore", divide="ignore"):
            seg_unit = sd / np.where(seg_len[sub_seg] > 0, seg_len[sub_seg], 1.0)[:, None]
        along = gx * seg_unit[:, 0] + gy * seg_unit[:, 1]
        steep2 = gx * gx + gy * gy
        cross = np.sqrt(np.maximum(steep2 - along * along, 0.0))
        cross_deg = np.degrees(np.arctan(cross))
        steep = np.sqrt(steep2)

        terr = grid.nearest(rasters.terrain, pm[:, 0], pm[:, 1], fill=model.TERRAIN["unknown"])
        terr = terr.astype(np.int64)

        is_osm = sub_kind == model.OSM
        is_bank = sub_kind == model.BANK
        # The bank geometry is offset onto dry ground, but with 30 m terrain
        # cells its midpoint can still sample the adjacent lake. Treat that
        # raster ambiguity as unknown dry ground, not impassable water.
        terr = np.where(is_bank & (terr == model.TERRAIN["water"]), model.TERRAIN["unknown"], terr)
        is_fall = np.isin(sub_kind, model.FALL_LINE_KINDS)
        is_free = ~is_osm & ~is_fall
        # steepness closes nothing on a pass descent, nor on any off-trail
        # edge (thalwegs included) inside a pass zone
        is_steep_ok = (sub_kind == model.PASS) | (~is_osm & steep_ok[sub_edge])

        factor = np.where(is_osm, way_factor[sub_edge], self.terrain_factor[terr])
        factor = np.where(is_free, factor * self.side_factor(cross_deg), factor)
        too_steep = (is_free & (steep > self.max_slope_tan)) | (is_fall & (np.abs(g) > self.max_slope_tan))
        # a crest: slower the sharper, a knife edge closed
        is_ridge = sub_kind == model.RIDGE
        flank = np.zeros(len(pm))
        if is_ridge.any():
            flank[is_ridge] = self.ridge_flank_deg(rasters, pm[is_ridge], seg_unit[is_ridge])
            factor = np.where(is_ridge, factor * self.ridge_factor(flank), factor)
        blocked = ~is_steep_ok & too_steep
        # crevasses: a mapped field slows everyone down, trail or not
        if rasters.crevasse is not None:
            in_field = grid.nearest(rasters.crevasse, pm[:, 0], pm[:, 1], fill=0) > 0
        else:
            in_field = np.zeros(len(pm), dtype=bool)
        factor = np.where(in_field, factor / (1.0 + self.crevasse.osm_slowdown), factor)
        roughness = np.clip(1.0 - factor, 0.0, 1.0)
        factor = np.where(blocked, 0.0, factor)
        terr = np.where(is_osm, model.TERRAIN["path"], terr)

        v_f = self.speed_ms(g, factor)
        v_b = self.speed_ms(-g, factor)
        floor = np.where(is_steep_ok & (factor > 0), self.steep_min_ms, 0.0)
        v_f = np.maximum(v_f, floor)
        v_b = np.maximum(v_b, floor)
        with np.errstate(divide="ignore"):
            t_f = np.where(v_f > 0, ds / np.where(v_f > 0, v_f, 1.0), np.inf)
            t_b = np.where(v_b > 0, ds / np.where(v_b > 0, v_b, 1.0), np.inf)

        # trails are built across slopes and fall lines run down them: only
        # free going pays for a traverse
        traverse = np.where(is_free, self.traverse_factor(cross_deg), 1.0)
        traverse = np.where(is_ridge, self.ridge_penalty(flank), traverse)
        # and crevasses: a mapped field for anyone, steep glacier off trail
        on_ice = (terr == model.TERRAIN["glacier"]) & ~is_osm
        traverse = traverse * np.where(in_field, 1.0 + self.crevasse.osm_penalty, 1.0)
        traverse = traverse * np.where(on_ice, self.glacier_crevasse_factor(np.degrees(np.arctan(steep))), 1.0)

        # a long steep slope, gone straight down off trail - but not below a
        # pass: down its sides there is no gentler way, its grade says how hard
        if rasters.long_steep is not None:
            on_steep = ~is_osm & ~is_steep_ok & (grid.nearest(rasters.long_steep, pm[:, 0], pm[:, 1], fill=0) > 0)
        else:
            on_steep = np.zeros(len(pm), dtype=bool)
        # (a closed piece adds nothing: its edge's time is inf already)
        tf_ok = np.where(np.isfinite(t_f), t_f, 0.0)
        tb_ok = np.where(np.isfinite(t_b), t_b, 0.0)
        steep_f = np.where(on_steep, tf_ok * self.steep_descent_share(g), 0.0)
        steep_b = np.where(on_steep, tb_ok * self.steep_descent_share(-g), 0.0)

        sl = slice(base, base + n)
        out.steep_fwd[sl] = np.bincount(sub_edge, weights=steep_f, minlength=n)
        out.steep_bwd[sl] = np.bincount(sub_edge, weights=steep_b, minlength=n)
        out.waived[sl] = np.bincount(sub_edge, weights=(is_steep_ok & too_steep).astype(np.float64), minlength=n) > 0
        out.length[sl] = np.bincount(sub_edge, weights=ds, minlength=n)
        out.time_fwd[sl] = np.bincount(sub_edge, weights=t_f, minlength=n)
        out.time_bwd[sl] = np.bincount(sub_edge, weights=t_b, minlength=n)
        out.weight_fwd[sl] = np.bincount(sub_edge, weights=t_f * traverse + steep_f, minlength=n)
        out.weight_bwd[sl] = np.bincount(sub_edge, weights=t_b * traverse + steep_b, minlength=n)

        # what a backpack makes harder, time-weighted; a closed piece adds
        # nothing here (its edge's time is inf already, and inf x 0 is NaN)
        up = np.maximum(g, 0.0) * 10.0
        down = np.maximum(-g, 0.0) * 10.0
        tf, tb = tf_ok, tb_ok
        for name, weights in (("climb_fwd", tf * up), ("climb_bwd", tb * down),
                              ("descent_fwd", tf * down), ("descent_bwd", tb * up),
                              ("rough_fwd", tf * roughness), ("rough_bwd", tb * roughness)):
            getattr(out, name)[sl] = np.bincount(sub_edge, weights=weights, minlength=n)

        nt = len(model.TERRAINS)
        by_terrain = np.bincount(sub_edge * nt + terr, weights=ds, minlength=n * nt).reshape(n, nt)
        out.terrain[sl] = np.argmax(by_terrain, axis=1).astype(np.uint8)

        ends = grid.bilinear(rasters.dem, xy[off[:-1], 0], xy[off[:-1], 1])
        out.ele_from[sl] = np.nan_to_num(ends, nan=0.0)
        ends = grid.bilinear(rasters.dem, xy[off[1:] - 1, 0], xy[off[1:] - 1, 1])
        out.ele_to[sl] = np.nan_to_num(ends, nan=0.0)
