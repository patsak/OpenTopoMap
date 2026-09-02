OpenTopoMap
===========

Topographic map from OpenStreetMap and DEM data: live vector tiles and Garmin `.img` / `.gmap` builds.

### Vector tiles (tilemaker + Martin)

Geofabrik extracts are kept current in place (full PBF once, then `.osc.gz` diffs) and turned into `.mbtiles` by tilemaker (`vector/tilemaker/process-otm.lua`), which Martin serves as files. Postgres holds only the pipeline's metadata. See [vector/HOWTO_vector_tiles.md](vector/HOWTO_vector_tiles.md) and [www/tilesvc](www/tilesvc).

Local stack:

```bash
cd www/garminsvc
docker compose up -d --build
docker compose run --rm tilesvc-job python -m tilesvc
docker compose restart tiles
```

Map UI: `http://localhost:8080/`. Martin: `http://localhost:3000/otm/{z}/{x}/{y}`.

### Garmin

Offline maps for Garmin devices. License of the Garmin maps is CC-BY-NC-SA; reselling is not allowed. Manual build: [garmin/README.md](garmin/README.md). Bbox build service: [www/garminsvc](www/garminsvc).

![screenshot1](https://raw.githubusercontent.com/der-stefan/OpenTopoMap/master/garmin/screenshots/screenshot1.png)
![screenshot2](https://raw.githubusercontent.com/der-stefan/OpenTopoMap/master/garmin/screenshots/screenshot2.png)
![screenshot3](https://raw.githubusercontent.com/der-stefan/OpenTopoMap/master/garmin/screenshots/screenshot3.png)
![screenshot4](https://raw.githubusercontent.com/der-stefan/OpenTopoMap/master/garmin/screenshots/screenshot4.png)
![screenshot5](https://raw.githubusercontent.com/der-stefan/OpenTopoMap/master/garmin/screenshots/screenshot5.png)
![screenshot6](https://raw.githubusercontent.com/der-stefan/OpenTopoMap/master/garmin/screenshots/screenshot6.png)
