# OpenTopoMap — векторные тайлы и стиль

Векторная (MapLibre GL JS) карта OpenTopoMap в генштабовском оформлении. Стиль —
порт garmin-стиля `opentopomap-hike` (`garmin/style/opentopomap-hike` и TYP-файлы
`garmin/style/typ/opentopomap-hike.txt`, `contours-hike.txt`).

## Состав

| Файл | Назначение |
| --- | --- |
| `tilemaker/process-otm.lua` | правила: какие теги в какой слой тайла, с какого зума |
| `tilemaker/tilemaker-config-otm-region.json` | 23 OSM-слоя → `otm.mbtiles` (без shapefile-слоёв) |
| `tilemaker/tilemaker-config-otm-ocean.json` | океан из shapefile'ов → `otm-ocean.mbtiles` |
| `tilemaker/tilemaker-config-otm.json` | полный конфиг (OSM + океан + admin-точки), для planet |
| `tools/typ_to_sprite.py` | извлекает XPM-битмапы из TYP и собирает спрайт (`1x` и `@2x`) |
| `symbols/*.svg` | векторные версии отдельных знаков |
| `tools/validate_style.py` | проверка стиля против слоёв конфига, спрайта и палитры TYP |
| `maplibregljs/otm_layers.json` | сам стиль |
| `maplibregljs/otm_style.js` | сборка стиля: источники, Mapterhorn, горизонтали, спрайт |
| `www/tilesvc/` | джоба: Geofabrik → PBF → tilemaker → mbtiles |
| `www/tilesvc/preview.py` | воркер превью: bbox → osmium extract → tilemaker → `.pmtiles` |
| `www/tilesvc/sql/` | схема метаданных в Postgres (последовательности osc, регионы, ревизии) |
| `www/otmlib/` | Geofabrik (скачивание, osc, вырезка bbox), DEM, метаданные |
| `www/garminsvc/garminsvc/vectorbasemap.py` | та же карта подложкой в сервисе сборки garmin-карт |

## Как это работает

Тайлы собираются заранее, а не считаются на запрос: Martin отдаёт готовые
`.mbtiles`, поэтому запрос тайла — это чтение из файла.

```
config.yaml ──► Geofabrik PBF (скачивается один раз)
                        │
                        ├─◄ osc.gz диффы ──► osmium apply-changes НА САМ PBF
                        │        (последняя применённая последовательность —
                        │         otm.replication_state в Postgres)
                        │
                        ├─► osmium merge ──► tilemaker ──► otm.mbtiles ──► Martin /otm/
                        │
                        └─► по кнопке «Превью» в garminsvc:
                            osmium extract bbox ──► tilemaker (зумы 10–14)
                                    ──► data/previews/<id>.pmtiles ──► nginx ──┐
                                                                                ├─► MapLibre
Mapterhorn DEM ──► hillshade + maplibre-contour (в браузере) ──────────────────┘
```

Тайлсет всего региона (`otm.mbtiles` через Martin) в подложках bbox-пикера больше
не показывается — там публичные карты плюс превью выделенной области. Ночная
сборка тайлсета и раздача через Martin остались как есть, для внешних потребителей.

Postgres в этой схеме — только журнал: докуда применены диффы по каждому региону
(`otm.replication_state`), какие регионы входят в тайлсет и где они
(`otm.regions` — bbox, для начального вида карты), и из какой ревизии входных
данных собран каждый тайлсет (`otm.tile_state`, чтобы ночью не пересобирать
неизменившееся). Ни один тайл через него не проходит, поэтому и PostGIS не нужен.

Garmin-сборка режет bbox из тех же PBF (`osmium extract -s smart`, ключ `smart`
держит мультиполигоны целыми на границе bbox) — см. `otmlib.geofabrik`. Кэш
`data/geofabrik-cache` общий с tilesvc, и отслеживание последовательностей тоже:
регион, который tilesvc и так держит свежим, garminsvc только проверяет.

