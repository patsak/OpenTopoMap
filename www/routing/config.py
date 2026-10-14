"""Areas to build routing graphs for, and the parameters of the terrain model.

Loaded from ``--config``, then ``OTM_ROUTING_CONFIG``, then the
``config.yaml`` next to this file. Every parameter has a default here; the
YAML only overrides. Unknown keys are an error rather than a silent no-op, so a
typo in a threshold cannot quietly leave the default in force.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from otmlib import paths
from routing import model

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent.parent
CONFIG_ENV = "OTM_ROUTING_CONFIG"
DEFAULT_CONFIG = ROOT / "config.yaml"


@dataclass(frozen=True)
class Area:
    """One mountain area with a graph of its own."""

    name: str
    bbox: tuple[float, float, float, float]  # west, south, east, north (EPSG:4326)
    # Local PBFs to cut the area from instead of the regions datasvc keeps
    # current (looked up in otm.regions) - for a run without the stack.
    pbf: tuple[Path, ...] = ()
    # A local DEM (file or VRT) instead of GEDTM read over HTTP.
    dem: Path | None = None
    # graph: parameters that differ here from the config-wide ones - a big
    # region is built sparser than a single massif (GraphParams names).
    graph: dict = field(default_factory=dict)
    # A region too big for one graph is cut into square tiles this many km a
    # side (routing.build.tiles), keeping those inside the Geofabrik regions
    # named in *within* and with at least min_relief_m between their lowest
    # and highest ground. The tiles become areas of their own; routing.compile
    # stitches them back into one graph.
    tile_km: float | None = None
    min_relief_m: float = 300.0
    within: tuple[str, ...] = ()
    region: str | None = None  # a tile: the region it was cut from
    # The working CRS (default: the UTM zone of the area). A region's tiles
    # all take the region's, so their lattices line up across the seams.
    epsg: int | None = None
    debug: bool = True  # write debug.gpkg (tiles: no - hundreds of MB each)


@dataclass(frozen=True)
class GraphParams:
    lattice_spacing_m: float = 150.0
    # Weight multiplier (not time) on lattice edges and connectors: of two
    # equally fast ways, prefer the valley floor, the crest, the pass descent
    # and the trail.
    lattice_penalty: float = 1.3
    # Walking the near bank of a lake or an unfordable river: preferable to
    # the open lattice, but less preferable than a thalweg/fall line.
    bank_penalty: float = 1.15
    # Added to the weight of a connector to an OSM node: stepping off a trail
    # and back on costs this much, so a route does not hop trail <-> terrain
    # for a few seconds' gain.
    connector_penalty_s: float = 60.0
    # Likewise stepping off a valley floor (thalweg, either bank) onto the
    # lattice, or back on: so a route keeps to the fall line rather than loop
    # out and back round a kink of the DEM channel.
    thalweg_leave_penalty_s: float = 120.0
    # Catchment a cell needs to be the start of a line of falling water.
    valley_catchment_km2: float = 0.2
    thalweg_node_spacing_m: float = 75.0
    osm_connect_radius_m: float = 90.0
    # Line nodes (thalweg, pass, bank) link to an OSM node this close.
    line_osm_connect_radius_m: float = 30.0
    sample_step_m: float = 20.0
    dem_res_m: float = 30.0
    # DEM and OSM are read this far beyond the area so hydrology near its edge
    # sees the whole catchment of short streams.
    margin_m: float = 2000.0
    # How big a river is gets decided on a coarse DEM reaching this far past
    # the area, so streams rising beyond the fine DEM keep their whole
    # catchment (routing.build.rivers).
    catchment_margin_m: float = 25000.0
    catchment_res_m: float = 90.0
    # grass | numpy | auto (grass when installed). numpy is a plain
    # priority-flood for small areas and tests.
    hydrology: str = "auto"
    # A pass looks for its low sides on rings out to this radius...
    pass_ring_max_m: float = 400.0
    # ...each at least this far below the pass.
    pass_min_drop_m: float = 3.0
    # Below a pass, in a sector opening pass_zone_angle_deg downhill towards
    # each valley it descends to (out to pass_zone_max_m), steepness closes
    # nothing - for the lattice, the connectors and the thalwegs alike: neither
    # max_slope_deg nor the steep patches of the DEM; an OSM cliff is climbed
    # at the dearest price (pass_cliff_*), a waterfall is gone round. Such
    # going is never slower than steep_min_speed_kmh: 100 m an hour, a hard
    # pass with fixed ropes.
    pass_zone_angle_deg: float = 45.0
    pass_zone_max_m: float = 4000.0
    # The sector's narrow end is not the pass point but a stretch of ridge
    # this long, centred on it, square to the way down.
    pass_zone_apex_m: float = 300.0
    # When a pass is ruled out by its category, do not let the route evade
    # that limit over a nearby summit. Edges at or above the saddle within
    # this radius require the same pass permission.
    pass_bypass_radius_m: float = 1200.0
    steep_min_speed_kmh: float = 0.1
    # In a pass zone a mapped cliff (natural=cliff) is no barrier either: it
    # is climbed, roped - the dearest going there is. Each edge across one
    # takes pass_cliff_time_s longer and weighs pass_cliff_penalty_s more.
    pass_cliff_time_s: float = 3600.0
    pass_cliff_penalty_s: float = 86400.0
    # Crests: the divides between the basins of streams this big (each kept
    # with its main stem, so a divide runs up spurs and along ridges, never
    # across a valley), cut into edges this long.
    ridge_catchment_km2: float = 1.0
    ridge_node_spacing_m: float = 150.0


@dataclass(frozen=True)
class FordParams:
    # A river with a larger catchment, or wider than max_width_m where OSM
    # says, is crossed only at a marked ford or a bridge.
    max_catchment_km2: float = 50.0
    max_width_m: float = 8.0
    # Each km2 of glacier upstream counts 1 + glacier_factor km2: meltwater
    # makes a glacier stream far bigger in summer than its basin suggests.
    glacier_factor: float = 3.0
    # Off trail, crossing a fordable stream costs crossing_base_s plus
    # crossing_penalty_s * sqrt(catchment) of weight: a route crosses a
    # stream when it has to, not back and forth for a few minutes' gain...
    crossing_base_s: float = 600.0
    crossing_penalty_s: float = 60.0
    # ...except for brooks below this, stepped over for free.
    free_below_km2: float = 1.0
    marked_ford_time_s: float = 300.0
    # How far from OSM water a DEM channel still counts as that same river.
    osm_match_m: float = 150.0


@dataclass(frozen=True)
class ObstacleParams:
    # Ground steeper than this cannot be walked at all.
    max_slope_deg: float = 40.0
    # DEM cliffs: connected patches steeper than this, of at least N cells.
    cliff_slope_deg: float = 45.0
    cliff_min_cells: int = 4
    # DEM canyon: a valley floor whose walls on both sides average this slope
    # over canyon_band_m and rise at least canyon_min_depth_m above it.
    canyon_wall_slope_deg: float = 38.0
    canyon_band_m: float = 60.0
    canyon_min_depth_m: float = 25.0
    canyon_min_cells: int = 3
    # key=value of OSM lines only an OSM way may cross.
    barrier_lines: tuple[str, ...] = (
        "natural=cliff",
        "barrier=wall",
        "barrier=fence",
        "railway=rail",
        "highway=motorway",
        "highway=trunk",
    )

    def barrier_pairs(self) -> tuple[tuple[str, str], ...]:
        return tuple(tuple(item.split("=", 1)) for item in self.barrier_lines)  # type: ignore[misc]


@dataclass(frozen=True)
class CrevasseParams:
    """Crevasses: not a barrier - a glacier is crossed roped up - but going
    that the route should rather avoid (routing.cost).

    * a crevasse field mapped in OSM (natural=crevasse; a line counts for
      osm_line_buffer_m either side) weighs x (1 + osm_penalty) and takes
      x (1 + osm_slowdown) as long - probing, belaying;
    * glacier steeper than glacier_from_deg is crevassed, more so the steeper
      it is, up to an icefall at glacier_full_deg: off trail it weighs up to
      x (1 + glacier_penalty).
    """

    osm_penalty: float = 3.0
    osm_slowdown: float = 0.5
    osm_line_buffer_m: float = 30.0
    glacier_from_deg: float = 12.0
    glacier_full_deg: float = 30.0
    glacier_penalty: float = 2.0


@dataclass(frozen=True)
class DescentParams:
    """Steep descents: a long steep slope is better gone down on a slant than
    straight (routing.cost). Off trail and outside pass zones (below a pass
    there is no gentler way; its grade says how hard it is), a piece
    descending more steeply than from_deg on a slope that drops at least
    min_drop_m (steeper than from_deg all the way) weighs x (1 +
    penalty_per_10deg per 10 degrees beyond) - weight, not time; a backpack makes it heavier still (pack
    steep_descent_per_10kg).
    """

    from_deg: float = 15.0
    penalty_per_10deg: float = 1.0
    min_drop_m: float = 100.0


@dataclass(frozen=True)
class RidgeParams:
    """Walking a crest (routing.cost): the sharper it is, the slower and the
    less it is worth it - a broad ridge is a highway, a knife edge is
    climbing, and down one valley and up the next may well be better.

    How sharp: the slope of the ground flank_m and 1.5 x flank_m either side
    of the crest, square to it (nearer, a 30 m DEM reads the crest's own
    cell, flat); the gentler of the two flanks counts (one can walk just
    below the crest on that side). Up to free_deg nothing changes; beyond, the going slows
    linearly to factor_at_40 at 40 degrees and weighs x (1 + penalty_per_10deg
    per 10 degrees above free_deg); a crest whose gentler flank is steeper
    than knife_deg is a knife edge: still walkable, but its weight is
    multiplied by knife_penalty on top - taken only when nothing else goes.
    """

    flank_m: float = 40.0
    free_deg: float = 20.0
    factor_at_40: float = 0.4
    penalty_per_10deg: float = 1.0
    knife_deg: float = 50.0
    knife_penalty: float = 10.0


@dataclass(frozen=True)
class SideSlope:
    # Walking across a slope: free up to free_deg, then slowing linearly to
    # factor_at_35 at 35 degrees (and no further down to max_slope_deg).
    free_deg: float = 15.0
    factor_at_35: float = 0.3
    # A traverse of steep ground is not only slow but tiring and exposed, so
    # beyond penalty_from_deg of side slope it also weighs more than it takes:
    # the weight of an off-trail piece is its time x (1 + penalty_per_10deg
    # per 10 degrees above). Time, as reported, is not changed by it.
    penalty_from_deg: float = 25.0
    penalty_per_10deg: float = 1.0


@dataclass(frozen=True)
class PackParams:
    """How a backpack slows the walk, per 10 kg carried (routing.pack).

    Applied at request time (``pack_kg``), so changing these needs no rebuild.
    Each piece of an edge takes longer by

        kg/10 x (flat + climb x up% /10 + descent x down% /10 + rough x roughness)

    where up% / down% is the gradient in per cent and roughness how much the
    ground already slows the walk (1 - terrain x side-slope factor; on a trail
    1 - its sac_scale/visibility factor): 0 on a good flat trail, 0.7 in scrub.
    """

    flat_per_10kg: float = 0.03
    climb_per_10kg: float = 0.06
    descent_per_10kg: float = 0.04
    rough_per_10kg: float = 0.15
    # the steep-descent weight (DescentParams) grows by this much per 10 kg
    steep_descent_per_10kg: float = 0.5
    max_kg: float = 40.0


DEFAULT_TERRAIN_SPEED = {
    "path": 1.0,
    "grass": 0.8,
    "heath": 0.65,
    "wood": 0.6,
    "scree": 0.5,
    "bare_rock": 0.45,
    "wetland": 0.35,
    "scrub": 0.3,
    "glacier": 0.5,
    "water": 0.0,
    "unknown": 0.7,
}

DEFAULT_OSM_SPEED = {
    "highway": {"steps": 0.5},
    "sac_scale": {
        "hiking": 1.0,
        "mountain_hiking": 0.9,
        "demanding_mountain_hiking": 0.75,
        "alpine_hiking": 0.6,
        "demanding_alpine_hiking": 0.45,
        "difficult_alpine_hiking": 0.35,
    },
    "trail_visibility": {
        "excellent": 1.0,
        "good": 1.0,
        "intermediate": 0.95,
        "bad": 0.85,
        "horrible": 0.75,
        "no": 0.65,
    },
}


@dataclass(frozen=True)
class Config:
    data_dir: Path
    # Areas with a graph each: the configured ones and the tiles of every
    # region that has been planned (routing.build.tiles writes the plans).
    areas: tuple[Area, ...]
    graph: GraphParams = GraphParams()
    ford: FordParams = FordParams()
    obstacles: ObstacleParams = ObstacleParams()
    side_slope: SideSlope = SideSlope()
    crevasse: CrevasseParams = CrevasseParams()
    descent: DescentParams = DescentParams()
    ridge: RidgeParams = RidgeParams()
    # Not part of model_params(): it is applied to a built graph per request.
    pack: PackParams = PackParams()
    terrain_speed: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_TERRAIN_SPEED))
    osm_speed: dict[str, dict[str, float]] = field(
        default_factory=lambda: {k: dict(v) for k, v in DEFAULT_OSM_SPEED.items()}
    )
    # Areas the config cuts into tiles (tile_km set), whether planned or not.
    regions: tuple[Area, ...] = ()

    @property
    def routing_dir(self) -> Path:
        return paths.routing(self.data_dir)

    def area_dir(self, name: str) -> Path:
        return self.routing_dir / name

    def area(self, name: str) -> Area:
        for area in self.areas:
            if area.name == name:
                return area
        known = ", ".join(a.name for a in self.areas) or "none"
        raise KeyError(f"unknown area {name!r} (configured: {known})")

    def region(self, name: str) -> Area | None:
        return next((r for r in self.regions if r.name == name), None)

    def tiles_path(self, region: str) -> Path:
        return self.routing_dir / f"{region}.tiles.json"

    def tiles_of(self, region: str) -> list[Area]:
        return [a for a in self.areas if a.region == region]

    def for_area(self, area: Area) -> Config:
        """This config with *area*'s own graph parameters in force."""
        if not area.graph:
            return self
        return dataclasses.replace(self, graph=_merged_graph(self.graph, area.graph, area.name))

    def model_params(self) -> dict:
        """Everything that shapes a graph, as plain JSON-able data."""
        return {
            "graph": dataclasses.asdict(self.graph),
            "ford": dataclasses.asdict(self.ford),
            "obstacles": dataclasses.asdict(self.obstacles),
            "side_slope": dataclasses.asdict(self.side_slope),
            "crevasse": dataclasses.asdict(self.crevasse),
            "descent": dataclasses.asdict(self.descent),
            "ridge": dataclasses.asdict(self.ridge),
            "terrain_speed": self.terrain_speed,
            "osm_speed": self.osm_speed,
        }

    def fingerprint(self, area: Area) -> str:
        """Changes whenever a rebuild of *area* would give another graph."""
        payload = {
            "schema": model.SCHEMA,
            "bbox": list(area.bbox),
            "dem": str(area.dem) if area.dem else None,
            "epsg": area.epsg,
            "params": self.for_area(area).model_params(),
        }
        blob = json.dumps(payload, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:16]


