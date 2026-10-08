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
| [s2veg.py](s2veg.py) | Wood / dwarf pine / grass for a bbox, classified from this season's Sentinel-1 and -2 |

[veglib.py](veglib.py) is not a tool either: it is what the two vegetation tools
share - the class list, the working grid, the two global land cover maps and how
they are fetched, and the way a class raster becomes polygons.

[demgrass.py](demgrass.py) is not a tool: it is the half the two DEM tools share
(DEM source and warp, the throwaway GRASS project, `r.watershed`, generalization,
output), so they cannot drift apart on the parts that are supposed to match.

Requirements: Python 3 with GDAL bindings (`osgeo`) — the same ones `www/otmlib`
uses — and, for the DEM tools, GRASS GIS 8. `dem2rivers.py` also needs the
`r.stream.order` addon, which is a one-time install. `s2veg.py` additionally
needs `pystac-client` and `scikit-learn` from `requirements.txt`, and a free
Copernicus account for the imagery (see its section below).

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
   a crest cell. Smoothing is off by default here (`--smooth 0`): chaiken on the
   douglas polyline walks the line up to 130 m off the ridge, and an angular
   crest is not a problem the way an angular river is. `--smooth N` is the river
   tool's smoothing (below), if it is wanted anyway. Output goes out as
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
5. **Smoothing** — a river has no corners, so here the line is rounded rather
   than only thinned: `--smooth` (default **2**) sliding-average passes over the
   raw cell trace take the staircase out, chaiken cuts what corners remain down
   to 0.4-cell segments, and douglas with `--simplify` (`auto` = 0.1 cell here)
   drops the near-collinear vertices chaiken leaves. The order is the point:
   chaiken on the usual 1.5-cell douglas polyline cuts each corner by a quarter
   of a long segment and moves the line up to 100 m, while averaging the trace
   first keeps it closer to the cells than the angular douglas line is (that one
   cuts the staircase along its chords). Line ends do not move, so confluences
   stay joined. Measured on 101 segments of a 30 m DEM, against the raw trace:

   | | vertices | max off the trace | bends > 60° over 60 m | > 40° |
   | --- | ---: | ---: | ---: | ---: |
   | `--smooth 0` (douglas 1.5 cells) | 2299 | 36 m | 395 | 977 |
   | `--smooth 1` | 11270 | 20 m | 28 | 298 |
   | `--smooth 2` (default) | 9066 | 29 m | 7 | 90 |
   | `--smooth 3` | 8128 | 35 m | 5 | 51 |

6. Output as above, GPKG/GeoJSON/Shapefile. No `.osm` writer here yet.

For `--bbox 116.66 55.82 117.74 56.37`, the run behind the table:

