Tools
=====

Standalone command-line helpers for preparing topographic data for OSM. They are
scripts, not a package: run them directly, each one takes `--help`. Nothing here
writes to OSM — the output is always a file for a human to review in JOSM/QGIS
first.

| Tool | What it does |
| --- | --- |
| [dem2ridges.py](dem2ridges.py) | Ridge lines (khrebtovka) from a DEM, via GRASS hydrology |
| [dem2rivers.py](dem2rivers.py) | River network from a DEM, with the Strahler hierarchy |
| [lc2veg.py](lc2veg.py) | The same polygons for a bbox, cut out of a ready-made global land cover map |

[veglib.py](veglib.py) is not a tool either: it is what the two vegetation tools
share - the class list, the two global land cover maps and how they are fetched,
and the way a class raster becomes polygons.

[demgrass.py](demgrass.py) is not a tool: it is the half the two DEM tools share
(DEM source and warp, the throwaway GRASS project, `r.watershed`, generalization,
output), so they cannot drift apart on the parts that are supposed to match.

Requirements: Python 3 with GDAL bindings (`osgeo`) — the same ones `www/otmlib`
uses — and, for the DEM tools, GRASS GIS 8. `dem2rivers.py` also needs the
`r.stream.order` addon, which is a one-time install.

```bash
brew install gdal
brew install --cask grass  # or set GRASS_BIN=/path/to/grass
```


```bash
python3 -m venv --system-site-packages tools/.venv
tools/.venv/bin/pip install -r tools/requirements.txt
tools/.venv/bin/python tools/lc2veg.py --bbox 114.0 56.20 114.2 56.35 -o veg.gpkg
```

`tools/.venv` is the path the tools themselves suggest, so run them with
`tools/.venv/bin/python` (or `source tools/.venv/bin/activate` and call them
directly). Started with the wrong interpreter they stop before doing any work and
print the command that would have worked, rather than failing with an ImportError
somewhere in the middle:

```
missing scipy in /opt/homebrew/opt/python@3.14/bin, but the venv next to the tool has them:
    …/tools/.venv/bin/python …/tools/lc2veg.py --bbox 114.0 56.20 114.2 56.35 -o veg.gpkg
```

dem2ridges.py
-------------

A ridge is a watershed of the inverted relief: flip the DEM upside down and the
crests become the valleys, so the ordinary stream-extraction chain draws them.

```bash
./dem2ridges.py --bbox 42.35 43.25 42.60 43.40 -o ridges.gpkg
./dem2ridges.py --dem ~/dem/caucasus --bbox 42.3 43.0 43.5 43.5 -o ridges.geojson
```

Pipeline:

1. **DEM** — either cropped by bbox out of the global GEDTM 30 m DTM through
   `/vsicurl` (the same source `www/otmlib/dem.py` feeds the tile and Garmin
   builds, so nothing is downloaded twice conceptually), or read from `--dem`
   (a file, or a directory of tiles mosaicked into a VRT).
2. **Warp** to a metric CRS (`--crs auto` picks the UTM zone of the centre) so a
   cell is square metres and the cell-count thresholds mean something.
3. `r.mapcalc inverted = -1.0 * dem`.
4. `r.watershed` flow accumulation on the inverted DEM. `-a` keeps the values
   positive at the region edge; `--sfd` switches MFD → D8 for crisper crests,
   `--convergence` (1–10) controls how much MFD spreads the flow.
5. `r.mapcalc seeds = if(accum > 18000, 1, 0)` — `--threshold`.
6. `r.stream.extract` on the inverted DEM with that 0/1 raster passed as
   `accumulation` and `threshold=1`, so it keeps exactly the seeded cells and
   only thins and connects them into a clean network. `--min-cells` drops short
   spurs.
7. **Sharpness filter** — hydrology also finds the watershed of a smooth dome
   (the Elbrus cone is the classic case): a divide, but no crest to draw. The
   convexity raster `dem - r.neighbors(average, --sharp-window)` is a few metres
   on such a flank and tens of metres on an arête, so `v.rast.stats` takes its
   median along every line and `--sharpness` (metres, default 10, `0` disables)
   drops the flat ones. The median survives into the output as `sharp_median`,
   so the cut can be re-checked in QGIS.
8. **Vectorize and generalize** — `r.stream.extract`'s own `stream_vector` by
   default (`--vectorizer r.to.vect` vectorizes its raster instead), then
   `v.generalize douglas` with `--simplify` (CRS units, `auto` = 1.5 cells, `0`
   keeps every vertex): a raster-traced line carries a vertex per cell, and the
   tolerance drops ~90 % of them while every vertex that survives still sits on
   a crest cell. Smoothing (chaiken, hermite) is deliberately not applied — on
   this data it adds vertices back and walks the line up to 130 m off the ridge,
   which is worse than a slightly angular line. Output goes out as
   GPKG/GeoJSON/Shapefile (driver from the extension) reprojected to EPSG:4326.

