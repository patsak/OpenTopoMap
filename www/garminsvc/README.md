# OpenTopoMap Garmin service (`garminsvc`)

HTTP-сервис сборки Garmin `.img` / `.gmap` по bbox или загруженному OSM/PBF.

Стили mkgmap и `*_options` лежат в [`garmin/`](../../garmin) — сервис только
читает их (`OTM_GARMIN_DIR`, по умолчанию `../../garmin` от корня репозитория).
Общая с [`tilesvc`](../tilesvc) логика (Geofabrik, DEM, ледники, разметка
горизонталей) — в [`otmlib`](../otmlib): обычный пакет, `import otmlib`, без pip.

Полный стек проще поднимать через compose — он же приносит Postgres, tilesvc,
воркер превью и nginx для их раздачи:

```bash
docker compose up -d --build
# http://localhost:8080
```

Локально без Docker нужен Postgres: там лежат и записи задач, и очередь huey.

```bash
pip install -r requirements-server.txt
python download_deps.py
export DATABASE_URL=postgresql://otm:otm@localhost:5432/otm
python server.py
```

Схема (`sql/001_schema.sql`, схема `otm_garmin`) применяется при старте; таблицы
`huey_*` создаёт сам huey.

## Превью области

Кнопка «Превью» собирает выделенный bbox в стиле OpenTopoMap и показывает его на
карте — в списке подложек появляется пункт «Превью области» рядом с публичными
картами (OSM, OpenTopoMap, CyclOSM и остальными).

Это веб-картография того же набора данных и тех же правил (`process-otm.lua`),
что уедут на устройство, но **не** точный рендер Garmin: на приборе рисует mkgmap
со своим стилем и TYP. Совпадает состав объектов и общий вид, не пиксели.

Как это устроено:

```
POST /preview {bbox} ──► otm.map_previews (queued) ──► очередь huey «otm-preview»
                                                              │
                          tilesvc-preview: sync регионов ──► osmium extract ──►
                          tilemaker (зумы 10–14) ──► data/previews/<id>.pmtiles
                                                              │
   GET /preview/<id> ◄── статус, а готовый файл браузер читает range-запросами
                         у nginx (`previews`, порт 8081) через pmtiles://
```

Ограничения намеренные:

* **только регионы из `www/tilesvc/config.yaml`.** Bbox вне них сервис отклоняет
  с перечнем доступных регионов: превью режется из тех же экстрактов, которые
  `tilesvc-job` держит свежими, а не скачивает новый регион по нажатию кнопки.
  Сборка `.img` этим не ограничена — она по-прежнему умеет любой bbox в мире.
* **очередь своя, отдельная от сборок.** Иначе превью ждало бы за многочасовой
  сборкой `.img`: консьюмер у той очереди один.
* **последние 8 превью** остаются на диске, остальные удаляются вместе с файлами.
  Повторный запрос той же области отдаёт уже собранное, не пересобирая.

Море в превью не закрашивается: слой `ocean` живёт в отдельном тайлсете из
shapefile'ов (`otm-ocean.mbtiles`), а превью режется только из OSM-экстракта. На
горном bbox это незаметно, на приморском вода будет цвета фона.

Если превью надолго застряло в «в очереди» — не поднят `tilesvc-preview`
(`docker compose up -d tilesvc-preview`), UI об этом прямо пишет.

## Откуда берутся данные

По bbox сервис сам находит минимальный набор экстрактов Geofabrik
(`otmlib.geofabrik.find_leaf_regions`), скачивает их в `data/geofabrik-cache`,
догоняет `.osc.gz` диффами и режет bbox через `osmium extract -s smart`. Никакого
ограничения покрытием тайлов нет — собрать можно любой bbox в мире, но первый
запрос в новом регионе платит за скачивание экстракта.

Кэш и отслеживание последовательностей общие с [`tilesvc`](../tilesvc): регион из
`www/tilesvc/config.yaml` уже свежий, и сборка по нему стартует сразу.

Горизонтали и трещины на устройстве строятся из ледникового подмножества
экстракта (целые полигоны), а не из векторных тайлов, которые режут ледник по
границам тайлов.

Подложка в выборщике bbox — только векторная OpenTopoMap, тот же стиль, что уедет
на устройство: базовые тайлы от Martin, hillshade и горизонтали из Mapterhorn в
браузере. Flask отдаёт лишь `/vector/config` и ассеты стиля. Подробности —
в [`vector/HOWTO_vector_tiles.md`](../../vector/HOWTO_vector_tiles.md).

## Тесты

```bash
cd www && pytest      # garminsvc, tilesvc и otmlib разом
```

`www/pytest.ini` кладёт `www` и `www/garminsvc` на `sys.path` — ту же раскладку
даёт Docker в `/app`. Нужны `osmium` и GDAL на PATH.

Тесты хранилища и метаданных нужны настоящий Postgres и скипаются без
`DATABASE_URL`; с ним `www/conftest.py` создаёт на прогон отдельную базу:

```bash
cd www && DATABASE_URL=postgresql://otm:otm@localhost:5432/otm pytest
```
