# OpenTopoMap Garmin service (`garminsvc`)

An HTTP service that builds Garmin `.img` / `.gmap` maps from a bbox or from an
uploaded OSM/PBF file.

The mkgmap styles and the `*_options` files live in [`garmin/`](../../garmin) —
the service only reads them (`OTM_GARMIN_DIR`, `../../garmin` from the
repository root by default). What it shares with [`tilesvc`](../tilesvc)
(Geofabrik, DEM, glaciers, contour post-processing) lives in
[`otmlib`](../otmlib): a plain package, `import otmlib`, no pip step.

The whole stack is easiest to bring up with compose — one file in
[`www/`](..) covers every service, and it also brings Postgres, tilesvc, the
preview worker and the nginx in front of all of it (`www/nginx.conf`: the
previews off the shared volume, everything else proxied here):

```bash
cd .. && docker compose up -d --build
# http://localhost:8080
```

The first `up` runs `garminsvc-init` before the service itself: it downloads the
sea and bounds data (~4.5 GB unpacked) into the shared volume and exits. That is
the slow part of a cold start, and it is deliberately not inside the server —
see [External tooling](#external-tooling) below. Watch it with
`docker compose logs -f garminsvc-init`; later `up`s find the data in place and
the container exits at once.

Locally, without Docker, you need Postgres: both the job records and the huey
queue live there.

```bash
cd ..                                        # www/, the import root
pip install -r garminsvc/requirements-server.txt
python -m garminsvc.download_deps
export DATABASE_URL=postgresql://otm:otm@localhost:5432/otm
python -m garminsvc.server
```

Everything runs from `www/`: the service is the package `garminsvc` there, next
to `otmlib` and `tilesvc` — the same layout the image has under `/app`.

The schema (`sql/001_schema.sql`, schema `otm_garmin`) is applied at startup;
huey creates its own `huey_*` tables.

## External tooling

mkgmap and splitter are pinned — version, sha256 and the files worth unpacking —
in [`artifacts.py`](artifacts.py). That manifest is the only place the versions
appear: the Dockerfile installs them by running the same code,
so the image and a checkout cannot end up on different revisions. Bumping one is
an edit to the manifest, and the checksum makes the build refuse anything else.

```bash
python -m garminsvc.fetchdeps            # both, into data/tools
python -m garminsvc.fetchdeps mkgmap --dest /opt/garmin-tools
```

Each distribution lands in a directory of its own — `data/tools/mkgmap/` holding
`mkgmap.jar`, its `lib/` and a `.otm-version` stamp. The `lib/` is not optional:
the jar's manifest `Class-Path` points at it, and without it mkgmap runs until it
first reaches for fastutil and then dies in the middle of a build. Only those
files are unpacked; the doc/ and examples/ trees stay in the archive. Set
`OTM_TOOLS_DIR` to install elsewhere — the image does, so the jars are baked in
rather than written to the shared data volume.

The sea and bounds archives are the other half, and data rather than tooling:
unversioned `*-latest.zip`, ~4.5 GB unpacked, kept in `data/` next to everything
else the two services share.

```bash
python -m garminsvc.fetchdata            # into data/sea and data/bounds
python -m garminsvc.fetchdata --check    # is it there? exit 1 if not
```

Neither step happens at request time or at startup. The server validates what it
needs (`deps.require_deps`) and refuses to boot if something is missing, naming
the command that installs it — nothing downloads from inside a gunicorn worker.
That is why the jars are a `RUN` in the Dockerfile and the data is a one-shot
`garminsvc-init` container the service depends on, and why the healthcheck no
longer needs a fifteen-minute grace period to cover a first boot.

`download_deps.py` runs both, which is what the local workflow above uses.

## Area preview

The “Preview” button builds the selected bbox in the OpenTopoMap style and shows
it on the map: an “Area preview” entry appears in the basemap dropdown next to
the public maps (OSM, OpenTopoMap, CyclOSM and the rest).

It is web cartography over the same data and the same rules (`process-otm.lua`)
that go to the device, but it is **not** an exact Garmin render: on the device
mkgmap draws with its own style and TYP. What matches is which features are
there and how the map reads overall, not the pixels.

How it works:

```
POST /preview {bbox} ──► otm.map_previews (queued) ──► huey queue "otm-preview"
                                                              │
                          tilesvc-preview: sync the regions ──► osmium extract ──►
                          tilemaker (zooms 0–14) ──► data/previews/<id>.pmtiles
                                                              │
   GET /preview/<id> ◄── status, while the built file is read by the browser
                         with range requests from nginx (`/previews/<id>.pmtiles`,
                         same origin as the page) through pmtiles://
```

The limits are deliberate:

* **Only the regions in `www/tilesvc/config.yaml`.** A bbox outside them is
  rejected, with the covered regions named: a preview is cut from the same
  extracts `tilesvc-job` keeps current, rather than downloading a fresh region
  on a button press. The map draws the outline of every downloaded region
  (`GET /regions`, the Geofabrik polygons `POST /preview` measures the bbox
  against), so where a preview is possible can be seen before the button is
  pressed rather than only in the error after it. Building an `.img` is not
  limited this way — it still takes any bbox in the world.
* **Its own queue, separate from the builds.** Otherwise a preview would wait
  out a multi-hour `.img` build: that queue has a single consumer.
* **The 8 newest previews** stay on disk, the rest are dropped along with their
  files. Asking for the same area again returns what was already built instead
  of building it twice.

The sea is not painted in a preview: the `ocean` layer is fed by shapefiles, not
by the OSM extract a preview is cut from, and the stack no longer builds an
ocean tileset. On a mountain bbox that goes unnoticed; on a coastal one the
water takes the background colour.

If a preview sits in “queued” for a long time, `tilesvc-preview` is not running
(`docker compose up -d tilesvc-preview`) — the UI says so.

## Where the data comes from

For a bbox the service works out the smallest set of Geofabrik extracts that
covers it (`otmlib.geofabrik.find_leaf_regions`), downloads them into
`data/geofabrik-cache`, brings them up to date from the `.osc.gz` diffs and cuts
the bbox out with `osmium extract -s smart`. Nothing limits this to the tiled
area — any bbox in the world can be built, but the first request in a new region
pays for downloading the extract.

The cache and the replication tracking are shared with
[`tilesvc`](../tilesvc): a region listed in `www/tilesvc/config.yaml` is already
current, and a build over it starts immediately.

Contours and crevasses on the device are built from the glacier subset of the
extract (whole polygons), not from vector tiles, which would cut a glacier along
tile borders.

The bbox picker shows the public raster maps (OSM and friends) plus, once it has
been built, the preview of the drawn area — see above. Flask serves only
`/vector/config`, the style assets and the preview records; the tiles themselves
come from nginx, which fronts both. For the cartography itself see
[`www/README.md`](../README.md).

## Tests

```bash
cd www && pytest      # garminsvc, tilesvc and otmlib in one go
```

`www/pytest.ini` puts `www` on `sys.path` — the same layout Docker gets under
`/app`, with the tests as `garminsvc.tests`. `osmium` and GDAL have to be on
PATH.

The storage and metadata tests need a real Postgres and are skipped without
`DATABASE_URL`; with it, `www/conftest.py` creates a scratch database per run:

```bash
cd www && DATABASE_URL=postgresql://otm:otm@localhost:5432/otm pytest
```