The threshold is a **cell count**, so it only means the same thing at the same
resolution: 18000 cells of 30 m ≈ 16 km² of catchment, i.e. main crests only.
Every run prints the accumulation range and the km² equivalent of the threshold,
so lowering it is a two-step loop:

```
accumulation: max 446883 cells (318.2 km2), threshold 18000 = 12.8 km2
seed cells above the threshold: 1665
line convexity (m): 2.0 / 16.1 / 39.6 / 50.3 / 84.1  [min / q1 / median / q3 / max]
sharp enough (>= 10.0 m): 24 of 27 lines
v.generalize douglas 40.0: 1405 -> 155 vertices
ridge lines: 24, 155 vertices
```

The convexity quartiles do the same job for `--sharpness`: in the run above the
three lines below 10 m are the ones crossing the Elbrus dome, while the real
arêtes sit at 30–80 m. Gentler ranges need a lower value.

Intermediates live in a work directory next to the output and are deleted unless
`--keep-work` is given; `-v` lets the GRASS modules print their own progress.


```bash
./dem2ridges.py --bbox 42.35 43.25 42.60 43.40 -o ridges.osm
```

1. In JOSM download the real OSM data for the area first — that is the layer you
   edit — then `File → Open` the `ridges.osm` on top of it.
2. Trace, one crest at a time: draw over the underlay in the OSM layer, or select
   a line in the reference layer, `Ctrl+C`, switch layer, `Ctrl+Shift+V` (paste
   at source position) and fix it up from there. Copying the whole layer in one
   go is exactly what should not happen.
3. Fix what the DEM cannot know: the network is split at every junction, so
   combine the parts of one crest into a single way (`C`); cut the line where the
   ridge really ends (a saddle, a summit, the foot of a spur) instead of where a
   segment happened to stop; delete the two-node stubs; never leave a way ending
   flush against the bbox edge.
4. Tag it: `natural=ridge`, or `natural=arete` for a rocky glacier-carved crest —
   the DEM cannot tell those apart, but a photo or an imagery layer can. Moraine
   crests are `geological=moraine` and are drawn separately (the contours overlay
   in `www/otmlib/ridges.py` already treats them as their own symbol). Add `name`
   from a topo map when the ridge has one.
5. Run the validator before uploading, and say in the changeset comment that the
   crests were drawn by hand with DEM guidance.

`sharp_median` is kept in the GPKG/GeoJSON output but deliberately dropped from
the `.osm` one — it is a working number, not an OSM tag. To re-check a doubtful
line, open the GPKG in QGIS next to the same DEM.

The result is a drafting aid for mapping `natural=ridge` by hand, not an import
candidate — DEM-derived geometry is not survey data, and OSM wants ridges mapped
deliberately, with names and sensible endpoints.

dem2rivers.py
-------------

The same chain as the ridge tool, minus the inversion: rivers are the drainage
network of the relief as it is.

```bash
./dem2rivers.py --bbox 42.0 43.3 43.0 43.8 -o rivers.gpkg
./dem2rivers.py --dem ~/dem/caucasus --catchment 5 -o brooks.gpkg
```

Pipeline: DEM and warp as above, then

1. `r.watershed` — flow accumulation on the DEM itself.
2. `r.stream.extract` — a channel starts where the catchment reaches
   `--catchment` km² (default **30**), which is the one knob that matters: 30 km²
   keeps the valley rivers, 1–5 km² fills the map with mountain brooks. Square
   kilometres rather than the ridge tool's cell count, so the same number means
   the same thing at any DEM resolution; the run prints the cell equivalent.
   `direction=` is written here too, because `r.stream.order` insists on the
   direction map coming from the very module that drew the streams.
3. `r.stream.order` — Strahler, Horton, Shreve and Hack hierarchies. Its
   `stream_vect` is the output: one feature per segment, **digitized downstream**,
   with length, straightness, sinuosity, cumulative length, source/outlet
   elevation, drop and gradient already in the table.
4. `catchment_km2` — the catchment at each segment's lower end, added from the
   accumulation raster `--catchment` was thresholded against (accumulation only
   grows downstream, so the maximum along a segment is its outlet value). This is
   what to style line width by: unlike Strahler order it does not change when the
   bbox happens to cut off a tributary. `r.stream.order` does write a `flow_accum`
   column, but its meaning is undocumented and it does not behave like an outlet
   value — a tributary can carry more of it than the trunk below the confluence.
5. Generalization and output as above, GPKG/GeoJSON/Shapefile. No `.osm` writer
   here yet.