```
accumulation: max 2498379 cells (1443.8 km2), channel starts at 30.0 km2 = 51913 cells
strahler order: 1x55, 2x26, 3x20 (607 km of channel)
segment catchment (km2): 31 / 139 / 1444  [min / median / max]
v.generalize 2x sliding_averaging, chaiken 9.6, douglas 2.4: 21326 -> 9066 vertices
river segments: 101, 9066 vertices
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

s2veg.py
--------

`lc2veg.py` cuts somebody else's finished map to a bbox. This one does the
classification itself, over Sentinel scenes fetched for the bbox that was asked
for — everything, from the scene search to the training sample to the prediction,
is cut to `--bbox` before a pixel is read. Two things follow from that: the
answer is as current as the last cloud-free pass, and, given training polygons of
your own, it can hold classes the global maps do not have. **Dwarf pine**
(стланик, *Pinus pumila*) is exactly such a class — LCFM and WorldCover both fold
it into tree cover or into moss.

```bash
./s2veg.py --bbox 114.0 56.20 114.2 56.35 --year 2025 --dry-run     # no account needed
./s2veg.py --bbox 114.0 56.20 114.2 56.35 --year 2025 -o veg.gpkg
./s2veg.py --bbox 114.0 56.20 114.2 56.35 --labels stlanik.gpkg -o veg.gpkg
./s2veg.py --bbox 114.0 56.20 114.2 56.35 --method kmeans --name 4=scrub -o veg.gpkg
```

### Access

The catalogue is open; the pixels are not. Sentinel data lives in the CDSE object
store, which wants S3 keys of your own — free, from
[the keys manager](https://eodata-s3keysmanager.dataspace.copernicus.eu) with a
[Copernicus account](https://dataspace.copernicus.eu):

```bash
export CDSE_S3_ACCESS_KEY=... CDSE_S3_SECRET_KEY=...
```

`--dry-run` lists the scenes that would be composited and stops, and needs none
of this — it is the cheap way to check that a bbox and a year have usable
imagery before committing to a download. The STAC search itself goes through
`pystac_client` against `stac.dataspace.copernicus.eu/v1`; CDSE rate-limits
paged searches through its WAF, so a 429 is waited out rather than fatal.

### The eleven features, and what each is for

The three classes do not separate on colour alone — in high summer a dwarf pine
thicket and a larch stand are both simply green.

| Feature | Source | What it carries |
| --- | --- | --- |
| `B02` `B03` `B04` `B08` `B11` `B12` | S2 L2A, `--summer` window | reflectance: green, and the SWIR that moisture and leaf structure show up in |
| `NDVI` | the same composite | (B8 − B4) / (B8 + B4) — how much green |
| `NDVI_spring` | S2 L2A, `--spring` window | **the one that tells dwarf pine from forest**: by snowmelt a larch or pine already stands above the snow and its NDVI is up, while dwarf pine is still lying underneath it and reads near zero |
| `VV_db` `VH_db` `VH_VV` | S1 monthly mosaic, `--sar-months` | gamma0 backscatter: a tree crown scatters through its volume and depolarizes, a mat of dwarf pine or a meadow does not, so VH/VV drops. Crown density and roughness, which no optical band sees |

Because the spring window is there for lying snow, its composite masks clouds
only and **keeps** SCL class 11 — masking snow out would delete exactly the
pixels the feature exists to measure. The summer composite drops it.

Both S2 windows are composited the same way: per MGRS tile the `--scenes` most
usable scenes, SCL-masked (4/5/6/7 kept), then a per-band median. Per tile
because a bbox often straddles two of them and the figures the catalogue reports
are the *whole tile's* — taking the globally best scenes would happily leave one
half of the bbox uncovered. "Most usable" comes from the per-item `statistics`
the catalogue publishes, not from `eo:cloud_cover`, and the difference is not
cosmetic: a granule that is 100 % `saturated_defective` is published as **0.0 %
cloud**, so sorting by cloud puts an empty scene first and wastes a `--scenes`
slot on it. Over the Kodar bbox that is exactly what the two 14 July 2025 S2C
granules are. The median then fills what one pass missed
with another, and takes the sting out of a cloud edge SCL did not catch. Scenes
from processing baseline 04.00 onward carry a −1000 DN offset, which is added
back before anything is divided by anything.

For SAR it is the **L3 monthly global mosaics**, not GRD scenes. A GRD is in
ground-range geometry with GCPs; making one comparable between slopes means
calibration and terrain flattening against a DEM — a SNAP-sized job — and in the
mountains where dwarf pine grows, layover would dominate whatever came out of it.
The mosaics are already orthorectified gamma0 on the MGRS grid at 20 m. They are
averaged in linear power and only then turned into decibels (averaging decibels
would quietly weight the quiet pixels). `VH_VV` is the **linear** ratio and stays
linear: in decibels it would be exactly `VH_db` minus `VV_db`, an exact linear
combination of the two features beside it, and since both classifiers measure
plain distance that would count one physical property twice. Coverage is the catch: before 2025 there are gaps over
inner Siberia, and a window with no mosaic over the bbox drops the three SAR
features with a line saying so rather than failing.

### Speed

A first run over a 13 x 17 km bbox at 20 m takes about **7 minutes**, most of it
waiting on the network. Three things are what make that number rather than an
hour, and all three are worth knowing before widening a bbox:

* **Reads are tuned for a window in a big file.** An L2A band is one 100 MB jp2
  per tile and the bbox is a small window inside it; left at GDAL's defaults that
  window is fetched in 16 kB ranges through a 16 MB cache, which is thousands of
  round trips and *seven minutes for a single band*. With 10 MB chunks and a
  cache that can hold them the same band takes 40–60 seconds.
* **Scenes are read in parallel** — `--workers`, six at a time by default. The
  reads are network bound, so this is the knob that sets the wall clock.
* **Only the asset that is actually needed is fetched.** Each band is read from
  the coarsest published copy that is still finer than the working grid, so a run
  at `--res 20` takes the 20 m jp2 and not the 10 m one. `B08` is the exception —
  L2A publishes it at 10 m only — which is why `--bands B04,B8A,B11,B12` at 20 m
  is four times cheaper than the same set with `B08`, for the same answer. The
  run says so when it notices.

`--features stack.tif` writes the stack itself, one band per feature, named — the
thing to open in QGIS before blaming the classifier. `--features-in stack.tif`
reads one back and skips the imagery entirely, which is what makes iterating on
`--name` bearable: the first run downloads, every run after it is seconds and
needs no credentials at all. The grid has to match, so pass the same `--bbox`,
`--res` and `--crs`; a mismatch is refused rather than silently resampled.

### Leaving features out

Both classifiers measure plain distance, so a quantity present twice counts
twice. Every run prints what it finds:

```
feature redundancy: 4 of 7 dimensions carry 95% of the variance
  B11 and B12 are the same thing (r=+0.94)
  distance counts a repeated quantity repeatedly - consider --drop