def config_path() -> Path:
    configured = os.environ.get(CONFIG_ENV, "").strip()
    return Path(configured) if configured else DEFAULT_CONFIG


def _dataclass_from(cls, raw: dict | None, where: str):
    raw = raw or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: expected a mapping, not {raw!r}")
    known = {f.name: f for f in dataclasses.fields(cls)}
    unknown = sorted(set(raw) - set(known))
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {', '.join(unknown)}")
    values = {}
    for name, value in raw.items():
        default = getattr(cls(), name) if name in known else None
        if isinstance(default, tuple):
            value = tuple(value)
        elif isinstance(default, bool):
            value = bool(value)
        elif isinstance(default, int):
            value = int(value)
        elif isinstance(default, float):
            value = float(value)
        values[name] = value
    return cls(**values)


def _merged_graph(base: GraphParams, overrides: dict, where: str) -> GraphParams:
    return _dataclass_from(GraphParams, {**dataclasses.asdict(base), **overrides}, f"{where}.graph")


def plan_fingerprint(region: Area) -> str:
    """What a region's tile plan depends on."""
    payload = [list(region.bbox), region.tile_km, region.min_relief_m, list(region.within)]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:16]


def _tiles(region: Area, routing_dir: Path) -> list[Area]:
    """The tiles of *region* from its plan, if one was made for these settings."""
    try:
        plan = json.loads((routing_dir / f"{region.name}.tiles.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return []
    if plan.get("plan") != plan_fingerprint(region):
        return []
    from otmlib.grass import utm_epsg

    epsg = region.epsg or utm_epsg((region.bbox[0] + region.bbox[2]) / 2, (region.bbox[1] + region.bbox[3]) / 2)
    return [
        dataclasses.replace(region, name=t["name"], bbox=tuple(t["bbox"]), tile_km=None,
                            region=region.name, debug=False, epsg=epsg)
        for t in plan.get("tiles", [])
    ]


AREA_KEYS = {"name", "bbox", "pbf", "dem", "graph", "tile_km", "min_relief_m", "within", "debug"}


def _area(raw: dict, index: int, base: Path) -> Area:
    where = f"areas[{index}]"
    if not isinstance(raw, dict) or "name" not in raw or "bbox" not in raw:
        raise ValueError(f"{where}: needs name and bbox")
    unknown = sorted(set(raw) - AREA_KEYS)
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {', '.join(unknown)}")
    bbox = tuple(float(v) for v in raw["bbox"])
    if len(bbox) != 4 or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]:
        raise ValueError(f"{where}: bbox must be [west, south, east, north]")
    pbf = raw.get("pbf") or ()
    if isinstance(pbf, str):
        pbf = (pbf,)
    dem = raw.get("dem")
    graph = dict(raw.get("graph") or {})
    _merged_graph(GraphParams(), graph, where)  # unknown keys and bad values fail here
    tile_km = raw.get("tile_km")
    if tile_km is not None and float(tile_km) < 5:
        raise ValueError(f"{where}: tile_km must be at least 5")
    within = raw.get("within") or ()
    if isinstance(within, str):
        within = (within,)
    return Area(
        name=str(raw["name"]),
        bbox=bbox,  # type: ignore[arg-type]
        pbf=tuple((base / p).resolve() for p in pbf),
        dem=(base / dem).resolve() if dem else None,
        graph=graph,
        tile_km=float(tile_km) if tile_km is not None else None,
        min_relief_m=float(raw.get("min_relief_m", 300.0)),
        within=tuple(str(w) for w in within),
        debug=bool(raw.get("debug", tile_km is None)),
    )


def _speeds(raw: dict | None, defaults: dict[str, float], where: str) -> dict[str, float]:
    merged = dict(defaults)
    for key, value in (raw or {}).items():
        merged[str(key)] = float(value)
    return merged


def from_dict(raw: dict, *, base: Path = ROOT, data_dir: Path | None = None) -> Config:
    known = {"areas", "data_dir", "graph", "ford", "obstacles", "side_slope", "crevasse", "descent", "ridge", "pack",
             "terrain_speed", "osm_speed"}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise ValueError(f"unknown top-level key(s) {', '.join(unknown)}")

    entries = tuple(_area(item, i, base) for i, item in enumerate(raw.get("areas") or []))
    names = [a.name for a in entries]
    if len(set(names)) != len(names):
        raise ValueError("area names must be unique")

    if data_dir is None:
        fallback = REPO_ROOT / "www/mapsvc/data"
        data_dir = Path(raw["data_dir"]) if raw.get("data_dir") else paths.resolve_data_dir(fallback)
    regions = tuple(a for a in entries if a.tile_km)
    areas = tuple(a for a in entries if not a.tile_km)
    for region in regions:
        areas += tuple(_tiles(region, paths.routing(data_dir)))

    terrain_speed = _speeds(raw.get("terrain_speed"), DEFAULT_TERRAIN_SPEED, "terrain_speed")
    unknown_terrain = sorted(set(terrain_speed) - set(model.TERRAINS))
    if unknown_terrain:
        raise ValueError(f"terrain_speed: unknown class(es) {', '.join(unknown_terrain)}")

    osm_speed = {k: dict(v) for k, v in DEFAULT_OSM_SPEED.items()}
    for key, table in (raw.get("osm_speed") or {}).items():
        osm_speed[str(key)] = _speeds(table, osm_speed.get(str(key), {}), f"osm_speed.{key}")

    # The A* heuristic divides distance by the top Tobler speed; a factor
    # above 1 anywhere would make it overestimate and miss the best route.
    for name, value in [*terrain_speed.items(),
                        *((f"{k}.{v}", f) for k, t in osm_speed.items() for v, f in t.items())]:
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"speed factor {name}={value} must be within 0..1")

    side_slope = _dataclass_from(SideSlope, raw.get("side_slope"), "side_slope")
    if side_slope.penalty_per_10deg < 0:
        raise ValueError("side_slope.penalty_per_10deg cannot be negative")

    crevasse = _dataclass_from(CrevasseParams, raw.get("crevasse"), "crevasse")
    if min(dataclasses.astuple(crevasse)) < 0 or crevasse.glacier_full_deg <= crevasse.glacier_from_deg:
        raise ValueError("crevasse: values cannot be negative, glacier_full_deg must exceed glacier_from_deg")

    descent = _dataclass_from(DescentParams, raw.get("descent"), "descent")
    if min(dataclasses.astuple(descent)) < 0 or descent.from_deg >= 90:
        raise ValueError("descent: values cannot be negative, from_deg must be under 90")

    ridge = _dataclass_from(RidgeParams, raw.get("ridge"), "ridge")
    if min(dataclasses.astuple(ridge)) < 0 or not ridge.free_deg < 40 < ridge.knife_deg or ridge.factor_at_40 > 1 \
            or ridge.knife_penalty < 1:
        raise ValueError("ridge: values cannot be negative, free_deg < 40 < knife_deg, factor_at_40 <= 1, "
                         "knife_penalty >= 1")

    pack = _dataclass_from(PackParams, raw.get("pack"), "pack")
    if min(dataclasses.astuple(pack)) < 0:
        raise ValueError("pack: coefficients cannot be negative")

    graph = _dataclass_from(GraphParams, raw.get("graph"), "graph")
    if graph.hydrology not in ("auto", "grass", "numpy"):
        raise ValueError("graph.hydrology must be auto, grass or numpy")
    if graph.lattice_penalty < 1 or not 1 <= graph.bank_penalty < graph.lattice_penalty \
            or graph.connector_penalty_s < 0 or graph.thalweg_leave_penalty_s < 0 \
            or graph.pass_bypass_radius_m < graph.pass_zone_apex_m / 2:
        raise ValueError("graph penalties must satisfy 1 <= bank_penalty < lattice_penalty; "
                         "connector_penalty_s and thalweg_leave_penalty_s must be >= 0; "
                         "pass_bypass_radius_m must cover half the pass apex")

    return Config(
        data_dir=data_dir,
        areas=areas,
        graph=graph,
        ford=_dataclass_from(FordParams, raw.get("ford"), "ford"),
        obstacles=_dataclass_from(ObstacleParams, raw.get("obstacles"), "obstacles"),
        side_slope=side_slope,
        crevasse=crevasse,
        descent=descent,
        ridge=ridge,
        pack=pack,
        terrain_speed=terrain_speed,
        osm_speed=osm_speed,
        regions=regions,
    )


def load(path: Path | None = None) -> Config:
    source = path or config_path()
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    try:
        return from_dict(raw, base=source.resolve().parent)
    except ValueError as exc:
        raise ValueError(f"{source}: {exc}") from None
