"""Assemble the combined graph: OSM ways plus the off-trail network.

The off-trail network follows how people actually cross mountains without a
trail:

* along the valley floor - the thalwegs, lines of falling water; a stream
  big enough to wade (ford.free_below_km2) is walked on one bank or the
  other, and getting across costs (ford.crossing_*), so a route crosses when
  it has to, not back and forth;
* along the crests - the divides between valleys, up spurs and along ridges;
* from one valley into the next over a pass - each mountain_pass is joined by
  its fall lines to the valleys on either side;
* across anything else on a sparse triangular lattice, dearer per second
  than the lines above so it only carries what they cannot;
* never through a barrier (see routing.build.barriers): along a river too big
  to ford it walks the near bank, and gets across only at a marked ford or on
  an OSM bridge.

Connectors tie the lines, the lattice and the OSM nodes together. All
coordinates are in the grid's CRS.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import numpy as np
import shapely
import shapely.ops
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree
from shapely import STRtree
from shapely.geometry import LineString, MultiLineString, Point

from routing import model
from routing.build import barriers as bar
from routing.build import hydrology
from routing.build import stream_match
from routing.build.demcache import DemLines
from routing.build.osmdata import OsmData, parse_width
from routing.build.rivers import RiverSize, effective_catchment
from routing.build.terrain import project
from routing.config import Config
from routing.cost import CostModel, Rasters
from routing.grid import Projector

log = logging.getLogger(__name__)

# fall lines are cut into edges this long, so connectors can join them midway
PASS_PIECE_M = 150.0
BANK_PIECE_M = 75.0
BANK_OFFSET_M = 10.0
CLIFF_LINE_BUFFER_M = 10.0
LINE_BARRIER_BUFFER_M = 2.0
WATERFALL_RADIUS_M = 20.0
RIVER_PIECE_M = 100.0
FORD_SEARCH_M = 300.0
MAX_DESCENT_CELLS = 3000
PASS_RINGS_M = (60.0, 90.0, 150.0, 250.0, 400.0)
# what steepness-free going (thalwegs, pass zones) ignores: the DEM's steep
# patches - an OSM waterfall is walked round, not through
SOFT_BARRIERS = ("steep",)
# what a pass zone lets through at the dearest price there is: a rock band on
# the way to a pass is climbed, with ropes (graph.pass_cliff_*)
ZONE_CLIMBED = ("cliff",)
# going over a pass: within the apex half-length plus this of its point
APEX_SLACK_M = 50.0
# a pass descent ends on a channel walkable for this long downstream
CLEAR_RUN_M = 600.0
# both banks of a wadable stream: lines this far either side of it, ending
# this far short of where its runs meet - the nodes that meet there are
# joined by connectors, which pay for crossing a stream like any other
BANK_SIDE_M = 8.0
BANK_PULL_M = 15.0
# a pass node is joined to the crest nodes this close
PASS_RIDGE_M = 150.0
# a node of a line (thalweg, bank, crest, pass) is joined to this many
# lattice nodes around it
LINE_LATTICE_LINKS = 2


@dataclass
class Block:
    """Edges of one kind. ``geom_off`` is None for straight edges."""

    kind: int
    u: np.ndarray
    v: np.ndarray
    geom_xy: np.ndarray | None = None
    geom_off: np.ndarray | None = None
    way_factor: np.ndarray | None = None
    extra_s: np.ndarray | None = None
    osm_way: np.ndarray | None = None
    # where steepness closes nothing: fall lines, edges inside a pass zone
    steep_ok: np.ndarray | None = None
    # the pass (index into the pass table) an edge belongs to or lies in the
    # zone of, -1 for none; and whether it crosses the DEM's steep patches,
    # which only that pass admits
    zone_pass: np.ndarray | None = None
    soft_crossed: np.ndarray | None = None
    # crosses a mapped cliff, which only a pass zone lets it
    cliff_crossed: np.ndarray | None = None
    # its wading is priced already (extra_s): crossing_penalties leaves it be
    wades: bool = False

    def __len__(self) -> int:
        return len(self.u)

    def subset(self, keep: np.ndarray) -> Block:
        if keep.all():
            return self
        geom_xy = geom_off = None
        if self.geom_off is not None:
            counts = np.diff(self.geom_off)
            vert_edge = np.repeat(np.arange(len(self.u)), counts)
            geom_xy = self.geom_xy[keep[vert_edge]]
            geom_off = np.concatenate([[0], np.cumsum(counts[keep])])
        pick = (lambda a: None if a is None else a[keep])
        return Block(self.kind, self.u[keep], self.v[keep], geom_xy, geom_off,
                     pick(self.way_factor), pick(self.extra_s), pick(self.osm_way), pick(self.steep_ok),
                     pick(self.zone_pass), pick(self.soft_crossed), pick(self.cliff_crossed), self.wades)


@dataclass
class Graph:
    node_x: np.ndarray
    node_y: np.ndarray
    node_kind: np.ndarray
    node_osm: np.ndarray
    edge_u: np.ndarray
    edge_v: np.ndarray
    edge_kind: np.ndarray
    geom_xy: np.ndarray
    geom_off: np.ndarray
    way_factor: np.ndarray
    extra_s: np.ndarray
    osm_way: np.ndarray
    steep_ok: np.ndarray
    zone_pass: np.ndarray
    soft_crossed: np.ndarray
    cliff_crossed: np.ndarray
    cross_pass: np.ndarray  # the pass an edge crosses the ridge at (near its point), -1
    passes: list[dict] = field(default_factory=list)  # osm, name, grade, x, y
    debug: dict = field(default_factory=dict)


def _polylines(lines: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    counts = np.array([len(c) for c in lines], dtype=np.int64)
    off = np.concatenate([[0], np.cumsum(counts)])
    xy = np.concatenate(lines) if lines else np.zeros((0, 2))
    return xy, off


def _trim(xy: np.ndarray, start_m: float, end_m: float) -> np.ndarray:
    """The polyline *xy* without its first *start_m* and last *end_m* metres."""
    line = LineString(xy)
    piece = shapely.ops.substring(line, start_m, max(start_m, line.length - end_m))
    coords = np.asarray(piece.coords) if piece.geom_type == "LineString" else np.asarray([piece.coords[0]] * 2)
    return coords if len(coords) >= 2 else np.vstack([coords, coords])


def _offset(xy: np.ndarray, dist: float) -> np.ndarray:
    """*xy* moved *dist* to its left (right if negative).

    GEOS removes loops created by sharp bends. A vertex-normal offset crosses
    a winding river repeatedly even though it intends to stay on one bank.
    """
    source = LineString(xy)
    shifted = source.offset_curve(dist, quad_segs=4, join_style="round")
    parts = [p for p in getattr(shifted, "geoms", [shifted])
             if isinstance(p, LineString) and not p.is_empty and len(p.coords) >= 2]
    if not parts:
        return xy.copy()
    coords = np.asarray(max(parts, key=lambda p: p.length).coords)
    # GEOS before 3.11 can reverse a right-hand offset. Graph endpoints retain
    # the source line's upstream -> downstream meaning.
    same = np.linalg.norm(coords[0] - xy[0]) + np.linalg.norm(coords[-1] - xy[-1])
    reverse = np.linalg.norm(coords[-1] - xy[0]) + np.linalg.norm(coords[0] - xy[-1])
    return coords if same <= reverse else coords[::-1]


def _drop_unlinked(graph: Graph) -> None:
    """Nodes no edge kept: a lattice point or a line node whose every edge a
    barrier took - dead weight in the package and the compiled graph."""
    used = np.zeros(len(graph.node_x), dtype=bool)
    used[graph.edge_u] = used[graph.edge_v] = True
    if used.all():
        return
    new_id = np.cumsum(used) - 1
    log.info("dropping %s nodes no edge reaches", int((~used).sum()))
    graph.node_x, graph.node_y = graph.node_x[used], graph.node_y[used]
    graph.node_kind, graph.node_osm = graph.node_kind[used], graph.node_osm[used]
    graph.edge_u, graph.edge_v = new_id[graph.edge_u], new_id[graph.edge_v]


def _shapely_lines(xy: np.ndarray, off: np.ndarray) -> np.ndarray:
    counts = np.diff(off)
    return shapely.linestrings(xy, indices=np.repeat(np.arange(len(counts)), counts))


class GraphBuilder:
    def __init__(self, cfg: Config, rasters: Rasters, slope_deg: np.ndarray,
                 hyd: hydrology.Hydrology, osm: OsmData, projector: Projector,
                 lattice_box: tuple[float, float, float, float],
                 rivers: RiverSize | None = None,
                 dem_lines: DemLines | None = None):
        self.cfg = cfg
        self.rasters = rasters
        self.grid = rasters.grid
        self.slope_deg = slope_deg
        self.hyd = hyd
        self.down = hyd.downstream()
        self.osm = osm
        self.projector = projector
        self.box = lattice_box
        self.dem_lines = dem_lines
        self.cost = CostModel(cfg)
        if rivers is None:  # the area's own catchments, glaciers from the terrain raster
            glacier = rasters.terrain == model.TERRAIN["glacier"]
            rivers = RiverSize().add(self.grid, effective_catchment(
                self.grid, hyd, glacier, cfg.ford.glacier_factor))
        self.rivers = rivers

        self._nx: list[np.ndarray] = []
        self._ny: list[np.ndarray] = []
        self._nkind: list[np.ndarray] = []
        self._nosm: list[np.ndarray] = []
        self.n = 0
        self.blocks: list[Block] = []
        self.barriers = bar.Barriers()
        self.crossings: list[tuple[LineString, float]] = []
        self.debug: dict = {"passes": [], "canyons": []}

        self.osm_ids = np.zeros(0, dtype=np.int64)
        self.osm_nodes = np.zeros(0, dtype=np.int64)
        self.line_nodes: list[np.ndarray] = []
        self.lattice_nodes = np.zeros(0, dtype=np.int64)
        self.bank_nodes = np.zeros(0, dtype=np.int64)
        self.ridge_nodes = np.zeros(0, dtype=np.int64)
        self.pass_nodes: list[int] = []
        # wadable streams: catchment of each chain (by index) that is one, and
        # the nodes meeting at each cell where one of them begins or ends
        self.wet_catch: dict[int, float] = {}
        self.bank_groups: dict[int, list[int]] = {}
        self.osm_wadable_lines: list[LineString] = []
        self.pass_zone = None
        self.zone_sectors: list = []
        self.pass_table: list[dict] = []
        self.ridge_divides: list[LineString] = []
        # thalweg centre lines after optional OSM alignment (index -> xy)
        self.chain_polylines: dict[int, np.ndarray] = {}

    # ------------------------------------------------------------- plumbing

    def add_nodes(self, x, y, kind: int, osm=None) -> np.ndarray:
        x = np.asarray(x, dtype=np.float64).ravel()
        y = np.asarray(y, dtype=np.float64).ravel()
        idx = np.arange(self.n, self.n + len(x), dtype=np.int64)
        self._nx.append(x)
        self._ny.append(y)
        self._nkind.append(np.full(len(x), kind, dtype=np.uint8))
        self._nosm.append(np.zeros(len(x), np.int64) if osm is None else np.asarray(osm, np.int64))
        self.n += len(x)
        return idx

    def node_xy(self) -> np.ndarray:
        return np.column_stack([np.concatenate(self._nx), np.concatenate(self._ny)])

    def in_box(self, x, y) -> np.ndarray:
        x0, y0, x1, y1 = self.box
        return (x >= x0) & (x <= x1) & (y >= y0) & (y <= y1)

    def _keep_unblocked(self, block: Block, xy: np.ndarray) -> Block:
        """*block* without the edges crossing a barrier, *steep_ok* filled in:
        a pass descent, or any other off-trail edge lying inside a pass zone
        (thalwegs too), may cross the DEM's steep patches and is not closed by
        steepness in costing; it may cross a mapped cliff too, climbed at
        the dearest price there is (graph.pass_cliff_*)."""
        if len(block) == 0:
            return block
        if block.geom_off is None:
            coords = np.stack([xy[block.u], xy[block.v]], axis=1)
            lines = shapely.linestrings(coords)
        else:
            lines = _shapely_lines(block.geom_xy, block.geom_off)
        if block.kind == model.PASS:
            steep_ok = np.ones(len(block), dtype=bool)
        elif block.kind == model.OSM or self.pass_zone is None:
            steep_ok = np.zeros(len(block), dtype=bool)
        else:
            steep_ok = shapely.within(lines, self.pass_zone)
        block.steep_ok = steep_ok
        # a crest is as sharp as the DEM's steep patches either side of it
        # say: that is weighed (routing.cost, config ridge), not a barrier
        blocked = self.barriers.blocked(lines, ignore=SOFT_BARRIERS if block.kind == model.RIDGE else ())
        block.soft_crossed = np.zeros(len(block), dtype=bool)
        block.cliff_crossed = np.zeros(len(block), dtype=bool)
        if block.zone_pass is None:
            block.zone_pass = np.full(len(block), -1, dtype=np.int32)
        if steep_ok.any():
            soft = self.barriers.blocked(lines, ignore=SOFT_BARRIERS + ZONE_CLIMBED)
            block.soft_crossed = steep_ok & blocked & ~soft
            block.cliff_crossed = block.soft_crossed & self.barriers.blocked(lines, only=ZONE_CLIMBED)
            if block.cliff_crossed.any():
                g = self.cfg.graph
                extra = np.where(block.cliff_crossed, g.pass_cliff_penalty_s, 0.0)
                block.extra_s = extra if block.extra_s is None else block.extra_s + extra
            blocked = np.where(steep_ok, soft, blocked)
            if block.kind != model.PASS:
                block.zone_pass = self._zone_pass(lines, steep_ok)
        if blocked.any():
            log.info("%s: %s of %s edges cross a barrier", model.KINDS[block.kind],
                     int(blocked.sum()), len(block))
        return block.subset(~blocked)

    def _zone_pass(self, lines, inside: np.ndarray) -> np.ndarray:
        """For each line lying in a pass zone: the easiest pass whose sector
        holds it (index into the pass table), -1 elsewhere."""
        out = np.full(len(lines), -1, dtype=np.int32)
        if not self.zone_sectors:
            return out
        polys = np.asarray([z for z, _ in self.zone_sectors], dtype=object)
        owner = np.array([i for _, i in self.zone_sectors], dtype=np.int64)
        grade = np.array([self.pass_table[i]["grade"] for i in owner], dtype=np.int64)
        idx = np.flatnonzero(inside)
        hit_line, hit_sector = STRtree(polys).query(np.asarray(lines, dtype=object)[idx], predicate="within")
        if hit_line.size == 0:
            return out
        # easiest first, so the last write per line is the easiest pass
        order = np.argsort(-grade[hit_sector], kind="stable")
        out[idx[hit_line[order]]] = owner[hit_sector[order]]
        return out

    def _cross_pass(self, graph: Graph) -> np.ndarray:
        """Edges that cross a pass or evade it over the nearby ridge.

        The small apex catches the edge through the saddle itself, whatever
        kind and however gentle. The associated DEM divide catches crossings
        of the same ridge nearby even where the DEM puts them just below the
        saddle. A height cap is the fallback where the coarse divide misses
        the crest. The nearest pass owns an edge where areas overlap, so a
        nearby easier saddle remains usable.
        """
        out = np.full(len(graph.edge_u), -1, dtype=np.int32)
        if not self.pass_table:
            return out
        g = self.cfg.graph
        points = shapely.points([p["x"] for p in self.pass_table], [p["y"] for p in self.pass_table])
        lines = _shapely_lines(graph.geom_xy, graph.geom_off)
        tree = STRtree(shapely.buffer(points, g.pass_zone_apex_m / 2 + APEX_SLACK_M))
        hit_edge, hit_pass = tree.query(lines, predicate="intersects")
        if hit_edge.size:
            dist = shapely.distance(lines[hit_edge], points[hit_pass])
            order = np.argsort(-dist, kind="stable")  # nearest last, so it wins
            out[hit_edge[order]] = hit_pass[order]

        # A category belongs to the neighbouring stretch of the same ridge,
        # not only to the mapped saddle point. Select divides which run through
        # the apex, clip them to the bypass radius and thicken by one DEM cell
        # because the graph's walkable ridge is a simplified copy.
        if self.ridge_divides:
            divides = np.asarray(self.ridge_divides, dtype=object)
            attach = shapely.buffer(points, g.pass_zone_apex_m / 2 + APEX_SLACK_M)
            hit_pass, hit_divide = STRtree(divides).query(attach, predicate="intersects")
            gates, owners = [], []
            circles = shapely.buffer(points, g.pass_bypass_radius_m)
            for p, d in zip(hit_pass.tolist(), hit_divide.tolist()):
                piece = shapely.intersection(divides[d], circles[p])
                if not piece.is_empty:
                    gates.append(piece.buffer(self.grid.res, cap_style="flat"))
                    owners.append(p)
            if gates:
                gate_edge, gate_i = STRtree(np.asarray(gates, dtype=object)).query(
                    lines, predicate="intersects",
                )
                if gate_edge.size:
                    gate_pass = np.asarray(owners, dtype=np.int32)[gate_i]
                    free = out[gate_edge] < 0
                    gate_edge, gate_pass = gate_edge[free], gate_pass[free]
                    dist = shapely.distance(lines[gate_edge], points[gate_pass])
                    order = np.argsort(-dist, kind="stable")
                    out[gate_edge[order]] = gate_pass[order]

        # One representative height per edge. Edges are short (the off-trail
        # lines are cut to their configured spacing); taking endpoints and the
        # midpoint also handles a longer OSM segment arching over the ridge.
        z_vertex = self.grid.bilinear(
            self.rasters.dem, graph.geom_xy[:, 0], graph.geom_xy[:, 1],
        )
        z_vertex = np.nan_to_num(z_vertex, nan=-np.inf)
        z_edge = np.maximum.reduceat(z_vertex, graph.geom_off[:-1])
        mid = shapely.get_coordinates(shapely.line_interpolate_point(lines, 0.5, normalized=True))
        z_mid = self.grid.bilinear(self.rasters.dem, mid[:, 0], mid[:, 1])
        z_edge = np.maximum(z_edge, np.nan_to_num(z_mid, nan=-np.inf))
        px = np.array([p["x"] for p in self.pass_table])
        py = np.array([p["y"] for p in self.pass_table])
        saddle_z = self.grid.bilinear(self.rasters.dem, px, py)

        cap_tree = STRtree(shapely.buffer(points, g.pass_bypass_radius_m))
        cap_edge, cap_pass = cap_tree.query(lines, predicate="intersects")
        if cap_edge.size:
            high = z_edge[cap_edge] >= saddle_z[cap_pass]
            cap_edge, cap_pass = cap_edge[high], cap_pass[high]
            free = out[cap_edge] < 0
            cap_edge, cap_pass = cap_edge[free], cap_pass[free]
            if cap_edge.size:
                dist = shapely.distance(lines[cap_edge], points[cap_pass])
                order = np.argsort(-dist, kind="stable")
                out[cap_edge[order]] = cap_pass[order]
        log.info("pass crossings and ridge bypasses: %s edges near %s passes",
                 int((out >= 0).sum()), len(np.unique(out[out >= 0])))
        return out

    def pass_zones(self, descents: list[dict]):
        """Below each pass, towards each valley it descends to: a sector
        pass_zone_angle_deg wide whose narrow end is pass_zone_apex_m of ridge
        centred on the pass, out to where the descent reaches its thalweg (and
        15% beyond, at most pass_zone_max_m)."""
        g = self.cfg.graph
        half = math.radians(g.pass_zone_angle_deg / 2)
        zones = []
        self.zone_sectors = []
        for owner, record in enumerate(descents):
            px, py = record["xy"]
            for path in record["paths"]:
                ex, ey = self.grid.cell_xy(*divmod(int(path[-1]), self.grid.width))
                dx, dy = float(ex) - px, float(ey) - py
                dist = math.hypot(dx, dy)
                if dist < 1.0:
                    continue
                theta = math.atan2(dy, dx)
                reach = min(max(dist * 1.15, 300.0), g.pass_zone_max_m)
                nx_, ny_ = -math.sin(theta), math.cos(theta)  # square to the way down
                a = g.pass_zone_apex_m / 2
                pts = [(px + nx_ * a, py + ny_ * a), (px - nx_ * a, py - ny_ * a)]
                for k in np.linspace(-half, half, 13):
                    phi = theta + k
                    # from the apex end on the same side, so the edges open at the angle
                    sx, sy = (px - nx_ * a, py - ny_ * a) if k < 0 else (px + nx_ * a, py + ny_ * a)
                    pts.append((sx + reach * math.cos(phi), sy + reach * math.sin(phi)))
                sector = shapely.MultiPoint(pts).convex_hull
                zones.append(sector)
                self.zone_sectors.append((sector, owner))
        if not zones:
            return None
        zone = shapely.union_all(zones)
        shapely.prepare(zone)
        self.debug["pass_zones"] = list(getattr(zone, "geoms", [zone]))
        log.info("pass zones: %s sectors, %.1f km2", len(zones), zone.area / 1e6)
        return zone

    # ---------------------------------------------------------------- steps

    def build(self) -> Graph:
        self.osm_graph()
        descents = self.pass_descents()
        self.pass_table = [
            {"osm": int(r["pass"].id), "name": r["pass"].tags.get("name", ""),
             "grade": model.pass_grade(r["pass"].tags.get("rtsa_scale")), "x": r["xy"][0], "y": r["xy"][1]}
            for r in descents
        ]
        self.pass_zone = self.pass_zones(descents)
        must_keep = np.zeros(self.grid.width * self.grid.height, dtype=bool)
        for record in descents:
            for path in record["paths"]:
                must_keep[path[-1]] = True
        spacing = max(1, round(self.cfg.graph.thalweg_node_spacing_m / self.grid.res))
        # Cached runs have no pass arrivals in them. A pass landing on the
        # valley changes where the runs split, so those are traced again;
        # the watershed they trace is the cached one.
        if self.dem_lines is not None and not must_keep.any():
            chains = self.dem_lines.chains
            log.info("thalwegs: %s runs (cached)", len(chains))
        else:
            chains = hydrology.thalweg_chains(self.hyd.accum_km2, self.down,
                                              self.cfg.graph.valley_catchment_km2, spacing, must_keep)
            log.info("thalwegs: %s runs", len(chains))
        self.obstacles(chains)
        self.banks()
        cell_node = self.thalwegs(chains, {int(p[-1]) for r in descents for p in r["paths"]})
        self.osm_stream_banks()
        self.passes(descents, cell_node)
        self.join_banks()
        self.ridges()
        self.lattice()
        self.connectors()
        self.fords()
        self.crossing_penalties()
        return self.assemble()

    def osm_graph(self) -> None:
        ways = self.osm.routable
        if not ways:
            return
        ids = np.concatenate([w.nodes for w in ways])
        lon = np.concatenate([w.lon for w in ways])
        lat = np.concatenate([w.lat for w in ways])
        uniq, first = np.unique(ids, return_index=True)
        x, y = self.projector.to_xy(lon[first], lat[first])
        self.osm_ids = uniq
        self.osm_nodes = self.add_nodes(x, y, model.OSM, osm=uniq)
        # a way reaching past the DEM: only its stretch on it, which the
        # neighbouring tile costs on its own DEM
        on_dem = ~np.isnan(self.grid.bilinear(self.rasters.dem, x, y))
        us, vs, factors, way_ids = [], [], [], []
        for way in ways:
            at = np.searchsorted(uniq, way.nodes)
            idx = self.osm_nodes[at]
            u, v = idx[:-1], idx[1:]
            keep = (u != v) & on_dem[at[:-1]] & on_dem[at[1:]]
            us.append(u[keep])
            vs.append(v[keep])
            factors.append(np.full(int(keep.sum()), self.cost.osm_factor(way.tags)))
            way_ids.append(np.full(int(keep.sum()), way.id, dtype=np.int64))
        self.blocks.append(Block(model.OSM, np.concatenate(us), np.concatenate(vs),
                                 way_factor=np.concatenate(factors),
                                 osm_way=np.concatenate(way_ids)))
        log.info("OSM graph: %s nodes, %s edges", len(uniq), len(self.blocks[-1]))

    def osm_node_of(self, osm_id: int) -> int | None:
        i = np.searchsorted(self.osm_ids, osm_id)
        if i < len(self.osm_ids) and self.osm_ids[i] == osm_id:
            return int(self.osm_nodes[i])
        return None

    # ---------------------------------------------------------------- passes

    def pass_descents(self) -> list[dict]:
        """Each pass's fall lines: from the low points of a ring around it down
        the D8 pointers to the first thalweg cell walkable onwards."""
        g = self.cfg.graph
        grid = self.grid
        # Down to a thalweg the valley can be walked along from: on a steep
        # side the first channels are gullies too steep to walk (or through
        # rock bands), cut off from the floor below, so the descent goes on
        # down the fall line until the channel runs for CLEAR_RUN_M downstream
        # over ground no steeper than max_slope_deg.
        stream = hydrology.clear_downstream(
            self.hyd.accum_km2, self.down, g.valley_catchment_km2,
            np.nan_to_num(self.slope_deg) > self.cfg.obstacles.max_slope_deg,
            grid.res, CLEAR_RUN_M,
        )
        out = []
        if not self.osm.passes:
            return out
        lon = np.array([p.lon for p in self.osm.passes])
        lat = np.array([p.lat for p in self.osm.passes])
        px, py = self.projector.to_xy(lon, lat)
        angles = np.radians(np.arange(0, 360, 10))
        radii = [r for r in PASS_RINGS_M if r <= g.pass_ring_max_m] or [g.pass_ring_max_m]
        for p, x, y in zip(self.osm.passes, px, py):
            if not self.in_box(x, y):
                continue
            z0 = grid.bilinear(self.rasters.dem, [x], [y])[0]
            if np.isnan(z0):
                continue
            # The mapped point is seldom exactly on the DEM's saddle, and the
            # valley heads are flat: look for the low sides on widening rings
            # until two directions at least 60 degrees apart have turned up.
            picked: list[tuple[int, float, float]] = []  # (direction, ring x, ring y)
            for radius in radii:
                rx = x + radius * np.cos(angles)
                ry = y + radius * np.sin(angles)
                z = grid.bilinear(self.rasters.dem, rx, ry)
                if np.isnan(z).any():
                    break
                low = (z <= np.roll(z, 1)) & (z < np.roll(z, -1)) & (z0 - z >= g.pass_min_drop_m)
                for i in np.argsort(z):
                    if low[i] and all(min(abs(i - j), len(z) - abs(i - j)) >= 6 for j, _, _ in picked):
                        picked.append((int(i), float(rx[i]), float(ry[i])))
                if len(picked) >= 2:
                    break
            picked = picked[:3]
            paths, rings = [], []
            for _, ring_x, ring_y in picked:
                r, c, inside = grid.cell_of([ring_x], [ring_y])
                if not inside[0]:
                    continue
                path = hydrology.fall_line(self.down, int(r[0] * grid.width + c[0]), stream,
                                           MAX_DESCENT_CELLS)
                if path is not None:
                    paths.append(path)
                    rings.append((ring_x, ring_y))
            out.append({"pass": p, "xy": (x, y), "paths": paths, "rings": rings})
        log.info("passes: %s in the area, %s with two or more descents", len(out),
                 sum(len(r["paths"]) >= 2 for r in out))
        return out

    def passes(self, descents: list[dict], cell_node: dict[int, int]) -> None:
        grid = self.grid
        lines, us, vs = [], [], []
        pending: list[tuple[np.ndarray, int, int, int]] = []  # polyline, from, to, pass index
        owners: list[int] = []
        for owner, record in enumerate(descents):
            p = record["pass"]
            x, y = record["xy"]
            node = self.osm_node_of(p.id)
            if node is None:
                node = int(self.add_nodes([x], [y], model.PASS, osm=[0])[0])
            self.line_nodes.append(np.array([node]))
            self.pass_nodes.append(node)
            valleys = set()
            for path, ring in zip(record["paths"], record["rings"]):
                rows, cols = np.divmod(path, grid.width)
                cx, cy = grid.cell_xy(rows, cols)
                end = int(path[-1])
                poly = np.column_stack([np.r_[x, ring[0], cx], np.r_[y, ring[1], cy]])
                if end in self.bank_groups:
                    # down to a wadable stream: on the bank it comes down to
                    length = float(np.hypot(*np.diff(poly, axis=0).T).sum())
                    poly = _trim(poly, 0.0, min(BANK_PULL_M, length / 3))
                    stop = int(self.add_nodes([poly[-1, 0]], [poly[-1, 1]], model.THALWEG)[0])
                    self.line_nodes.append(np.array([stop]))
                    self.bank_groups[end].append(stop)
                else:
                    if end not in cell_node:
                        cell_node[end] = int(self.add_nodes([cx[-1]], [cy[-1]], model.THALWEG)[0])
                    stop = cell_node[end]
                pending.append((poly, node, stop, owner))
                valleys.add(end)
            # descents count once per valley they reach: two that meet the same
            # thalweg cell lead down the same side
            self.debug["passes"].append({"id": p.id, "name": p.tags.get("name", ""),
                                         "x": x, "y": y, "descents": len(valleys)})
        # cut each descent into pieces with nodes of their own in between
        for poly, start, end, owner in pending:
            line = LineString(poly)
            pieces = bar.segment_lines(line, PASS_PIECE_M)
            prev = start
            for k, piece in enumerate(pieces):
                if k == len(pieces) - 1:
                    nxt = end
                else:
                    px, py = piece.coords[-1]
                    nxt = int(self.add_nodes([px], [py], model.PASS)[0])
                    self.line_nodes.append(np.array([nxt]))
                lines.append(np.asarray(piece.coords))
                us.append(prev)
                vs.append(nxt)
                owners.append(owner)
                prev = nxt
        if not lines:
            return
        xy, off = _polylines(lines)
        block = Block(model.PASS, np.array(us), np.array(vs), xy, off,
                      zone_pass=np.array(owners, dtype=np.int32))
        # A pass is where people get across a ridge however steep its sides:
        # like any fall line it is not closed by steepness; an OSM cliff on the
        # way down is climbed at the dearest price, a waterfall cuts the
        # descent - the pass zone around it is where the route then goes round.
        self.blocks.append(self._keep_unblocked(block, self.node_xy()))

    # ------------------------------------------------------------- obstacles

    def obstacles(self, chains: list[np.ndarray]) -> None:
        cfg, grid = self.cfg, self.grid
        ford, obs = cfg.ford, cfg.obstacles
        proj = self.projector
        rivers = self.rivers

        # --- water areas
        water = list(project(self.osm.terrain_areas.get("water", []), proj))

        # --- OSM rivers: too big to ford -> barrier, else a crossing penalty
        osm_lines = project([line for line, _ in self.osm.waterways], proj) if self.osm.waterways else []
        osm_wadable: list[tuple[LineString, float]] = []
        for line, (_, tags) in zip(osm_lines, self.osm.waterways):
            width = parse_width(tags.get("width"))
            for piece in bar.segment_lines(line, RIVER_PIECE_M):
                catch = rivers.of_line(np.asarray(piece.coords))
                too_big = width > ford.max_width_m if width is not None else catch > ford.max_catchment_km2
                if too_big:
                    w = width if width is not None else float(bar.river_width_m(catch))
                    water.append(piece.buffer(w / 2 + 5.0, cap_style="flat"))
                elif catch > ford.free_below_km2:
                    osm_wadable.append((piece, catch))

        # --- DEM channels the same, where OSM has no water nearby
        osm_water = [*osm_lines, *project(self.osm.terrain_areas.get("water", []), proj)]
        near_tree = STRtree(np.asarray(osm_water, dtype=object)) if osm_water else None
        dem_big = 0
        on_ice = self.rasters.terrain.ravel() == model.TERRAIN["glacier"]
        for index, chain in enumerate(chains):
            # a channel down the ice is no river to cross: the melt runs in
            # and under the glacier until its tongue
            if on_ice[chain].mean() > 0.5:
                continue
            line = bar.chain_line(chain, grid)
            catch = float(rivers.at(*line.coords[-1])[0])
            if catch <= ford.free_below_km2:
                continue
            if catch > ford.max_catchment_km2:
                mid = line.interpolate(0.5, normalized=True)
                if near_tree is not None and len(near_tree.query(mid, predicate="dwithin",
                                                                 distance=ford.osm_match_m)):
                    continue
                w = float(bar.river_width_m(catch))
                water.append(line.buffer(w / 2 + 5.0, cap_style="flat"))
                dem_big += 1
            else:
                self.wet_catch[index] = catch
        raw_xy = {i: self._chain_xy(chains[i]) for i in range(len(chains))}
        matched = stream_match.match_chains(
            chains, raw_xy,
            list(zip(osm_lines, [t for _, t in self.osm.waterways])) if self.osm.waterways else [],
            rivers, self.cfg, ford.free_below_km2, ford.max_catchment_km2, ford.max_width_m, ford.osm_match_m,
        )
        self.chain_polylines = {
            i: matched.chain_xy.get(i, raw_xy[i]) for i in raw_xy
        }
        self.crossings = stream_match.canonical_crossings(
            chains, self.chain_polylines, self.wet_catch, osm_wadable, set(matched.chain_xy), ford.osm_match_m,
        )
        if osm_wadable:
            merged = shapely.line_merge(shapely.union_all(
                np.asarray([line for line, _ in osm_wadable], dtype=object)
            ))
            self.osm_wadable_lines = [
                line for line in getattr(merged, "geoms", [merged])
                if isinstance(line, LineString) and not line.is_empty
            ]
        self.water = water
        self.barriers.add(water, "water")

        # --- cliffs
        # the DEM's steep patches are a softer barrier than a mapped cliff: at
        # 30 m a couloir or a scree slope reads as one (see SOFT_BARRIERS)
        self.barriers.add(bar.dem_cliffs(self.slope_deg, grid, obs.cliff_slope_deg, obs.cliff_min_cells),
                          "steep")
        cliffs = list(project(self.osm.cliff_areas, proj)) if self.osm.cliff_areas else []
        lines = []
        if self.osm.barrier_lines:
            projected = project([line for line, _ in self.osm.barrier_lines], proj)
            for (_, tag), line in zip(self.osm.barrier_lines, projected):
                if tag == "natural=cliff":
                    cliffs.append(line.buffer(CLIFF_LINE_BUFFER_M))
                else:
                    lines.append(line.buffer(LINE_BARRIER_BUFFER_M))
        self.barriers.add(cliffs, "cliff")
        self.barriers.add(lines, "line")

        # --- canyons: OSM, then DEM valley floors walled in on both sides
        canyons = list(project(self.osm.canyon_areas, proj)) if self.osm.canyon_areas else []
        if self.osm.canyon_lines:
            canyons += [line.buffer(obs.canyon_band_m) for line in project(self.osm.canyon_lines, proj)]
        canyons += self.dem_canyons(chains)
        self.barriers.add(canyons, "canyon")

        # --- waterfalls
        if self.osm.waterfalls:
            fx, fy = proj.to_xy([p.lon for p in self.osm.waterfalls], [p.lat for p in self.osm.waterfalls])
            self.barriers.add([Point(x, y).buffer(WATERFALL_RADIUS_M) for x, y in zip(fx, fy)],
                              "waterfall")
        log.info("barriers: %s (%s DEM rivers too big to ford), %s fordable crossings",
                 self.barriers.count(), dem_big, len(self.crossings))

    def dem_canyons(self, chains: list[np.ndarray]) -> list:
        obs = self.cfg.obstacles
        if not chains:
            return []
        cells = np.unique(np.concatenate(chains))
        flags = hydrology.canyon_cells(self.rasters.dem, self.slope_deg, self.hyd.drain, cells,
                                       self.grid.res, obs.canyon_wall_slope_deg,
                                       obs.canyon_band_m, obs.canyon_min_depth_m)
        canyon = cells[flags]
        if canyon.size == 0:
            return []
        # canyon cells linked down their D8 pointers form the canyon reaches
        is_canyon = np.zeros(self.grid.width * self.grid.height, dtype=bool)
        is_canyon[canyon] = True
        down = self.down[canyon]
        linked = (down >= 0) & is_canyon[np.maximum(down, 0)]
        local = np.searchsorted(canyon, down[linked])
        src = np.flatnonzero(linked)
        adj = coo_matrix((np.ones(len(src)), (src, local)), shape=(len(canyon), len(canyon)))
        ncomp, comp = connected_components(adj, directed=False)
        sizes = np.bincount(comp, minlength=ncomp)
        out = []
        rows, cols = np.divmod(canyon, self.grid.width)
        cx, cy = self.grid.cell_xy(rows, cols)
        for k in np.flatnonzero(sizes >= obs.canyon_min_cells):
            members = src[comp[src] == k]
            segs = [((cx[i], cy[i]), (cx[local_i], cy[local_i]))
                    for i, local_i in zip(members, local[comp[src] == k])]
            if not segs:
                continue
            reach = MultiLineString(segs)
            # the corridor covers the walls, so nothing crosses it either
            out.append(reach.buffer(obs.canyon_band_m))
            self.debug["canyons"].append(reach)
        log.info("DEM canyons: %s reaches (%s floor cells)", len(out), int(canyon.size))
        return out

    # ----------------------------------------------------------------- lines

    def banks(self) -> None:
        lines = bar.bank_lines(self.water, BANK_OFFSET_M, self.box)
        us, vs, all_nodes = [], [], []
        for line in lines:
            line = shapely.segmentize(line, BANK_PIECE_M)
            coords = np.asarray(line.coords)
            ring = line.is_ring
            if ring:
                coords = coords[:-1]
            if len(coords) < 2:
                continue
            nodes = self.add_nodes(coords[:, 0], coords[:, 1], model.BANK)
            all_nodes.append(nodes)
            us.append(nodes[:-1])
            vs.append(nodes[1:])
            if ring:
                us.append(nodes[-1:])
                vs.append(nodes[:1])
        if not us:
            return
        self.bank_nodes = np.concatenate(all_nodes)
        self.line_nodes.append(self.bank_nodes)
        block = Block(model.BANK, np.concatenate(us), np.concatenate(vs))
        self.blocks.append(self._keep_unblocked(block, self.node_xy()))

    def _chain_xy(self, chain: np.ndarray) -> np.ndarray:
        """A run of cells as the line walked: the D8 staircase taken out, ends fixed."""
        r, c = np.divmod(chain, self.grid.width)
        x, y = self.grid.cell_xy(r, c)
        if len(chain) > 2:
            x = np.r_[x[0], (x[:-2] + x[1:-1] + x[2:]) / 3, x[-1]]
            y = np.r_[y[0], (y[:-2] + y[1:-1] + y[2:]) / 3, y[-1]]
        return np.column_stack([x, y])

    def thalwegs(self, chains: list[np.ndarray], descent_ends: set[int]) -> dict[int, int]:
        """The valley-floor lines. A dry run (a brook, a channel on the ice) is
        one line between nodes shared with the runs it meets. A wadable stream
        is two, one along either bank. Where it just runs on (one wadable run
        in, one out, nothing else) the banks share a node either side, and a
        connector across - its wading priced - joins them. Where more meets
        (a confluence, a brook, a pass descent: *descent_ends*) or a run
        starts or ends, the lines stop short of the cell, and every node
        stopping there is joined to every other (join_banks); those joins,
        like any off-trail edge, pay for crossing a stream."""
        grid = self.grid
        width = grid.width
        ends = np.array([[c[0], c[-1]] for c in chains], dtype=np.int64).reshape(-1, 2)
        rows, cols = np.divmod(ends, width)
        cx, cy = grid.cell_xy(rows, cols)
        inside = self.in_box(cx, cy).any(axis=1)
        picked = [i for i, ok in enumerate(inside) if ok]
        wet_in: dict[int, list[int]] = {}
        wet_out: dict[int, list[int]] = {}
        dry_at: dict[int, int] = {}
        for i in picked:
            first, last = int(chains[i][0]), int(chains[i][-1])
            if i in self.wet_catch:
                wet_out.setdefault(first, []).append(i)
                wet_in.setdefault(last, []).append(i)
            else:
                dry_at[first] = dry_at.get(first, 0) + 1
                dry_at[last] = dry_at.get(last, 0) + 1
        wet_cells = set(wet_in) | set(wet_out)
        through = {c for c in wet_cells
                   if len(wet_in.get(c, ())) == 1 and len(wet_out.get(c, ())) == 1
                   and c not in dry_at and c not in descent_ends}
        groups: dict[int, list[int]] = {c: [] for c in wet_cells - through}
        plain = np.unique([int(c) for i in picked for c in (chains[i][0], chains[i][-1])
                           if int(c) not in wet_cells]).astype(np.int64)
        rows, cols = np.divmod(plain, width)
        ex, ey = grid.cell_xy(rows, cols)
        nodes = self.add_nodes(ex, ey, model.THALWEG)
        cell_node = dict(zip(plain.tolist(), nodes.tolist()))
        self.line_nodes.append(nodes)
        xy_of = {i: self.chain_polylines.get(i, self._chain_xy(chains[i])) for i in picked}

        def new_node(x: float, y: float) -> int:
            node = int(self.add_nodes([x], [y], model.THALWEG)[0])
            self.line_nodes.append(np.array([node]))
            return node

        # the shared bank nodes where a stream runs on, and the wade between
        side_node: dict[tuple[int, float], tuple[int, np.ndarray]] = {}
        wade_u, wade_v, wade_s = [], [], []
        ford = self.cfg.ford
        for c in through:
            up, down = xy_of[wet_in[c][0]], xy_of[wet_out[c][0]]
            d = (up[-1] - up[-2]) / max(np.hypot(*(up[-1] - up[-2])), 1e-9) \
                + (down[1] - down[0]) / max(np.hypot(*(down[1] - down[0])), 1e-9)
            d /= max(np.hypot(*d), 1e-9)
            left = np.array([-d[1], d[0]])
            here = down[0]
            for side in (BANK_SIDE_M, -BANK_SIDE_M):
                at = here + side * left
                side_node[(c, side)] = (new_node(*at), at)
            wade_u.append(side_node[(c, BANK_SIDE_M)][0])
            wade_v.append(side_node[(c, -BANK_SIDE_M)][0])
            catch = self.wet_catch[wet_out[c][0]]
            wade_s.append(ford.crossing_base_s + ford.crossing_penalty_s * math.sqrt(catch))

        lines, us, vs = [], [], []
        for i in picked:
            chain = chains[i]
            first, last = int(chain[0]), int(chain[-1])
            xy = xy_of[i]
            wet = i in self.wet_catch
            length = float(np.hypot(*np.diff(xy, axis=0).T).sum())
            pull = min(BANK_PULL_M, length / 3)
            cut_a = pull if first in groups else 0.0
            cut_b = pull if last in groups else 0.0
            core = _trim(xy, cut_a, cut_b) if cut_a or cut_b else xy
            for side in ((BANK_SIDE_M, -BANK_SIDE_M) if wet else (0.0,)):
                line = _offset(core, side) if side else core.copy()
                ends_ = []
                for cell, k in ((first, 0), (last, -1)):
                    if cell in groups:
                        node = new_node(*line[k])
                        groups[cell].append(node)
                    elif cell in through:
                        node, at = side_node[(cell, side)]
                        line[k] = at
                    else:
                        node = cell_node[cell]
                    ends_.append(node)
                lines.append(line)
                us.append(ends_[0])
                vs.append(ends_[1])
        self.bank_groups = groups
        if lines:
            xy, off = _polylines(lines)
            block = Block(model.THALWEG, np.array(us), np.array(vs), xy, off)
            self.blocks.append(self._keep_unblocked(block, self.node_xy()))
        if wade_u:
            block = Block(model.CONNECTOR, np.array(wade_u), np.array(wade_v),
                          extra_s=np.array(wade_s), wades=True)
            self.blocks.append(self._keep_unblocked(block, self.node_xy()))
        log.info("thalwegs: %s runs, %s along wadable streams (both banks), %s wades on the way",
                 len(picked), sum(i in self.wet_catch for i in picked), len(wade_u))
        return cell_node

    def join_banks(self) -> None:
        """Every node ending where wadable runs meet, joined to every other
        there: along a bank, across a stream (which costs, see
        crossing_penalties), on into a side valley."""
        us, vs = [], []
        for nodes in self.bank_groups.values():
            for k, a in enumerate(nodes):
                for b in nodes[k + 1:]:
                    us.append(a)
                    vs.append(b)
        if us:
            block = Block(model.CONNECTOR, np.array(us), np.array(vs))
            self.blocks.append(self._keep_unblocked(block, self.node_xy()))

    def osm_stream_banks(self) -> None:
        """Continuous banks along mapped wadable rivers.

        DEM thalweg runs stop at hydrological junctions. Their short joining
        connectors can swap sides on a winding mapped river. The full OSM
        geometry supplies a continuous alternative on each bank.
        """
        if not self.osm_wadable_lines:
            return
        node_at: dict[tuple[float, float], int] = {}
        us, vs, all_nodes = [], [], []

        def node(x: float, y: float) -> int:
            key = (round(x, 2), round(y, 2))
            if key not in node_at:
                node_at[key] = int(self.add_nodes([x], [y], model.THALWEG)[0])
                all_nodes.append(node_at[key])
            return node_at[key]

        for center in self.osm_wadable_lines:
            xy = np.asarray(center.coords)
            for side in (BANK_SIDE_M, -BANK_SIDE_M):
                bank = shapely.segmentize(LineString(_offset(xy, side)), BANK_PIECE_M)
                coords = np.asarray(bank.coords)
                if len(coords) < 2 or not self.in_box(coords[:, 0], coords[:, 1]).any():
                    continue
                nodes = np.array([node(float(x), float(y)) for x, y in coords], dtype=np.int64)
                us.append(nodes[:-1])
                vs.append(nodes[1:])
        if not us:
            return
        nodes = np.asarray(all_nodes, dtype=np.int64)
        self.line_nodes.append(nodes)
        block = Block(model.THALWEG, np.concatenate(us), np.concatenate(vs))
        log.info("OSM stream banks: %s nodes, %s edges", len(nodes), len(block))
        self.blocks.append(self._keep_unblocked(block, self.node_xy()))

    # ---------------------------------------------------------------- ridges

    def ridges(self) -> None:
        """The crests: divides between the basins of streams draining
        ridge_catchment_km2 (hydrology.basin_labels), walked along like a
        fall line - their own gradient, not the slope of the sides; how sharp
        they are slows them and weighs them (routing.cost, config ridge), and
        a knife edge is dear, never closed: the DEM's steep patches beside
        it are no barrier to it, as they are to the lattice."""
        g = self.cfg.graph
        water = self.rasters.terrain == model.TERRAIN["water"]
        # Cached divides were traced with no lake. On a lake the DEM is flat
        # and its "basins" a tangle, so a mapped shore is traced again.
        if self.dem_lines is not None and not water.any():
            raw = self.dem_lines.ridges
        else:
            labels = hydrology.basin_labels(self.hyd.accum_km2, self.down, g.ridge_catchment_km2)
            labels[water] = -1
            raw = hydrology.divide_lines(labels, self.grid)
        self.ridge_divides = [
            LineString(xy) for xy in raw
            if len(xy) >= 2 and self.in_box(xy[:, 0], xy[:, 1]).any()
        ]
        tolerance = 0.75 * self.grid.res
        node_at: dict[tuple[float, float], int] = {}
        lines, us, vs, all_nodes = [], [], [], []

        def node(x: float, y: float) -> int:
            key = (round(x, 2), round(y, 2))
            if key not in node_at:
                node_at[key] = int(self.add_nodes([x], [y], model.RIDGE)[0])
                all_nodes.append(node_at[key])
            return node_at[key]

        for xy in raw:
            if not self.in_box(xy[:, 0], xy[:, 1]).any():
                continue
            line = shapely.simplify(LineString(xy), tolerance)
            for piece in bar.segment_lines(line, g.ridge_node_spacing_m):
                coords = np.asarray(piece.coords)
                if len(coords) < 2 or piece.length < 1.0:
                    continue
                a, b = node(*coords[0]), node(*coords[-1])
                if a == b:
                    continue
                lines.append(coords)
                us.append(a)
                vs.append(b)
        if not lines:
            return
        self.ridge_nodes = np.array(all_nodes, dtype=np.int64)
        self.line_nodes.append(self.ridge_nodes)
        xy, off = _polylines(lines)
        block = Block(model.RIDGE, np.array(us), np.array(vs), xy, off)
        log.info("ridges: %s divides, %s edges", len(raw), len(block))
        self.blocks.append(self._keep_unblocked(block, self.node_xy()))

    # --------------------------------------------------------------- lattice

    def lattice(self) -> None:
        s = self.cfg.graph.lattice_spacing_m
        x0, y0, x1, y1 = self.box
        dy = s * math.sqrt(3) / 2
        # Anchored to the CRS, not to the box: neighbouring tiles of a region
        # share a CRS, so in the strip where they overlap their lattice nodes
        # fall on the very same points and routing.compile merges them. The
        # first row is an even one, so local and global row parity agree.
        r0 = int(math.floor(y0 / dy))
        r0 -= r0 % 2
        gy0 = r0 * dy
        gx0 = math.floor(x0 / s) * s - s
        nrows = int((y1 - gy0) // dy) + 1
        ncols = int((x1 - gx0) // s) + 2
        rr, cc = np.meshgrid(np.arange(nrows), np.arange(ncols), indexing="ij")
        x = gx0 + cc * s + (rr % 2) * s / 2
        y = gy0 + rr * dy
        keep_box = (x >= x0 - s) & (x <= x1 + s) & (y >= y0 - dy) & (y <= y1 + dy)
        x, y, keep_box = x.ravel(), y.ravel(), keep_box.ravel()
        pts = shapely.points(x, y)
        alive = keep_box & ~self.barriers.blocked(pts, ignore=SOFT_BARRIERS + ZONE_CLIMBED)
        steep = self.barriers.blocked(pts, only=SOFT_BARRIERS + ZONE_CLIMBED)
        if self.pass_zone is not None and steep.any():
            steep &= ~shapely.within(pts, self.pass_zone)  # in a pass zone steep ground is walked, cliffs climbed
        alive &= ~steep
        index = np.full(nrows * ncols, -1, dtype=np.int64)
        index[alive] = self.add_nodes(x[alive], y[alive], model.LATTICE)
        self.lattice_nodes = index[alive]
        grid_index = index.reshape(nrows, ncols)

        us, vs = [], []
        def link(a, b):
            ok = (a >= 0) & (b >= 0)
            us.append(a[ok])
            vs.append(b[ok])
        link(grid_index[:, :-1].ravel(), grid_index[:, 1:].ravel())
        even = grid_index[0:-1:2]
        odd = grid_index[1::2]
        n_even = min(len(even), len(odd))
        e, o = even[:n_even], odd[:n_even]
        link(e[:, 1:].ravel(), o[:, :-1].ravel())  # even (r, c) - odd (r+1, c-1)
        link(e.ravel(), o.ravel())  # even (r, c) - odd (r+1, c)
        odd2 = grid_index[1:-1:2]
        even2 = grid_index[2::2]
        n_odd = min(len(odd2), len(even2))
        o, e = odd2[:n_odd], even2[:n_odd]
        link(o.ravel(), e.ravel())  # odd (r, c) - even (r+1, c)
        link(o[:, :-1].ravel(), e[:, 1:].ravel())  # odd (r, c) - even (r+1, c+1)
        block = Block(model.LATTICE, np.concatenate(us), np.concatenate(vs))
        log.info("lattice: %s nodes (%s inside barriers), %s edges", int(alive.sum()),
                 int((~alive).sum()), len(block))
        self.blocks.append(self._keep_unblocked(block, self.node_xy()))

    # ------------------------------------------------------------ connectors

    def connectors(self) -> None:
        g = self.cfg.graph
        xy = self.node_xy()
        pairs: list[np.ndarray] = []

        def nearest(src: np.ndarray, dst: np.ndarray, k: int, radius: float) -> None:
            if len(src) == 0 or len(dst) == 0:
                return
            tree = cKDTree(xy[dst])
            dist, j = tree.query(xy[src], k=k, distance_upper_bound=radius)
            dist = dist.reshape(len(src), k)
            j = j.reshape(len(src), k)
            for col in range(k):
                ok = np.isfinite(dist[:, col])
                pairs.append(np.column_stack([src[ok], dst[j[ok, col]]]))

        lattice = self.lattice_nodes
        line = np.unique(np.concatenate(self.line_nodes)) if self.line_nodes else np.zeros(0, np.int64)
        osm = self.osm_nodes
        if len(osm):
            osm = osm[self.in_box(xy[osm, 0], xy[osm, 1])]
        line_not_osm = line[~np.isin(line, osm)] if len(line) else line

        nearest(line_not_osm, lattice, LINE_LATTICE_LINKS, g.lattice_spacing_m)
        nearest(lattice, osm, 1, g.osm_connect_radius_m)
        nearest(osm, lattice, 1, g.lattice_spacing_m)
        nearest(line_not_osm, osm, 1, g.line_osm_connect_radius_m)
        # a pass is on a crest: on to it either way
        nearest(np.asarray(self.pass_nodes, dtype=np.int64), self.ridge_nodes, 2, PASS_RIDGE_M)
        if not pairs:
            return
        both = np.concatenate(pairs)
        both = both[both[:, 0] != both[:, 1]]
        both = np.unique(np.sort(both, axis=1), axis=0)
        touches_osm = np.isin(both, self.osm_nodes).any(axis=1)
        extra = np.where(touches_osm, g.connector_penalty_s, 0.0)
        # off the valley floor onto the lattice, or back: a fall line is left
        # when it pays, not for a loop round a kink of the DEM channel
        kinds = np.concatenate(self._nkind)[both]
        leaves_thalweg = ((kinds == model.THALWEG).any(axis=1) & (kinds == model.LATTICE).any(axis=1))
        extra = extra + np.where(leaves_thalweg & ~touches_osm, g.thalweg_leave_penalty_s, 0.0)
        block = Block(model.CONNECTOR, both[:, 0], both[:, 1], extra_s=extra)
        log.info("connectors: %s (%s to OSM nodes)", len(block), int(touches_osm.sum()))
        self.blocks.append(self._keep_unblocked(block, xy))

    def fords(self) -> None:
        """A marked ford off the OSM ways, on a river too big to ford: an edge
        from the near bank through the ford to the far one."""
        if not self.osm.fords or len(self.bank_nodes) == 0:
            return
        xy = self.node_xy()
        tree = cKDTree(xy[self.bank_nodes])
        fx, fy = self.projector.to_xy([p.lon for p in self.osm.fords], [p.lat for p in self.osm.fords])
        in_water = self.barriers.blocked(shapely.points(fx, fy), only=("water",))
        us, vs, made = [], [], 0
        for p, x, y, wet in zip(self.osm.fords, fx, fy, in_water):
            if not wet or self.osm_node_of(p.id) is not None or not self.in_box(x, y):
                continue  # on a fordable stream, or on an OSM way that crosses already
            da, ia = tree.query([x, y], distance_upper_bound=FORD_SEARCH_M)
            if not np.isfinite(da):
                continue
            a = xy[self.bank_nodes[ia]]
            db, ib = tree.query([2 * x - a[0], 2 * y - a[1]], distance_upper_bound=FORD_SEARCH_M)
            if not np.isfinite(db) or ib == ia:
                continue
            b = xy[self.bank_nodes[ib]]
            if not self.barriers.blocked([LineString([a, b])], only=("water",))[0]:
                continue  # both on the same side
            node = int(self.add_nodes([x], [y], model.FORD, osm=[p.id])[0])
            us += [int(self.bank_nodes[ia]), node]
            vs += [node, int(self.bank_nodes[ib])]
            made += 1
        if made:
            self.blocks.append(Block(model.FORD, np.array(us), np.array(vs)))
        log.info("marked fords across big rivers: %s", made)

    def crossing_penalties(self) -> None:
        """Wet feet: an off-trail edge across a fordable stream pays for it -
        crossing, not ending on it."""
        if not self.crossings:
            return
        streams = np.asarray([line for line, _ in self.crossings], dtype=object)
        catch = np.array([c for _, c in self.crossings])
        tree = STRtree(streams)
        xy = self.node_xy()
        for block in self.blocks:
            if block.kind not in (model.THALWEG, model.LATTICE, model.CONNECTOR, model.BANK) \
                    or len(block) == 0 or block.wades:
                continue
            lines = shapely.linestrings(np.stack([xy[block.u], xy[block.v]], axis=1))
            hit_edge, hit_stream = tree.query(lines, predicate="crosses")
            worst = np.zeros(len(block))
            np.maximum.at(worst, hit_edge, catch[hit_stream])
            ford = self.cfg.ford
            penalty = np.where(worst > 0, ford.crossing_base_s + ford.crossing_penalty_s * np.sqrt(worst), 0.0)
            block.extra_s = penalty if block.extra_s is None else block.extra_s + penalty

    # -------------------------------------------------------------- assemble

    def assemble(self) -> Graph:
        xy = self.node_xy()
        us, vs, kinds, geoms, offs, factors, extras, ways, steeps = [], [], [], [], [], [], [], [], []
        zones: list[np.ndarray] = []
        softs: list[np.ndarray] = []
        cliffs: list[np.ndarray] = []
        base = 0
        for block in self.blocks:
            n = len(block)
            if n == 0:
                continue
            us.append(block.u)
            vs.append(block.v)
            kinds.append(np.full(n, block.kind, dtype=np.uint8))
            if block.geom_off is None:
                g = np.stack([xy[block.u], xy[block.v]], axis=1).reshape(-1, 2)
                o = np.arange(n + 1, dtype=np.int64) * 2
            else:
                g, o = block.geom_xy, block.geom_off
            geoms.append(g)
            offs.append(o[:-1] + base)
            base += len(g)
            factors.append(block.way_factor if block.way_factor is not None else np.ones(n))
            extras.append(block.extra_s if block.extra_s is not None else np.zeros(n))
            ways.append(block.osm_way if block.osm_way is not None else np.zeros(n, np.int64))
            steeps.append(block.steep_ok if block.steep_ok is not None else np.zeros(n, bool))
            zones.append(block.zone_pass if block.zone_pass is not None else np.full(n, -1, np.int32))
            softs.append(block.soft_crossed if block.soft_crossed is not None else np.zeros(n, bool))
            cliffs.append(block.cliff_crossed if block.cliff_crossed is not None else np.zeros(n, bool))
        graph = Graph(
            node_x=xy[:, 0],
            node_y=xy[:, 1],
            node_kind=np.concatenate(self._nkind),
            node_osm=np.concatenate(self._nosm),
            edge_u=np.concatenate(us).astype(np.int64),
            edge_v=np.concatenate(vs).astype(np.int64),
            edge_kind=np.concatenate(kinds),
            geom_xy=np.concatenate(geoms),
            geom_off=np.concatenate([*offs, [base]]).astype(np.int64),
            way_factor=np.concatenate(factors).astype(np.float64),
            extra_s=np.concatenate(extras).astype(np.float64),
            osm_way=np.concatenate(ways),
            steep_ok=np.concatenate(steeps),
            zone_pass=np.concatenate(zones).astype(np.int32),
            soft_crossed=np.concatenate(softs),
            cliff_crossed=np.concatenate(cliffs),
            cross_pass=np.full(sum(len(u) for u in us), -1, dtype=np.int32),
            passes=self.pass_table,
            debug=self.debug,
        )
        _drop_unlinked(graph)
        graph.cross_pass = self._cross_pass(graph)
        graph.debug["barriers"] = list(zip(self.barriers.geoms, self.barriers.labels))
        graph.debug["crossings"] = list(self.crossings)
        counts = np.bincount(graph.edge_kind, minlength=len(model.KINDS))
        log.info("graph: %s nodes, %s edges (%s)", len(graph.node_x), len(graph.edge_u),
                 ", ".join(f"{model.KINDS[k]} {c}" for k, c in enumerate(counts) if c))
        return graph