```

On the Kodar bbox `B12` correlates 0.94 with `B11` and 0.92 with `B04`, so almost
nothing is left of it once those two are in — either drop it from the
classification with `--drop B12` or, cheaper, never fetch it (`--bands B04,B8A,B11`).
`--drop` still writes the feature to `--features`; it only keeps it out of the
distance. `NDVI` against `NDVI_spring` sits at 0.87 on that bbox, which is high
but is exactly the pair whose *difference* carries the meaning, so both stay.

### Splitting one class of a map instead of the whole bbox

`--within wood` clusters only the pixels the labels call tree cover and leaves
every other pixel with the class it was given. That is the other way to come at
dwarf pine: take a global map's word for *where the trees are* — which it is good
at — and ask the imagery only which of them are not trees at all.

```bash
./s2veg.py --bbox … --labels worldcover --within wood --method kmeans --clusters 4 \
           --name 3=scrub -o veg.gpkg
```

Every cluster then comes back named `wood` by the majority vote, since inside the
split they all are; the table and `--name` are what do the work.

**Split the classes the thing straddles, not the one it is nearest.** Both global
maps cut the dwarf pine belt roughly in half between tree cover and moss — over
the Kodar bbox, per elevation band:

| band | WorldCover `wood` / `moss` | LCFM `wood` / `moss` |
| --- | --- | --- |
| 800–1000 m | 81 % / 9 % | 86 % / 14 % |
| 1000–1200 m | 61 % / 23 % | 68 % / 29 % |
| 1200–1400 m | 47 % / 37 % | 53 % / 44 % |
| 1400–1700 m | 11 % / 70 % | 16 % / 78 % |

So `--within wood` alone hands k-means a pre-mixed set with its top chopped off,
and what comes back is elevation-diffuse — a candidate cluster at median 725 m
with its third quartile at 1052 m, which is sparse valley forest and dwarf pine
in one bag. `--within wood,moss` gives the belt whole, and the cluster lands at
746 / **1189** / 1472 m with NDVI 0.49 and VH/VV between forest and scree. Same
object the whole-bbox LCFM run found at 755 / 1169 / 1446 — two label sources and
two clustering domains agreeing is about as much confirmation as this method can
offer.

The split also cleans up after the map: 1.4 % of the bbox that WorldCover calls
tree cover comes back as bare rock (NDVI 0.21, SWIR 0.29, VH/VV −8.1 dB) and
0.7 % as shoreline water (VV −12.8 dB).

### Training once and applying to other squares

`--save-model model.joblib` keeps the fitted kNN, the feature list and the
scaling; `--model model.joblib` classifies with it instead of training, and needs
no labels at all. That is the working shape of the tool for a range rather than a
valley: draw polygons once where you know the ground, then run the saved model
over square after square.

```bash
./s2veg.py --bbox 109.97 54.82 110.46 55.05 --bands B04,B8A,B11 --texture 5 \
           --labels worldcover,train.gpkg --save-model barguzin.joblib -o veg.gpkg
