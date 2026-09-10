OpenTopoMap
===========

Topographic map from OpenStreetMap and DEM data: live vector tiles and Garmin `.img` / `.gmap` builds.

### Vector tiles (tilemaker, on demand)

Geofabrik extracts are kept current in place (full PBF once, then `.osc.gz` diffs). Tiles are rendered from them by tilemaker (`vector/tilemaker/process-otm.lua`) per drawn bbox, on demand, into a `.pmtiles` file nginx serves. Postgres holds only the pipeline's metadata. See [www/README.md](www/README.md) and [www/tilesvc](www/tilesvc).

Local stack:

```bash
cd www
docker compose up -d --build
docker compose run --rm tilesvc-job python -m tilesvc
```

Map UI: `http://localhost:8080/`. Built previews: `http://localhost:8081/<id>.pmtiles`.

### Tools

Command-line helpers for preparing topographic data for OSM: ridge lines from a DEM (GRASS hydrology on the inverted relief). See [tools/README.md](tools/README.md).

### Garmin

Offline maps for Garmin devices. License of the Garmin maps is CC-BY-NC-SA; reselling is not allowed. Manual build: [garmin/README.md](garmin/README.md). Bbox build service: [www/garminsvc](www/garminsvc).
