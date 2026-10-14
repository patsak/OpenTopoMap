# routing — hiking routes on and off trail

`POST /route/api/v1/route` takes a few points as GeoJSON and returns a walking
route through them, in the mountains and the hills around them. The route
follows OSM trails where they help. Where there are none, it goes the way
people actually cross mountain country without a trail:

* **down and up the lines of falling water**: the valley floors (thalwegs)
  traced on the DEM, on one bank of a stream or the other - crossing it when
  that pays, not back and forth;
* **along the crests**: the divides between valleys, up spurs and along
  ridges - the broader, the easier; a sharp crest is left for its flank or
  the valley;
* **from one valley into the next over a pass**: each OSM `mountain_pass=yes`
  is joined by its fall lines to the valleys on both sides;
* **slower the steeper it is, and slower in scrub than in wood than on
  grass**: Tobler's hiking function on the gradient of travel, times a factor
  for the ground cover, times a penalty for walking across a slope; a long
  steep slope is gone down on a slant rather than straight, the more so the
  heavier the pack;
* **not across what cannot be crossed**: lakes, rivers too big to ford except
  at a marked ford or a bridge, cliffs, canyons, fences, railways.

## The graph

One graph per *area*, a mountain massif named in `config.yaml`, not a whole
Geofabrik region: the off-trail part is dense. It is built by
`python -m routing.build` from the area's OSM extract and the GEDTM 30 m DEM, in
the area's UTM zone. The DEM is read from the HGT cache the Garmin builds use
too (`otmlib.dem`, `data/dem-cache/hgt`): 1° SRTM1 tiles cut from GEDTM the
first time a degree is wanted, so a rebuild reads relief off the local disk.
The warp, both watersheds and the thalweg runs and divides taken from the fine
one are cached under `data/routing/.cache/dem`, keyed by the HGT tiles, the
grid and the catchment thresholds. A rebuild after an OSM edit reads them
back instead of warping and running `r.watershed` again. A pass's arrival
cell and a mapped lake still resplit those lines, because that depends on
the map.

A tile is rebuilt only when its schema, parameters, DEM or OSM data changed -
the data by checksum of the objects in its own cut and in the glaciers around
it, so the nightly change of a whole federal district's extract leaves the
mountain tiles nobody edited alone:

| edge kind | what it is |
| --- | --- |
| `osm` | a foot-routable OSM way, between consecutive nodes |
| `thalweg` | a valley floor: cells draining ≥ `valley_catchment_km2`, chained down the D8 pointers of GRASS `r.watershed`. Where a run lies parallel to an OSM waterway within `ford.osm_match_m`, its geometry follows the whole mapped river instead of independently matching short pieces. Along a wadable stream (above `free_below_km2`) two lines, one on each bank (8 m off); mapped streams additionally get continuous OSM-derived bank lines so hydrological run junctions cannot make a route swap banks repeatedly. Any residual thalweg edge crossing the centreline pays the normal wading penalty |
| `pass` | a fall line from a `mountain_pass` down to the valley on each low side (found on rings 60–400 m around the mapped point). It runs down to the first thalweg that stays walkable (≤ `max_slope_deg`) for 600 m downstream, not merely to the first gully. A pass is crossed **however steep**: neither `max_slope_deg` nor the DEM's steep patches close it or anything else in its zone (OSM cliffs, waterfalls, rivers, canyons and fences still do), and it is walked at Tobler's speed but never slower than `steep_min_speed_kmh` (0.1). Where a ridge is too steep for the lattice, the pass is the way over. |
| `lattice` | a triangular grid, `lattice_spacing_m` apart, so a route always exists; its weight carries `lattice_penalty` (connectors too) so the lines above win where they serve |
| `connector` | thalweg/pass/bank nodes to the lattice, the lattice to OSM nodes; stepping onto or off a trail costs `connector_penalty_s` (60 s), off a valley floor onto the lattice or back `thalweg_leave_penalty_s` (120 s) - so a route follows the fall line instead of looping out and back round a kink of the DEM channel |
| `bank` | the walkable near bank of a river too big to ford, or of a lake, offset 10 m onto dry ground. Its `bank_penalty` is lower than the lattice penalty but higher than a fall line's, so a route without a trail prefers following the shore to crossing open ground, while still preferring a thalweg |
| `ford` | a marked `ford=yes` across a river too big to ford anywhere else |
| `ridge` | a crest: the boundary between the basins of streams draining ≥ `ridge_catchment_km2`, each stream keeping its label through confluences along its main stem, so a divide runs up spurs and along ridges, never across a valley. Cut into `ridge_node_spacing_m` edges; pass nodes are joined to the crest nodes within 150 m. Like a fall line only its own gradient counts, but the sharper the crest the slower and dearer (see *Crests* below); never closed by its sharpness - the DEM's steep patches beside it are no barrier to it |