./s2veg.py --bbox <another square> --bands B04,B8A,B11 --texture 5 \
           --model barguzin.joblib -o veg2.gpkg
```

The imagery for the new square still has to be fetched — the model saves the
training, not the pixels. The feature vector must match exactly, and a mismatch
is refused rather than guessed at:

```
barguzin.joblib was trained on a different feature vector.
    it wants  B04, B8A, B11, NDVI, NDVI_spring, VV_db, VH_db, VH_VV, NDVI_sd, B8A_sd
    this run has B04, B8A, B11, NDVI, NDVI_spring, VV_db, VH_db, VH_VV
    match --bands, --texture and --flatten-sar to the training run
```

Resolution, year and the date windows only warn, because a different season is a
judgement about the ground rather than a broken array.

**A transferred model is an extrapolation until checked**, so every `--model` run
measures how far the new ground is from what it was trained on — the distance to
the nearest training pixel, against the same figure measured among the training
pixels themselves:

```
distance to the nearest training pixel: median 0.43, p90 0.78
  where it was trained it was:          median 0.29, p90 0.58
```

Past 1.5x the training p90 the run says to check the result against imagery;
past 3x it says the classes are guesses.

How well it actually travels, measured: the Barguzin model applied to Kodar,
250 km away, kept the altitudinal order intact — forest at a median 666 m, dwarf
pine at 1076 m, lichen at 1458 m — and found 14.0 km2 of dwarf pine against the
18.9 km2 a model trained on Kodar itself found. But the proportions shifted
badly: 38.7 % grass against 17-19 % in the native runs, because Barguzin's grass
was taught from alpine meadow at 1700 m and the same feature values turned up on
Kodar's valley floor. Within one range and one season this is a real saving;
across ranges it is a first draft that needs a handful of local polygons to fix
the classes that drifted.

### Texture

`--texture 5` adds the local standard deviation of NDVI and of the near infrared
over a 5 x 5 window. The obvious argument for it — a forest canopy is lumpy at
this scale and a dwarf pine mat is smooth — is **backwards**, and the measured
version is more useful than the guess. Over the Kodar bbox the median NDVI spread
in that window is 0.031 under closed forest, 0.030 under meadow and **0.092** in
the dwarf pine belt. Closed forest is uniform at 20 m and so is a meadow; what is
not uniform is the belt between them, where dwarf pine, scree and patches of
tundra share a pixel. The feature finds an ecotone, not a canopy.

The second tempting claim, that texture ignores the terrain the way the SAR ratio
does not, is also false: with NDVI held constant on steep ground the medians swing
46 % of their own level between aspects (63 % for the infrared) against 17 % for
VH/VV — hillshade makes texture. It is usable because the gap between classes is
about twice that swing, not because it is clean. Watch its lower tail: on the
Kodar run it also fired on heterogeneous valley ground and pulled the tenth
percentile of the scrub class down to 601 m, which is below where any dwarf pine
belt can start.

### Flattening the SAR against the relief

`--flatten-sar` fits each SAR feature against slope and aspect by least squares,
with NDVI in the model as a covariate, and subtracts only the geometric part.
The covariate is the point: vegetation is itself organised by aspect in the
mountains (NDVI 0.43 on north-facing ground against 0.72 on south-facing over the
Kodar bbox), so a fit without it charges that difference to geometry and
normalizes away the signal.

What it is **not** is the textbook cosine correction, which cannot help here at
all: that multiplies both polarizations by the same function of the incidence
angle, and in `VH_VV` the factor cancels exactly. Measured results over Kodar:

```
VV_db: aspect spread 0.728 -> 0.058     dB
VH_db: aspect spread 1.275 -> 1.107
VH_VV: aspect spread 0.049 -> 0.042
```

So it works on the absolute channels, where the effect really is a gain, and
barely moves the ratio. The ratio's residual dependence is polarization-specific
and tracks the look direction (east is consistently the lowest quadrant), which a
monthly mosaic does not record per pixel. Removing that properly means
terrain-flattened RTC from GRD or SLC instead of these mosaics.

Which is the short answer to whether `VH_VV` alone can separate wood, scrub and
grass: it cannot. Clustered on its own it orders the ground 0.29 / 0.22 / 0.13,
and the low end is scree at 1476 m with NDVI 0.34, not grass — dwarf pine mat and
bare rock depolarize alike. It earns its place as one feature among several, not
as the discriminator.

### The elevation column

The cluster table carries median elevation per cluster, read from the same global
30 m DTM the DEM tools use. It is **not** a feature and must not become one — fed
to the classifier, height would let it draw contour lines and call them
vegetation. As a column beside the medians it is an independent witness, and in
the mountains it is the most useful one there is: dwarf pine lives in a belt
between where the forest stops and where the goltsy begin, so a cluster that sits
in that belt and nowhere else has named itself. `--no-elevation` skips the read.

### Labels

kNN is supervised, so something has to say what wood looks like. `--labels` takes
either:

* `lcfm` / `worldcover` — the global maps `lc2veg.py` uses, fetched for the same
  bbox. Convenient, and enough for wood / grass / moss / water, but **it cannot
  invent a stlanik class they do not contain**: in the boreal zone both map
  almost no shrub, so the run warns and the scrub class comes out nearly empty.
* a vector file with a `code` field (veglib's class numbers) or a `class` field
  (their names) — which is what both of these tools already write, so an
  `lc2veg.py` output can be corrected in JOSM/QGIS and fed straight back in. This
  is the way to get dwarf pine: draw twenty patches you are sure of, per class.
* **both, comma separated** — `--labels worldcover,mine.gpkg`. The map goes down
  first and the polygons on top, which is far less work than drawing all six
  classes by hand to get one of them right: the map is reliable about water,
  scree and moss and wrong only about the class you care about.

  A class the later source mentions **replaces** the earlier one's version of it
  entirely, rather than merging. This matters more than it sounds: WorldCover
  calls 60 % of a boreal bbox tree cover, so a dozen careful hand-drawn `wood`
  polygons merged into that would be three thousand random map pixels and twelve
  of yours, and the sample would never notice them. Drawing a class is a
  statement that the map is wrong about it.

Hand-drawn extents are checked against `--bbox` **before** anything is
downloaded, because labels are read after the imagery and a file drawn over the
wrong valley would otherwise announce itself at the end of a twenty minute
fetch:

```
--labels: train.gpkg does not overlap --bbox.
    the polygons are over 109.9741 54.8288 110.4598 55.0433
    the bbox is        113.9880 56.1952 114.2110 56.3547
    nothing has been downloaded; fix one of the two and run again
