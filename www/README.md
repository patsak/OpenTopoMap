# OpenTopoMap — vector tiles and style

The OpenTopoMap vector map (MapLibre GL JS) in Genshtab styling. The style is a
port of the `opentopomap-hike` Garmin style (`garmin/style/opentopomap-hike`
plus the TYP files `garmin/style/typ/opentopomap-hike.txt` and
`contours-hike.txt`).

## What is here

| File | Purpose |
| --- | --- |
| `tilemaker/process-otm.lua` | the rules: which tags go to which tile layer, from which zoom |
| `tilemaker/tilemaker-config-otm-region.json` | 23 OSM layers, no shapefile layers — what a preview is rendered from |
| `tilemaker/tilemaker-config-otm-ocean.json` | ocean from shapefiles; nothing in the stack builds it any more, kept for a by-hand run |
| `tilemaker/tilemaker-config-otm.json` | the full config (OSM + ocean + admin points), for a planet build |
| `tools/typ_to_sprite.py` | pulls the XPM bitmaps out of a TYP and assembles the sprite (`1x` and `@2x`) |
| `symbols/*.svg` | vector versions of individual symbols |
| `tools/validate_style.py` | checks the style against the config's layers, the sprite and the TYP palette |
| `maplibregljs/otm_layers.json` | the style itself |
| `maplibregljs/otm_style.js` | assembles the style: sources, Mapterhorn, contours, sprite |
| `www/tilesvc/` | the job: Geofabrik → PBF, kept current from the diffs |
| `www/tilesvc/preview.py` | the preview worker: bbox → osmium extract → tilemaker → `.pmtiles` |
| `www/tilesvc/sql/` | the metadata schema in Postgres (osc sequences, regions, revisions) |
| `www/otmlib/` | Geofabrik (downloads, osc, bbox cutting), DEM, metadata |
| `www/nginx.conf` | the front door: the built `.pmtiles` as files, everything else proxied to garminsvc |
| `www/garminsvc/vectorbasemap.py` | the same cartography inside the Garmin build service |

## How it works

Nothing is rendered on a schedule. The nightly job only keeps the region PBFs
current; tiles are built for one drawn bbox at a time, when someone asks for a
preview, and the result is a single `.pmtiles` file served as static content.

```
config.yaml ──► Geofabrik PBF (downloaded once)
                        │
                        ├─◄ osc.gz diffs ──► osmium apply-changes ON THE PBF ITSELF
                        │        (the last applied sequence lives in
                        │         otm.replication_state in Postgres)
                        │
                        └─► on the "Preview" button in garminsvc:
                            osmium extract bbox ──► tilemaker (zooms 0–14)
                                    ──► data/previews/<id>.pmtiles ──► nginx ──┐
                                                                                ├─► MapLibre
Mapterhorn DEM ──► hillshade + maplibre-contour (in the browser) ──────────────┘
```

A preview is a cache, so it has a lifetime: the same rectangle drawn again is
served from the file that is already there, but only for 24 hours
(`otmlib.previews.TTL_SECONDS`). Past that the row and its file are swept and
the area goes through tilemaker again — the PBF it was cut from has had a night
of diffs applied since, and `process-otm.lua` may have moved on too. On top of
the TTL, only the newest `KEEP_PREVIEWS` (8) files are kept, so a burst of
previews inside one day still cannot fill the volume. The sweep runs on the
preview worker, at startup and after every build; a preview that is past its
TTL is never served in the meantime, whether or not its file is still on disk.

There is no whole-region tileset any more. It used to be rebuilt nightly and
served by a tile server of its own, but after the picker moved to public
basemaps plus a preview of the drawn area, nothing read it: the build cost half
an hour a night and a couple of gigabytes on disk for tiles no one opened. A
preview is a plain file, so nothing serves tiles here now — nginx hands the
`.pmtiles` to the browser, which reads it with range requests.

Postgres here is a logbook and nothing more: how far the diffs have been applied
per region (`otm.replication_state`), which regions are covered and where they
are (`otm.regions` — a bbox, for the map's opening view and for refusing a
preview outside it), and the preview records themselves (`otm.map_previews`). No
tile passes through it, which is why there is no PostGIS.

The Garmin build cuts its bbox out of the same PBFs (`osmium extract -s smart`,
where `smart` keeps multipolygons whole across the bbox edge) — see
`otmlib.geofabrik`. The `data/geofabrik-cache` directory is shared with tilesvc,
and so is the sequence tracking: a region tilesvc already keeps current is only
checked by garminsvc.