**The drawn route** (`route.py`): off trail - valley floors, pass descents, crests, the lattice and the connectors between them - the line is smoothed by corner cutting (Chaikin, 3 rounds), the run's ends kept, so the D8 staircase, the lattice's 60° zigzag and the hops between lines leave no sharp corners. Distance and time stay the edges' own. Trails and fords are drawn as mapped.

**Barriers** (`build/barriers.py`): no off-trail edge crosses any of these:
* `water`:
  * OSM water areas;
  * OSM rivers whose DEM catchment exceeds `ford.max_catchment_km2`, or whose `width` exceeds `ford.max_width_m`;
  * DEM channels that big where OSM maps no water.
* `cliff`:
  * OSM `natural=cliff`;
  * DEM patches steeper than `cliff_slope_deg`.
* `canyon`:
  * OSM gorges;
  * DEM valley floors whose walls on both sides average `canyon_wall_slope_deg` over `canyon_band_m` and rise `canyon_min_depth_m`. The floor is not walked and the corridor is not crossed.
* `waterfall`.
* `line`: the `barrier_lines` of the config.

A smaller stream is waded anywhere off trail, for `crossing_base_s + crossing_penalty_s × √catchment` of weight (600 s + 60 s × √km²: 13 min for 10 km²), free below `free_below_km2`. Only an edge that crosses the stream pays, not one ending on it; with both banks laid as lines, walking along a stream costs nothing and crossing it costs once. A mapped stream the DEM traced too is counted once, along the DEM's line. Trails cross on their bridges and fords for free.

**How big a river is** (`build/rivers.py`): "catchment" above means an *effective* catchment, with two corrections over the area's own DEM:
* **The whole basin.** The fine DEM ends `margin_m` past the area, so a stream rising beyond it came out a brook. On Elbrus, Adyl-Su and Shkhelda, which rise on the Main Ridge, measured under 10 km². Catchments are also accumulated on a coarse DEM (`catchment_res_m`, 90 m) reaching `catchment_margin_m` (25 km) further, and the larger value wins.
* **Meltwater.** Each km² of glacier upstream (OSM `natural=glacier`) counts `1 + ford.glacier_factor` times, because a glacier stream carries far more in summer than its basin suggests.
* **No barriers on the ice.** DEM channels running down the ice are neither barriers nor wading.

On Elbrus these two corrections took Adyl-Su, Adyr-Su and Shkhelda from fordable along their whole length to fordable nowhere. Of 75 random routes, 33 used to wade a river that is now too big.

**Cost** (`cost.py`): an edge is cut into `sample_step_m` pieces. On each piece:

```
v = 6·exp(−3.5·|g + 0.05|) km/h × terrain_speed[class] × f_side(cross slope)
```