```

Practical sizes, learned the hard way: at 20 m with `--label-erode 1` a patch has
to be about 150 m across to survive erosion with the 50 pixels `--min-samples`
wants, and patches must be spread over aspects as well as elevations — both the
SAR ratio and the texture features swing substantially between north- and
south-facing ground (see below), so a class drawn only on sunny slopes teaches
sunny slopes.

### Two ways to get a dwarf pine class

`--method knn` (the default) is supervised: it learns each class from the labels,
so it can only ever produce classes the labels already contain. For stlanik that
means drawing the polygons yourself.

`--method kmeans` turns the problem round. k-means splits the bbox on the eleven
features alone — nothing has to have been mapped before for the split to happen,
so dwarf pine comes out as its own cluster whether or not any map knows about it
— and the global map is then asked only what to *call* each cluster, by majority
vote inside it. Naming is the part a global map is good at; splitting is the part
it is bad at, which is exactly the other way round from what is needed here.

Every run prints the table that makes the call. This is the Kodar bbox above, at
20 m, k=8:

```
clusters                           1        2        3        4        5        6        7        8
               share %           9.0     27.9      2.1     23.9     18.8      1.9     11.0      4.2
               named            moss     wood    water     wood     wood    water     moss     moss
               NDVI            0.278    0.792    0.537    0.750    0.865   -0.212    0.524    0.264
               NDVI_spring     0.229    0.670    0.275    0.500    0.754   -0.475    0.420    0.190
               VH_VV_db       -9.630   -5.731   -6.246   -6.506   -6.103   -5.487   -7.382   -8.580
```

(that run predates `VH_VV` being made a linear ratio, so it shows the dB
difference; the ordering of the clusters is the same either way)

```
  cluster 2 is labelled wood 99%, moss 0%, grass 0%
  cluster 7 is labelled moss 60%, wood 35%, grass 4%