```
accumulation: max 2263431 cells (1601.4 km2), channel starts at 30.0 km2 = 42403 cells
strahler order: 1x49, 2x24, 3x14 (601 km of channel)
segment catchment (km2): 31 / 122 / 1601  [min / median / max]
v.generalize douglas 39.9: 19172 -> 1909 vertices
river segments: 87, 1909 vertices
```

Caveats worth knowing before tracing from this: the lines cut straight across
lakes and reservoirs, they know nothing about canals, karst or dry beds, and on a
flat valley floor a 30 m DEM has no fall left to route on — a few segments come
out with `gradient = 0` and their source a decimetre *below* their outlet. The
network is a drawing aid, not survey data.

lc2veg.py
---------

The whole planet has already been classified at 10 m, twice. This tool does not
repeat the work: it cuts one of those maps to a bbox, translates its legend into
the classes a topographic map needs and hands back polygons - about five seconds
for 200 km².

```bash
./lc2veg.py --bbox 114.0 56.20 114.2 56.35 -o veg.gpkg
./lc2veg.py --bbox 114.0 56.20 114.2 56.35 --source worldcover --classes all \
            -o veg.gpkg --raster veg.tif
```

```
lcfm: ESA LCFM LCM-10 v100 (2020), 10 m
  LCFM_LCM-10_V100_2020_N54E114_MAP 2400x1800 at 114.000,56.350
area: 13.1 x 17.2 km in EPSG:32650 at 10 m
lcfm: water 4.1%, bare 0.1%, grass 7.0%, scrub 0.2%, wood 69.7%, wetland 0.1%, moss 18.9%
cleaned: water 4.0%, bare 0.0%, grass 5.8%, scrub 0.1%, wood 71.0%, wetland 0.0%, moss 19.0%
polygons: 361 kept, 0 under 1 ha dropped
  wood         146.9 km2   natural=wood
  moss          39.3 km2   natural=fell / tundra
  grass         12.0 km2   natural=grassland
  water          8.3 km2   natural=water
```

Two sources, both 10 m, both open, `--source` picks one:

| | |
| --- | --- |
| `lcfm` | ESA LCFM LCM-10, 2020 — the newer map, and the default |
| `worldcover` | ESA WorldCover v200, 2021 |

Seven classes come out, and `--classes` says which of them get polygonized —
everything but `wetland` by default, `all` adds it:

| Class | Source classes | Traced in OSM as |
| --- | --- | --- |
| `wood` | tree cover, mangroves | `natural=wood` |
| `scrub` | shrubland | `natural=scrub` |
| `grass` | grassland, cropland | `natural=grassland` |
| `moss` | moss and lichen | `natural=fell`, or `natural=tundra` in the arctic lowlands |
| `bare` | bare / sparse vegetation | `natural=bare_rock`, `natural=scree` |
| `water` | permanent water bodies | `natural=water` |
| `wetland` | herbaceous wetland | `natural=wetland` |

`moss` and `bare` are deliberately not merged: above the treeline a lichen-covered
slope and a scree field look alike from below but are different surfaces, and
`vector/tilemaker/process-otm.lua` already styles `natural=fell` apart from
`natural=scree`. Watch out for the numbering when reading the sources directly —
LCFM calls moss 70 and bare 80, WorldCover calls bare 60 and moss 100. Built-up,
snow and unclassifiable have no class here and are dropped.

### Values, not pictures

Both products are also served as WMS layers (`titiler.terrascope.be/wms`, layers
`lcfm-lcm-10_map` and `esa-worldcover-map-10m-2021-v2_map`), and a WMS answers
with a **rendered PNG**. Polygonizing that means reading the classes back out of
the colour table, which resampling and JPEG artefacts quietly corrupt — two
classes whose colours are close become one polygon, and nothing warns you.

The same data is available as data:

* **LCFM** — the COGs under `services.terrascope.be/download` want a Terrascope
  login (they answer 401), but the public titiler in front of them will cut a
  GeoTIFF of the raw class codes out of any item:
  `…/collections/lcfm-lcm-10/items/{item}/bbox/{W},{S},{E},{N}/{w}x{h}.tif?assets=MAP`.
  Which items cover a bbox comes from the STAC API at `stac.terrascope.be`, and
  the legend comes with them (`classification:classes`), so nothing is hardcoded
  by eye. The response is an uncompressed GeoTIFF — a whole degree at 10 m is
  288 MB and a minute and a half — so requests are cut into `--chunk` squares
  and mosaicked.
* **WorldCover** — plain COGs in a public S3 bucket, read in place through
  `/vsicurl`, no tile server in between.

Both are tiled in 3° squares named after their south-west corner (`N54E114`), and
a bbox that straddles a tile edge simply fetches both.