* `g` is the gradient along the direction of travel, so up and down differ.
* Ground steeper than `max_slope_deg` closes an off-trail edge.
* A fall line counts only its own gradient.
* The route reports time. It is chosen by weight: time × kind penalty, plus the wading and connector costs.
* **Traverse penalty.** Off trail, a steep traverse also weighs more than it takes: past `side_slope.penalty_from_deg` (25°) of side slope, each piece's weight is its time × (1 + `penalty_per_10deg` per 10° above). The default 1.0 doubles it at 35°. Trails and fall lines are exempt. On Elbrus this cut steep traversing by a third for 0.6% more time.
* **Crests** (`ridge:`). A broad ridge is the easiest way along a range, a sharp one is climbing, and down one valley and up the next may be better. How sharp: the slope of the ground 40 and 60 m either side of the crest, square to it, the gentler side counting (one walks just below the crest there). Up to `free_deg` (20°) nothing changes; beyond, the going slows to `factor_at_40` (×0.4) at 40° and weighs ×(1 + `penalty_per_10deg` per 10°); past `knife_deg` (50°) the crest is a knife edge: still walkable, but its weight ×`knife_penalty` (10) on top, taken only when nothing else goes. With 30 m cells nearer samples read the crest's own cell, flat.
* **Steep descents** (`descent:`). Off trail and outside pass zones (below a pass there is no gentler way; its grade says how hard it is), a piece going down more steeply than `from_deg` (15°) on a slope that drops at least `min_drop_m` (100 m; every patch of ground steeper than 15° whose height spans that much) weighs extra: its time × `penalty_per_10deg` (1.0) per 10° beyond. A terrace bank or a moraine wall under 100 m is not weighed. Kept apart in the package (`steep_*`), so a backpack adds `pack.steep_descent_per_10kg` (0.5) of it per 10 kg: on the tests' scarp a 25° face is gone straight down light and round by the ramp with 30 kg.

**Cliffs on the way to a pass.** Inside a pass zone a mapped cliff (`natural=cliff`) is no barrier either: it is climbed, roped. Each edge across one takes `pass_cliff_time_s` (1 h) longer and weighs `pass_cliff_penalty_s` (a day) more - the dearest going in the model, taken only when there is no way round - and needs that pass, closing with it. Waterfalls, water, canyons and fences still close.

**Crevasses** (`crevasse:` in the config). They are not a barrier — a glacier is crossed roped up — but the route avoids them:
* **Mapped fields.** A crevasse field mapped in OSM (`natural=crevasse`; a single crevasse counts 30 m either side) weighs ×4 and takes ×1.5 as long.
* **Steep glacier.** Off trail, glacier steeper than 12° is taken as crevassed: its weight rises to ×3 at 30°, an icefall.

**Pass limits** (`passlimit.py`, request options):
* `max_pass_grade` (rtsa_scale: н/к, 1А … 3Б): no pass harder than that.
* `max_top_passes`: at most N passes of exactly that grade.
* `allow_unrated`: whether passes with no grade may be crossed.

A route **goes over** a pass when it crosses the ridge within ~200 m of the pass point, on any edge however gentle: the grade is for what the DEM does not show. Edges at or above the saddle within `pass_bypass_radius_m` (1200 m) belong to the nearest pass too, so ruling out Хатиджи 2А cannot be evaded over the 4533 m summit beside it. Edges that only a pass zone makes walkable close with that pass as well. The grade limit is a filter in the search. The N limit is met greedily: while too many top-grade passes are crossed, forbid the one whose loss costs least and route again. The answer lists the passes crossed (`passes`: name, grade).

**Backpack** (`pack.py`, request option `pack_kg`). A load is applied per request, with no rebuild. Per 10 kg, each piece of an edge takes longer by:
* `flat_per_10kg` (3%) everywhere: a good flat trail barely notices;
* `climb_per_10kg` (6%) per 10% of climb;
* `descent_per_10kg` (4%) per 10% of descent;
* `rough_per_10kg` (15%) × roughness. Roughness is 1 minus the factor the ground already slows the walk by (terrain × side slope off trail, `sac_scale`/visibility on one): 0 on a plain path, 0.7 in scrub.

So 20 kg costs +6% on a flat path, +42% up a 30% trail, +30% down it, +27% across flat scrub. The build stores each edge's time split along these lines (`climb_*`, `descent_*`, `rough_*` in the package), which makes the load a linear sum per request. The engine reweighs every arc it relaxes for the load, so a heavy pack changes the route, not only its time: the weight scales with the loaded time, and the steep-descent part grows on top. Wading and stepping off a trail do not scale, so a heavy route may rather cross a stream than walk round.

The result is a **package** in `data/routing/<area>/`, swapped in atomically:
* `graph.npz`;
* `meta.json`;
* `debug.gpkg`: edges with their speeds, barriers, passes and canyons, for QGIS.

A build is skipped while the package is current, that is, built from the same extracts with the same parameters.