```

Cluster 7 is a ninth of the bbox and the map cannot make up its mind about it —
60 % moss against 35 % wood, which is the signature of ground whose class the
legend does not contain. It carries real vegetation (NDVI 0.52, against 0.26–0.28
for the rock clusters), and its depolarization sits between forest (−5.7, −6.1)
and scree (−9.6): denser than a lichen slope, no crown to scatter through. That
is dwarf pine, and `--name 7=scrub` makes it so.

The check that settles it is not in the table, because elevation is deliberately
not a feature — so a DEM is an independent witness. Median elevation per cluster
over that bbox: water 592 m, valley forest 597–628 m, slope forest 744 m,
**cluster 7 at 1169 m (quartiles 755–1446)**, bare 1301 m, scree 1577 m. Cluster 7
occupies exactly the belt between where the forest stops and where the goltsy
begin, which is where dwarf pine lives.

What did *not* happen is worth saying: `NDVI_spring` for cluster 7 came out at
0.42, not near zero as the snow-cover argument predicts. The spring window that
year resolved to scenes of 25 May and 9 June, and by 9 June at 1100 m most of the
snow is already off, so the median over the window averaged the evidence away. On
this bbox the identification rests on the altitudinal belt, the SAR ratio and the
map's own confusion, not on the spring index. A narrower and earlier `--spring`
with `--scenes 1` is the way to put the index back to work.

`--name` takes `N=CLASS` pairs and overrides the vote for those clusters only;
`--clusters` sets k (default 8 — more than the classes, deliberately, so a class
is free to split into sun and shade or into two ages of forest); `--cluster-raster
ids.tif` writes the raw numbers to put next to the table in QGIS before deciding.
A cluster that falls on no labelled pixel at all is named in the log and left as
nodata until `--name` says otherwise.

The honest limit: k-means splits on distance in standardized feature space, so it
finds whatever is *separable*, not whatever is *meaningful*. Nothing guarantees
one cluster is one class — raise `--clusters` and forest splits by slope aspect
long before it splits by species. The table and the cluster raster are there
because that judgement stays yours.

### Sampling and training

Whichever it is, every labelled blob is shrunk by `--label-erode` pixels before
sampling, so only its interior trains — a label raster and an image are never
registered to the pixel, and a class boundary is mixed ground anyway. Then up to
`--samples` pixels per class are drawn at random, classes under `--min-samples`
are named and left untaught, the features are standardized (a reflectance of 0.03
and a backscatter of −15 dB are not comparable distances otherwise), and a
`--neighbors`-nearest-neighbour vote over a held-out quarter reports how well it
reproduces its own labels. What a run says about all that, in order (numbers here
are only to show the shape of it):

```
area: 13.1 x 17.2 km in EPSG:32650 at 10 m (1309 x 1722 px)
summer composite: 8 scenes, 99.4% of the bbox covered
features: B02, B03, B04, B08, B11, B12, NDVI, NDVI_spring, VV_db, VH_db, VH_VV_db
training pixels: grass 3000, moss 3000, wood 3000
  not taught, under --min-samples: bare 7
class medians                   grass     moss     wood
               NDVI             0.700    0.763    0.822
               NDVI_spring      0.551    0.558    0.602
               VH_VV_db        -7.978   -5.956   -5.500
kNN k=7: 84.1% of the held-out training pixels classified as they were labelled
```

That percentage is agreement with the labels, not accuracy on the ground: labels
drawn from a global map carry that map's own mistakes into the score. The class
medians table is the more useful line — it is where you see whether
`NDVI_spring` is in fact separating anything on *this* bbox, or whether the
snowmelt window was picked a fortnight too late.

Cleanup and output are `lc2veg.py`'s, unchanged: `--smooth` majority filter,
`--min-area` sieve, polygonize, simplify, GPKG/GeoJSON/Shapefile in EPSG:4326,
plus `--raster` for the class raster. `--classes` says what gets polygonized, and
defaults to `wood,scrub,grass` — the rest is still classified, so a lake does not
end up called grass.

The same caveat as everywhere else in this directory, only more so: a kNN over
eleven numbers is a drafting aid for `natural=wood` / `natural=scrub` /
`natural=grassland`, not an import candidate. Classify the valley, then correct
it by hand.
