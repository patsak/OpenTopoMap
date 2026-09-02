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

### Garmin

Offline maps for Garmin devices. License of the Garmin maps is CC-BY-NC-SA; reselling is not allowed. Manual build: [garmin/README.md](garmin/README.md). Bbox build service: [www/garminsvc](www/garminsvc).

![screenshot1](https://raw.githubusercontent.com/der-stefan/OpenTopoMap/master/garmin/screenshots/screenshot1.png)
![screenshot2](https://raw.githubusercontent.com/der-stefan/OpenTopoMap/master/garmin/screenshots/screenshot2.png)
![screenshot3](https://raw.githubusercontent.com/der-stefan/OpenTopoMap/master/garmin/screenshots/screenshot3.png)
![screenshot4](https://raw.githubusercontent.com/der-stefan/OpenTopoMap/master/garmin/screenshots/screenshot4.png)
![screenshot5](https://raw.githubusercontent.com/der-stefan/OpenTopoMap/master/garmin/screenshots/screenshot5.png)
![screenshot6](https://raw.githubusercontent.com/der-stefan/OpenTopoMap/master/garmin/screenshots/screenshot6.png)