```bash
python -m routing.build --area khibiny                          # synced regions, needs Postgres
python -m routing.build --area khibiny --pbf some-region.osm.pbf  # any extract, no stack
python -m routing.check --area khibiny --islands islands.geojson
```

`routing.check` reads the package (and, for the routes, the compiled graph holding it) and reports:
* connectivity, and the islands outside the largest component;
* off-trail edges that cross a barrier (exit status 1);
* passes with fewer than two descents into different valleys;
* speeds by kind and terrain;
* a sample of random routes.

## The API

```bash
curl -s localhost:8080/route/api/v1/route -H 'Content-Type: application/json' -d '{
  "type": "FeatureCollection",
  "properties": {"allow_offroad": true},
  "features": [
    {"type": "Feature", "geometry": {"type": "Point", "coordinates": [33.60, 67.70]}, "properties": {}},
    {"type": "Feature", "geometry": {"type": "Point", "coordinates": [33.80, 67.72]}, "properties": {}}
  ]}'
```

**Input.** A FeatureCollection of Points (visited in order), or a MultiPoint or LineString. Options go in `properties` or the query string:
* `allow_offroad=false` keeps the route on trails;
* `pack_kg` is the backpack, 0 to `pack.max_kg` (40);
* `max_pass_grade`, `max_top_passes`, `allow_unrated` limit the passes (see above).

**Output.** A FeatureCollection:
* **The route.** The first feature is the whole route as a 3D LineString, with `distance_m`, `duration_s`, `ascent_m`, `descent_m`, `offroad_m`, `legs[]` and the snapped `waypoints[]`.
* **Stretches.** The rest are stretches of one kind of going (`trail`, `offroad` with its `terrain`, `ford`), so a client can draw them apart.

**Errors** come back as `{"error": "..."}`:
* `400`: malformed input.
* `422`: a point off the map, or no way through (a river, a cliff, a canyon).
* `503`: the area's graph is not built yet.

**Other endpoints:**
* `GET /route/api/v1/areas`: the areas and their build state.
* `GET /route/health`.
* `GET /route/`: a debugging map to click routes on. The real UI, with an elevation profile and GPX download, is in mapsvc (see its README).

## Regions and tiles

A whole mountain system (`caucasus`, `altai`, `tunka`, `western_sayan`, `barguzin`, `south_muya`, `kodar` in `config.yaml`) is too big to build as one area, so it is built in tiles and served as one graph:

1. **Plan** (`build/tiles.py`): the region's bbox is laid out in squares of `tile_km` (60 km). A tile is kept when the relief of its part inside the Geofabrik regions of `within` (the Russian federal districts), on a ~1 km DEM, reaches `min_relief_m` - however small that part: the Russian side of the main ridge lies in tiles mostly in Georgia. The plan goes to `<routing>/<region>.tiles.json`, and its tiles are areas from then on.
2. **Build** (`routing.build --area caucasus --jobs N`): the region is cut once out of its districts' extracts, then every tile is built like any area, N at a time. Up to ~5 GB per job. Tiles of a region share one CRS (the region's UTM zone) and a lattice anchored to it, and each reaches 500 m past its edges.
3. **Compile** (`routing.compile --area caucasus`): the tiles merge into one graph. OSM nodes merge by id, the lattice and DEM nodes of the overlap strips by position, and duplicate edges go. A route crosses tiles without noticing them.

A region's valley lines are sparser than a single massif's (`graph:` in its config entry): from 0.5 km², nodes every 120 m. The lattice is 150 m everywhere. Compiled: Tunka (6 tiles) 1.2 M nodes, 6.7 M arcs, 267 MB; Altai 14 M nodes, 3.0 GB; the Caucasus 27 M nodes, 148 M arcs, 5.7 GB. Compiling reads the tiles a field at a time and writes each arc array as soon as it is made: the Caucasus and Altai together peak at 10.5 GB and take under a minute and a half.

## Engines

The service builds its answer from the graph whatever the engine. The engine only finds the path (`engine/base.py`: `route`, `edge_time`); `graphstore.ENGINES` maps `OTM_ROUTING_ENGINE` to one over a compiled graph directory. There is one:

* **disk** (default): A* in numba over a compiled graph (`routing.compile`, `data/routing/graphs/<name>/*.npy`). The arrays are opened with `mmap`, so the process holds only the pages a search reads plus its own working arrays: 9 bytes per node of the graph. The backpack and trails-only are applied while arcs are relaxed, so a load changes the route, not just its time. 2 GB of server is enough for the Caucasus.

  The compiled graph is kept lean, about 40 bytes an arc:
  * **The backpack folded in.** It is linear in the load and its coefficients are the service's config (`pack:`), not the request's, so compile stores per arc only `arc_pack_t` and `arc_pack_w`: time(kg) = time + kg/10 × pack_t, weight(kg) = weight + kg/10 × pack_w - exactly routing.pack's formulas (the tests check against Dijkstra on the package). Changing `pack:` needs `routing.compile` (seconds per region, no rebuild); the service warns if a graph was folded with other coefficients.
  * **Passes apart.** Under 1% of arcs go over or need a pass: a flag bit in `arc_kind`, and `pass_arc` / `pass_need` / `pass_over` for those.
  * **Narrow ids.** `arc_ref` and `geom_off` are 32-bit; node lon/lat are not stored but projected from `node_x/y` when a route is written.

An in-memory networkx engine and an OSRM export were tried and dropped. OSRM routes on an edge-based graph, one edge per turn, and the dense off-trail lattice blows up in it: one 60 km tile became 12 M edge-based edges, 2.7 GB of files and 4.3 GB to customize.

Valhalla, GraphHopper and BRouter were considered:
* **Valhalla:** its pedestrian costing is fixed C++ code, so our speeds cannot be passed in.
* **GraphHopper:** new edge attributes need a Java fork.
* **BRouter:** low on memory, but our directional times would have to be squeezed into discrete lookup tags, and it computes slope from its own SRTM.

## Running it

**Build machine** (the graphs; plenty of RAM and cores, no Docker needed):

```bash
routing/.venv/bin/python -m routing.build --all --jobs 6      # plan, cut, build every tile
routing/.venv/bin/python -m routing.compile --all             # one graph per area/region
```

**Server** (2 GB is enough). It needs only `data/routing/graphs/` and the routing image, or `routing.server` with numpy, numba, shapely, GDAL and flask:

```bash
rsync -a mapsvc/data/routing/graphs/ server:/srv/otm/data/routing/graphs/
OTM_DATA_DIR=/srv/otm/data gunicorn --threads 4 'routing.server:create_app()'
```

In the stack: `docker compose up -d routing datasvc-job`. The graphs are the last step of the data pipeline: datasvc's nightly job, once the Geofabrik extracts are current and the GOL is built, runs the same two commands (`datasvc.job.build_routing`, datasvc config `routing: true`, `OTM_ROUTING_JOBS` tiles at once). Only tiles whose OSM data, DEM or parameters changed are rebuilt - a checksum each on a quiet night - and only graphs whose tiles moved are compiled. A failed tile is not current, so the next night builds it; the service keeps the graphs it has meanwhile and rescans `graphs/`, remapping a recompiled graph without a restart. By hand: `docker compose run --rm datasvc-job python -m routing.build --area tunka`.

**Local development:**

```bash
python3 -m venv --system-site-packages routing/.venv   # GDAL from the system
routing/.venv/bin/pip install -r routing/requirements.txt pytest
routing/.venv/bin/python -m pytest -q routing/tests
OTM_PORT=8093 routing/.venv/bin/python -m routing.server   # then open /route/
```

The build needs GRASS 8.2+ for `r.watershed` (8.2 in the datasvc image, Debian 12). It falls back to a numpy priority-flood (`graph.hydrology: numpy`), which suits small areas and the tests.

## Limits worth knowing

* **The DEM is 30 m.** Narrow gorges, small cliffs and the exact saddle of a pass are smoothed. Cliffs come from DEM slope above 45° plus OSM; a mapped pass is searched for on widening rings.
* **Ground cover comes from OSM polygons.** Where none are mapped the class is `unknown` (×0.7). `tools/lc2veg.py` could fill those gaps from WorldCover.
* **Every off-trail edge is costed as a whole edge**, so a route reports full edges up to its waypoints. A waypoint is snapped to the nearest node: an OSM node within 50 m, otherwise any node.