Рельеф на вебе — [Mapterhorn](https://mapterhorn.com/data-access/). Горизонтали
[maplibre-contour](https://github.com/onthegomap/maplibre-contour) считает в браузере;
ниже зума контуров рельеф несут `natural=ridge`/`arete` из `natural_lines` (с z9 в
`process-otm.lua`).

## Запуск (Docker)

```bash
cd www/garminsvc
docker compose up -d --build
docker compose run --rm tilesvc-job python -m tilesvc
docker compose restart tiles
```

Martin: `http://localhost:3000/otm/{z}/{x}/{y}` и `/otm-ocean/{z}/{x}/{y}`;
каталог — `http://localhost:3000/catalog`.

Первый прогон скачивает полные экстракты регионов из `www/tilesvc/config.yaml`
(федеральный округ — единицы гигабайт) и строит тайлсет целиком, это часы. До
этого момента Martin перезапускается по кругу: файлов, которые он должен отдавать,
ещё нет. `docker compose restart tiles` в конце — потому что Martin открывает
mbtiles при старте и пересобранный файл сам не подхватывает.

Повторные прогоны применяют только новые `.osc.gz` и пересобирают тайлсет лишь
если что-то изменилось.

Ключи:

| Переменная | По умолчанию | Что делает |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://otm:otm@postgres:5432/otm` | метаданные и задачи garminsvc |
| `OTM_DATA_DIR` | `/app/data` | общий каталог данных обоих сервисов |
| `OTM_TILEMAKER_THREADS` | по числу ядер минус два | потоки tilemaker |
| `OTM_TILEMAKER_BIN` | `tilemaker` в `PATH` | другой бинарь tilemaker |
| `OTM_TILESVC_MEM` | `6g` | лимит памяти контейнера джобы |
| `OTM_MARTIN_CACHE_MB` | `256` | кеш Martin в памяти |
| `OTM_PREVIEW_PORT` | `8081` | порт nginx, который раздаёт `data/previews/*.pmtiles` |
| `OTM_PREVIEW_PUBLIC_URL` | `http://127.0.0.1:8081` | этот же адрес, но каким его видит браузер |
| `OTM_PREVIEW_MEM` | `4g` | лимит памяти воркера превью |
| `OTM_VECTOR_TILES_URL` | — | взять тайлы `otm` с чужого сервера вместо локального Martin |

Флаги джобы: `--sync-only` — только PBF и последовательности, без сборки тайлов;
`--recreate` — пересобрать оба тайлсета, игнорируя записанные ревизии.

## Сборка тайлсета вручную

Джоба делает это сама, но для одного региона иногда быстрее руками. tilemaker в
Homebrew нет — образ `ghcr.io/systemed/tilemaker:master` (из него же бинарь берёт
`www/tilesvc/Dockerfile`) или сборка из исходников.

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

Региональный конфиг — потому что слои `ocean`/`ocean-low`/`boundary_labels`
полного конфига читают shapefile'ы, которых для одного региона нет. Океан
собирается отдельно и глобально:

```bash
# в data/: shapefiles/water-polygons-split-4326/, shapefiles/simplified-...
docker run --rm -v "$PWD:/data" -v "$PWD/../../../vector/tilemaker:/style:ro" \
  -w /data ghcr.io/systemed/tilemaker:master \
    --bbox -180,-85.0511287798,180,85.0511287798 \
    --output /data/vector-tiles/otm-ocean.mbtiles \
    --config /style/tilemaker-config-otm-ocean.json \
    --process /style/process-otm.lua \
    --store /data/tilemaker-store/otm-ocean --shard-stores
```

Пути `source:` в ocean-конфиге относительны рабочему каталогу — отсюда `-w /data`.
Для planet/Europe берите `tilemaker-config-otm.json`: он делает то же самое одним
прогоном, включая admin-точки.

## Локальные зависимости

```bash
brew install osmium-tool gdal librsvg
cd www/garminsvc && python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-server.txt -r ../tilesvc/requirements.txt
```

## Спрайт из TYP

```bash
python3 vector/tools/typ_to_sprite.py
```

## Проверка стиля

```bash
python3 vector/tools/validate_style.py
```

Слои берутся из конфигов tilemaker (`write_to` разворачивается: `land_low` — это
диапазон зумов слоя `land`, а не отдельный слой).

Что стоит помнить при правке стиля — схема tilemaker отличается от «одна таблица
на слой»:

* подписи лежат в своих слоях (`water_polygons_labels`, `water_lines_labels`,
  `street_labels`) и содержат `name` только там, где он есть. Проверка
  `["!=", ["get", "name"], ""]` в них не нужна и не работает: у объекта без
  атрибута `get` вернёт `null`, а `null != ""` — истина;
* `intermittent`, `tunnel`, `bridge` — булевы и присутствуют только когда `true`,
  сравнивать надо с `true`, не с `"yes"`;
* `ele`, `population`, `admin_level` — числа.