Relief on the web comes from [Mapterhorn](https://mapterhorn.com/data-access/).
[maplibre-contour](https://github.com/onthegomap/maplibre-contour) computes the
isolines in the browser; below the contour floor the relief is carried by
`natural=ridge`/`arete` from `natural_lines` (from z9, see `process-otm.lua`).

## Running it (Docker)

```bash
cd www
docker compose up -d --build
docker compose run --rm tilesvc-job python -m tilesvc
```

The map is at `http://localhost:8080/`, and so is everything else: nginx is the
only service bound to a host port (`www/nginx.conf`). It serves the built
previews from `/previews/<preview id>.pmtiles` off the shared volume and proxies
the rest — the page, the API, the style assets — to garminsvc.

The first run downloads the full extracts of the regions listed in
`www/tilesvc/config.yaml` — a federal district is a few gigabytes, so it takes a
while, but only once. Later runs apply the new `.osc.gz` diffs and nothing else.

Until a region has been downloaded, previews over it are refused: there is
nothing to cut them from. The map outlines the regions that have been
downloaded (`GET /regions`), so the covered area is visible before a rectangle
is drawn.

Settings:

| Variable | Default | What it does |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://otm:otm@postgres:5432/otm` | metadata and the garminsvc jobs |
| `OTM_DATA_DIR` | `/app/data` | the data directory both services share |
| `OTM_TILEMAKER_THREADS` | cores minus two | tilemaker threads |
| `OTM_TILEMAKER_BIN` | `tilemaker` on `PATH` | a different tilemaker binary |
| `OTM_TILESVC_MEM` | `6g` | memory limit of the job container |
| `OTM_PORT` | `8080` | host port of the nginx in front of the whole stack |
| `OTM_PREVIEW_PUBLIC_URL` | `/previews` | where the browser reads previews from; an absolute URL only if they are published under a host of their own |
| `OTM_PREVIEW_MEM` | `4g` | memory limit of the preview worker |

The job takes only `--config`: bringing the PBFs and their sequences up to date
is all it does.

## Building a tileset by hand

Nothing in the stack does this any more — previews are cut per bbox — but a
whole-region tileset is still the quickest way to look at a cartography change
over a large area. tilemaker is not in Homebrew: use the
`ghcr.io/systemed/tilemaker:master` image (the same one `www/tilesvc/Dockerfile`
takes its binary from) or build from source.

```bash
cd www/garminsvc/data
wget -P geofabrik-cache https://download.geofabrik.de/russia/north-caucasus-fed-district-latest.osm.pbf

docker run --rm -v "$PWD:/data" -v "$PWD/../../../vector/tilemaker:/style:ro" \
  ghcr.io/systemed/tilemaker:master \
    --input /data/geofabrik-cache/north-caucasus-fed-district-latest.osm.pbf \
    --output /data/vector-tiles/otm.mbtiles \
    --config /style/tilemaker-config-otm-region.json \
    --process /style/process-otm.lua \
    --store /data/tilemaker-store --shard-stores
```

Note that tilemaker picks its output format from the extension: `.mbtiles` and
`.pmtiles` are single files, anything else is a directory of loose tiles.

The regional config, because the `ocean`/`ocean-low`/`boundary_labels` layers of
the full config read shapefiles that do not exist for a single region. That is
also why the sea is unpainted in a preview. The ocean tileset is built
separately and globally, if you want one:

```bash
# in data/: shapefiles/water-polygons-split-4326/, shapefiles/simplified-...
docker run --rm -v "$PWD:/data" -v "$PWD/../../../vector/tilemaker:/style:ro" \
  -w /data ghcr.io/systemed/tilemaker:master \
    --bbox -180,-85.0511287798,180,85.0511287798 \
    --output /data/vector-tiles/otm-ocean.mbtiles \
    --config /style/tilemaker-config-otm-ocean.json \
    --process /style/process-otm.lua \
    --store /data/tilemaker-store/otm-ocean --shard-stores
```

The `source:` paths in the ocean config are relative to the working directory,
hence `-w /data`. For planet or Europe use `tilemaker-config-otm.json`: it does
all of the above in one run, admin points included.

## Local dependencies

```bash
brew install osmium-tool gdal
cd www/garminsvc && python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-server.txt -r ../tilesvc/requirements.txt
pip install cairosvg pillow  # for typ_to_sprite.py's SVG symbol overrides
```

## The sprite, from the TYP

```bash
python3 vector/tools/typ_to_sprite.py
```

## Checking the style

```bash
python3 vector/tools/validate_style.py
```

The layers come from the tilemaker configs (`write_to` is expanded: `land_low`
is a zoom range of the `land` layer, not a layer of its own).

Worth remembering when editing the style — tilemaker's schema is not
"one table per layer":

* labels live in layers of their own (`water_polygons_labels`,
  `water_lines_labels`, `street_labels`) and carry `name` only where there is
  one. A `["!=", ["get", "name"], ""]` check there is unnecessary and does not
  work: for a feature without the attribute `get` returns `null`, and
  `null != ""` is true;
* `intermittent`, `tunnel`, `bridge` are booleans and are present only when
  true, so compare against `true`, not `"yes"`;
* `ele`, `population`, `admin_level` are numbers.
