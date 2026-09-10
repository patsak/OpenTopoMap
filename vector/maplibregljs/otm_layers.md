# otm_layers.json — layer notes

`otm_layers.json` is now strict JSON (a bare layer array, no `const` wrapper,
no comments), so this is where the annotations that used to live inline as
`//` comments are kept. Sections and layer ids below match the file.

## Overview

Port of the Garmin style `garmin/style/opentopomap-hike` + its TYP palette
`garmin/style/typ/opentopomap-hike.txt`. Icons and area hatchings are not
redrawn here: `vector/tools/typ_to_sprite.py` extracts them from that same TYP
file into `otm_sprite`, so the handheld map and the web map cannot drift.

Palette (TYP type in brackets):
- paper `#F3E6C4` [0x4b], built-up `#E8B09A` [0x10], building `#2A2A2A` [0x13]
- forest `#A8C888` / trees `#3D6B2E` [0x38 0x39 0x50]
- meadow `#DCECB8` / `#8CB868` [0x17], scrub `#BCD4A0` / `#5A8A42` [0x4f]
- mountain tundra `#C8D0A0` / stipple `#5A3318` + grass tufts `#5A8A42` [0x58]
- farmland `#F3E6C4` / `#C4A870` [0x1c], sand `#F3E6C4` / `#D4C060` [0x55]
- rock/scree/moraine `#5A3318` [0x56 0x57 0x54 0x36]
- water `#9DD4E8` [0x32 0x3c] / lines and labels `#2A6A9A` [0x18 0x1f]
- glacier `#FFFFFF` [0x4d] / crevasse `#2A6A9A` [0x34 0x35]
- roads `#C43C32` [0x01-0x03] / `#E8B84A` [0x04 0x08] / `#F5E8C0` [0x05 0x06]
- trails: track `#6B4423` [0x07 0x0a] / footpath and steps `#000000` [0x16 0x13]
- contours `#C07848` / `#A86038` / `#8B4518` [0x20-0x22]

Data from `tilemaker/process-otm.lua` (source `opentopomap-vector`), which
extends the Geofabrik schema with moraines, leaf_type, summit and pass
elevations, rtsa_scale and the hiking POI set. The ocean comes from a second
tileset (source `opentopomap-ocean`), built from coastline shapefiles.
Contours are isolines that maplibre-contour draws from the Mapterhorn
raster-dem source in the browser.

## Layer notes

**hypsometric-tint** — Hypsometric tint: OpenTopoMap's own low-zoom look
(lowland green through beige to brick-red with elevation), ported 1:1 from its
Mapnik color ramp (`mapnik/relief_color_text_file.txt` in
der-stefan/OpenTopoMap). Sits below every fill layer (forest, water,
built-up...) so it only shows through where nothing else covers the ground -
exactly like the Mapnik version, where this ramp is the base and
landuse/water paint over it. Only matters at small scale: fades out by z10,
where forest/other landuse already covers most of the visible ground and the
Genshtab paper tone (plus hillshade below) carries the relief instead, per the
"shading only" note on the hillshade layer. The 0m stop is fully transparent,
matching the source ramp's "0 0 0 0 0" row - sea level/water should show the
ocean/paper underneath, not green.

### Landcover

**fell** — natural=fell: mountain tundra above the tree line, stippled with
scattered stone dots and a few sparser grass-tuft marks, same pattern as the
Garmin map.

**bare-rock** — The Garmin bare rock fill is a dense random stipple. On a
screen that reads as noise over whole mountainsides, so it is toned down
instead of thinned out.

### Water and ice

**crevasse-poly** — No outline: a crevasse field has no edge on the ground, so
it is shown by the mapped crevasse-lines, as on the Garmin map.

**moraine-poly** — Cover moraine on ice: the same brown-dot stipple as scree,
drawn after the white glacier fill so the dots sit on the ice as on a
Genshtab sheet. A dedicated layer is required because the land "scree" layer
is below ice.

**hillshade** — Relief: shading only, no hypsometric tinting - the paper
stays the base tone.

### Ridges (khrebtovka)

Mapped crest lines, natural=ridge/arete. They carry the relief on their own
below the contour floor, where a 20 m interval would only be a brown wash —
the division the Garmin overlay makes between its 0x10 ridge symbol and the
contour symbols. Moraine crests are excluded: moraine-ridge draws them with
their own ticked symbol.

### Contours

Three-step Genshtab hierarchy derived from the elevation: every 100 m thick,
every 50 m medium, the rest thin. Isolines come from Mapterhorn in the browser
and have no ice/slope flags, so they stay brown everywhere.

`minzoom` matches the contour source, which maplibre-contour fills from z12 up.

### Waterways, moraine crests, crevasses, cliffs

**water-lines-intermittent** — line-dasharray cannot be data-driven, so
intermittent water needs its own layer.

### Roads and trails

Genshtab colours with a black casing on every driveable road; trails and
tracks are dashes without casing, exactly like the Garmin line types.

### Symbols and labels

**poi-symbols** — Sprite images already carry their intended screen size
(roughly 1.5x the Garmin bitmap, enough to stay legible); the single-tree
landmarks are large to begin with and would dominate.

**peak-labels** — `to-number` is a no-op on the tile's numeric ele and keeps
this working on a tileset that carries it as text.

**glacier-labels** — Names come from the label layer, not the fill: tilemaker
writes one centroid per named water body there, so a glacier spread over many
polygons is labelled once instead of per polygon.

**water-polygon-labels** — Lakes stay off until large scales; glaciers are
labelled by the layer above, three zooms earlier, because they are the
primary content here. The label prints the Genshtab water mark under the name:
the water surface elevation, in the same water blue, rounded to a decimetre so
a tagged `280.4` survives and float noise does not. An unnamed lake with an
`ele` gets the number alone — `process-otm.lua` writes a label centroid for it
even without a name, the way a Genshtab sheet marks the level of a nameless
tarn. The `case` chain, rather than the `concat` peak-labels uses, is what
keeps a nameless lake from getting an empty first line and a name-only lake
from getting a stray `0`.

**water-line-labels** — Its own layer, with a later per-feature floor than the
line itself: a river is drawn from z7 but only worth naming from z12.
