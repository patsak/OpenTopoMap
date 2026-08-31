# OpenTopoMap Garmin service (`garminsvc`)

HTTP-сервис сборки Garmin `.img` / `.gmap` по bbox или загруженному OSM/PBF.

Стили mkgmap и `*_options` лежат в [`garmin/`](../../garmin) — сервис только
читает их (`OTM_GARMIN_DIR`, по умолчанию `../../garmin` от корня репозитория).

```bash
pip install -r requirements-server.txt
python download_deps.py
python server.py
# http://localhost:8080
```

Или Docker (build context — корень репозитория, чтобы забрать `garmin/style`):

```bash
docker compose up --build
```

Векторная подложка для выбора области раздаётся отдельно: Martin (`tiles` в
`docker-compose.yml`) читает `data/vector-tiles/*.mbtiles`, Flask отдаёт только
`/vector/config` и ассеты стиля. Подробности — в `vector/HOWTO_vector_tiles.md`.
