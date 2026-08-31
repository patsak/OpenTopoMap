# OpenTopoMap — векторные тайлы и стиль

Векторная (MapLibre GL JS) карта OpenTopoMap в генштабовском оформлении. Стиль —
порт garmin-стиля `opentopomap-hike` (`garmin/style/opentopomap-hike` и TYP-файлы
`garmin/style/typ/opentopomap-hike.txt`, `contours-hike.txt`).

Источником истины для оформления служит Garmin-стиль: палитра, битмапы-паттерны и
иконки берутся напрямую из TYP, а не перерисовываются вручную. Это гарантирует, что
цвета и штриховки векторной карты совпадают с тем, что видно на устройстве.

## Состав

| Файл | Назначение |
| --- | --- |
| `tilemaker/process-otm.lua` | обработка OSM: морены, `leaf_type`, `ele`, перевалы, фильтр POI, ранние зумы троп |
| `tilemaker/tilemaker-config-otm.json` | конфиг слоёв (с океаном и admin-точками из shapefile) |
| `tilemaker/tilemaker-config-otm-region.json` | то же без shapefile — для региональных вырезок вроде Кавказа |
| `tools/typ_to_sprite.py` | извлекает XPM-битмапы из TYP и собирает спрайт (`1x` и `@2x`) |
| `symbols/*.svg` | векторные версии отдельных знаков, перекрывают битмап из TYP |
| `tools/build_contours.py` | серверные горизонтали с атрибутами `on_glacier` / `steep` и трещинами |
| `tools/validate_style.py` | проверка стиля: дубли слоёв, отсутствующие спрайты и source-layer, цвета вне палитры TYP |
| `maplibregljs/otm_layers.json` | сам стиль (51 слой) |
| `maplibregljs/otm_style.js` | сборка стиля целиком: источники, DEM, горизонтали, спрайт |
| `maplibregljs/index.html` | демо-страница |
| `www/garminsvc/garminsvc/vectorbasemap.py` | та же карта подложкой в сервисе сборки garmin-карт |

## Сборка

Все команды ниже — из корня репозитория `OpenTopoMap/`, если не сказано иначе.

### 0. Зависимости

```bash
# macOS
brew install osmium-tool tippecanoe librsvg

# tilemaker в Homebrew нет — удобнее Docker:
#   docker pull ghcr.io/systemed/tilemaker:master
# или собрать из https://github.com/systemed/tilemaker

# Python для горизонталей (тот же venv, что у garminsvc):
cd www/garminsvc && python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-server.txt   # в т.ч. pyhgtmap / npyosmium
```

`tippecanoe` и `osmium` нужны только для серверных горизонталей; базовую карту
собирает один tilemaker.

### 1. Спрайт из TYP

```bash
python3 vector/tools/typ_to_sprite.py
```

Скрипт читает `garmin/style/typ/opentopomap-hike.txt`, конвертирует битмапы в PNG
(чистый Python, без Pillow) и укладывает их в `maplibregljs/otm_sprite.png` и
`otm_sprite@2x.png` с соответствующими JSON. Битмапы масштабируются вдвое
относительно garmin-размера, иначе на экране они нечитаемо мелкие.

Если в `vector/symbols/` есть SVG с именем знака, берётся он, а не битмап из TYP
(нужен `rsvg-convert`). Размер задан в SVG: вершина 14×12, перевал 14×11,
стоянка 22×22, хижина/приют 20×20. У хижины (`0x661e`) и `hut` (`0x2b07`)
битмапы совпадают — одного SVG достаточно на оба имени.

### 2. Пример: Северный Кавказ

Ниже — полный проход от Geofabrik до mbtiles, на котором удобно проверять
ледники Эльбруса, морены, трещины, перевалы и хижины. Вырезка
`north-caucasus-fed-district` весит порядка сотен мегабайт и собирается на
ноутбуке за разумное время; для Европы рецепт тот же, только PBF больше и
нужны shapefile океана (см. §2.5).

#### 2.1. Скачать OSM

```bash
mkdir -p www/garminsvc/data/geofabrik-cache
cd www/garminsvc/data/geofabrik-cache
wget https://download.geofabrik.de/russia/north-caucasus-fed-district-latest.osm.pbf
cd ../../..
```

Если файл уже лежит в кэше garminsvc — этот шаг можно пропустить.

#### 2.2. Собрать векторные тайлы (tilemaker)

Полный `tilemaker-config-otm.json` тянет shapefile океана и admin-точек
(shortbread). Для континентальной вырезки они не нужны и только мешают
(каталога `data/water-polygons-…` нет). Используйте региональный конфиг без
этих слоёв:

```bash
mkdir -p www/garminsvc/data/vector-tiles
PBF=www/garminsvc/data/geofabrik-cache/north-caucasus-fed-district-latest.osm.pbf
OUT=www/garminsvc/data/vector-tiles/otm.mbtiles

# Docker: рабочий каталог репозитория монтируется в /data
docker run --rm -v "$PWD":/data -w /data ghcr.io/systemed/tilemaker:master \
  /data/$PBF \
  --config /data/vector/tilemaker/tilemaker-config-otm-region.json \
  --process /data/vector/tilemaker/process-otm.lua \
  --output /data/$OUT \
  --store /data/www/garminsvc/data/tilemaker-store \
  --shard-stores

# или локальный бинарник:
# tilemaker "$PBF" \
#   --config vector/tilemaker/tilemaker-config-otm-region.json \
#   --process vector/tilemaker/process-otm.lua \
#   --output "$OUT" \
#   --store www/garminsvc/data/tilemaker-store --shard-stores
```

`--store` — временный диск под промежуточные данные; после успешной сборки его
можно удалить. Если не хватает RAM, уберите `--compact`, если он есть в вашей
сборке tilemaker, и увеличьте `--store` на быстрый диск.

На выходе: `www/garminsvc/data/vector-tiles/otm.mbtiles` — сама карта (OSM-слои
из `process-otm.lua`). Без горизонталей её уже можно смотреть: клиент посчитает
изолинии из DEM mapterhorn сам.

#### 2.3. Серверные горизонтали и трещины

Клиентские изолинии не знают ледник и крутизну. Полное garmin-поведение —
`pyhgtmap` → `contour_post` для горизонталей и отдельно `crevasse` для трещин,
затем `build_contours.py`.

Трещины больше не режутся из DEM-горизонталей: для каждой площадной
`natural=crevasse` берётся хост-ледник (`natural=glacier`, который её покрывает)
и штрихи кладутся **перпендикулярно** его тегу `direction` (кардинальные
румбы или градусы). DEM для объектов карты не нужен.

Горизонтали по-прежнему из DEM — тайлы SRTM `.hgt` в
`www/garminsvc/data/dem-cache/hgt/`. Их качает сам garminsvc при сборке bbox;
для ручного прогона Эльбруса достаточно одного градуса `N43E042`
(Эльбрус ≈ 43.35°N, 42.44°E). Можно положить готовый файл или один раз
собрать маленькую карту через UI garminsvc вокруг Приэльбрусья — HGT останутся
в кэше.

Минимум для проверки ледников (один градус):

```bash
mkdir -p www/garminsvc/data/contours-caucasus
cd www/garminsvc
. .venv/bin/activate
export PYTHONPATH=.
export MPLCONFIGDIR=/tmp/mpl   # pyhgtmap трогает matplotlib

python3 - <<'PY'
from pathlib import Path
import os
from garminsvc.contour_terrain import contour_step
from garminsvc.contour_post import postprocess_contour_pbfs
from garminsvc.crevasse import build_crevasse_stripes
from pyhgtmap.main import main_internal

repo = Path('.').resolve()
hgt = repo / 'data/dem-cache/hgt/N43E042.hgt'
pbf = repo / 'data/geofabrik-cache/north-caucasus-fed-district-latest.osm.pbf'
out = repo / 'data/contours-caucasus'
out.mkdir(parents=True, exist_ok=True)
for stale in out.glob('*.osm.pbf'):
    stale.unlink()

step = contour_step(hgt)          # 10 м на равнине, 20 м в горах
prev = os.getcwd()
os.chdir(out)
try:
    main_internal([
        f'--step={step}', '--line-cat=100,50', '--pbf',
        '--start-node-id=10000000', '--start-way-id=10000000',
        f'--output-prefix=contours-{hgt.stem}', str(hgt),
    ])
finally:
    os.chdir(prev)

pbfs = sorted(out.glob(f'contours-{hgt.stem}*.osm.pbf'))
postprocess_contour_pbfs(pbfs, pbf, [hgt])   # glacier=yes, steep=yes
build_crevasse_stripes(pbf, out / 'crevasse-stripes.osm')  # glacier direction, no DEM
print('contours:', len(pbfs), 'files in', out)
PY
```

Чтобы покрыть весь федеральный округ, передайте в pyhgtmap все `.hgt` из
`data/dem-cache/hgt/`, которые пересекают вырезку (примерно N41–N45, E37–E49),
с разными `--start-node-id` / `--start-way-id` на каждый тайл — как делает
`garminsvc.pipeline.build_contour_pbfs` (шаг id 1.5M на тайл). Проще всего
собрать bbox всего округа через UI garminsvc и забрать уже готовые
`data/jobs/<id>/build/data/contours-hike/*.osm.pbf` и
`data/jobs/<id>/build/data/crevasse-stripes.osm`.

Собрать mbtiles из tagged-контуров:

```bash
# из корня репозитория
python3 vector/tools/build_contours.py \
  www/garminsvc/data/contours-caucasus/*.osm.pbf \
  --crevasses www/garminsvc/data/contours-caucasus/crevasse-stripes.osm \
  --output www/garminsvc/data/vector-tiles/otm-contours.mbtiles
```

Скрипт сам делает `osmium sort` (pyhgtmap пишет неупорядоченные PBF) и гоняет
`tippecanoe`. На выходе два слоя: `contours` (`ele`, `level`, `on_glacier`,
`steep`) и `crevasses` (`type`, `width`).

#### 2.4. Посмотреть результат

**Через garminsvc + Martin** (подложка в выборщике bbox):

```bash
cd www/garminsvc && docker compose up --build
# UI:    http://localhost:8080  →  Подложка → «OpenTopoMap (вектор…)»
# tiles: http://localhost:3000/otm/{z}/{x}/{y}
#        http://localhost:3000/otm-contours/{z}/{x}/{y}
```

Имена файлов фиксированы: `data/vector-tiles/otm.mbtiles` и
`otm-contours.mbtiles`. Центр карты: Эльбрус ≈ `43.35, 42.45`, зум 12–14.

**Отдельно, без сервиса** — поднять Martin на mbtiles и указать URL в
`index.html` / `otmVectorStyle({ tiles, contours })`, либо временно прописать
локальные шаблоны в демо-странице.

Проверка стиля (без тайлов):

```bash
python3 vector/tools/validate_style.py
```

#### 2.5. Европа / мир целиком

Для континента нужен полный конфиг со shapefile:

```bash
git clone https://github.com/shortbread-tiles/shortbread-tilemaker
cd shortbread-tilemaker && ./get-shapefiles.sh
# положите/symlink data/water-polygons-… рядом с вызовом tilemaker

wget https://download.geofabrik.de/europe-latest.osm.pbf
# при огромном PBF иногда помогает: osmium renumber -v … -o …-renumbered.osm.pbf

tilemaker europe-latest.osm.pbf \
  --config /path/to/OpenTopoMap/vector/tilemaker/tilemaker-config-otm.json \
  --process /path/to/OpenTopoMap/vector/tilemaker/process-otm.lua \
  --output europe.mbtiles \
  --store tilemaker.store.d --shard-stores
```

### 3. DEM как COG (опционально)

Hillshade в стиле по умолчанию идёт из terrarium-тайлов mapterhorn. Свой COG
нужен только если хотите свой DEM-сервер:

```bash
gdalwarp input.tif output.tif -of COG -co BLOCKSIZE=256 \
  -co TILING_SCHEME=GoogleMapsCompatible -co COMPRESS=DEFLATE \
  -co RESAMPLING=BILINEAR -co OVERVIEW_RESAMPLING=NEAREST \
  -co OVERVIEWS=IGNORE_EXISTING -co ADD_ALPHA=NO -dstnodata NaN
```

### 4. Подложка в garminsvc

После §2 файлы уже лежат в `www/garminsvc/data/vector-tiles/` под нужными
именами — достаточно `docker compose up` (см. §2.4).

Тайлы раздаёт Martin, Flask их не проксирует. Публичный URL Martin —
`OTM_MARTIN_PUBLIC_URL` (по умолчанию `http://127.0.0.1:3000`). Вместо локальных
файлов можно указать чужой тайл-сервер через `OTM_VECTOR_TILES_URL` и
`OTM_VECTOR_CONTOURS_URL` (шаблон `https://host/{z}/{x}/{y}`) — они приоритетнее
mbtiles. Если нет ни того, ни другого, векторная подложка в списке не появляется.

garminsvc отдаёт только `/vector/config` и `/vector/assets/…` (стиль и спрайт).
MapLibre в выборщике грузится лениво при первом выборе подложки.

Стиль собирается общей `otmVectorStyle` из `maplibregljs/otm_style.js` — её же
использует `index.html`. В Leaflet карта вставляется через `maplibre-gl-leaflet`
(только меркатор). `docker-compose.yml` монтирует `../../vector/maplibregljs` в
`/app/vector/maplibregljs`; без этого монтирования ассеты стиля не найдутся.

## Что перенесено из opentopomap-hike

### Палитра

Генштабовская бумага `#F3E6C4` вместо белого фона, коричневые горизонтали
`#A86038`, синяя вода `#2A6A9A`, красно-жёлтые дороги (`#C43C32`, `#E8B84A`),
розовато-бежевая застройка `#E8B09A`. Валидатор ругается на любой цвет в стиле,
которого нет в палитре TYP, — это и есть механизм соблюдения палитры.

### Ландшафт штриховками, а не заливками

Лес, луга, пашня, сад/виноградник, кустарник, песок, карьер, болото, кладбище
рисуются `fill-pattern` из спрайта. Лес дополнительно разделён по `leaf_type`
на хвойный и лиственный.

Осыпи (`scree`) и скалы (`bare-rock`) появляются с z12 и приглушены по
прозрачности (0.75 и 0.55): в полном контрасте они забивают горизонтали, которые
в горах важнее.

### Ледники и морены

Ледник белый с синим контуром вместо голубой заливки, начинается с z8. Покровная
морена на льду (`natural=glacier` + `glacier:part=moraine` + `surface=scree`,
также `geological=moraine`) рисуется осыпью — коричневыми точками поверх белого
льда, как на генштабе. Площадные морены вне ледника — той же точечной штриховкой,
линейные — хребтовкой с односторонними штрихами (два смещённых слоя с разными
`line-dasharray`). Трещины рисуются полигоном плюс поперечной штриховкой,
ширина штриха зависит от атрибута `width`.

Трещины — главное препятствие на леднике, поэтому им в Lua выставлен `MinZoom(9)`
вместо общего 12 для слоёв `natural_*`, и сами слои в конфиге начинаются с z9.
Контура у площадных трещин нет: на местности у трещинного поля нет границы, и
замкнутая линия читалась бы как обрыв или край ледника. Заметность даёт сама
штриховка — она идёт с z11 и к z17 утолщается до 3.2 px.

### Тропы на ранних зумах

Garmin показывает тропы с resolution 19–20, поэтому в Lua минзумы понижены:
дороги-`track` и большинство `path`/`footway` — с z12, размеченные (graded)
тропы — уже с z11. Тропы пунктирные чёрные, треки — коричневый пунктир,
лестницы — частая мелкая насечка.

### Чистка POI

Портирован `garmin/style/opentopomap-hike/inc/hiking_poi_filter`: городские
`amenity`/`leisure`/`tourism`/`shop` вычищаются в Lua (до тайлов, а не в стиле),
`sport` убирается всегда. Взамен добавлены походные точки: перевалы, родники,
водопады, колодцы, питьевая вода, геодезические знаки, укрытия, броды.

### Подписи вершин и перевалов

У вершин подпись «имя + высота», у перевалов — «имя + `rtsa_scale` + высота»
(категория трудности из OSM). Для этого в Lua добавлены атрибуты `ele`,
`rtsa_scale`, `summit_cross`, а `node_keys` расширены на `natural`, `waterway`,
`mountain_pass`, `ford`, `geodesy`.

### Прочее

Пересыхающие водотоки вынесены в отдельный пунктирный слой; обрывы рисуются
линией с зубцами; горизонтали на леднике синие, а на крутых склонах прореживаются
(при серверных тайлах); подписи горизонталей — коричневые с полупрозрачным
бумажным гало.

## Известные ограничения

- Клиентские изолинии не поддерживают `on_glacier`/`steep` и не дают трещин —
  нужен серверный тайлсет (§2.3).
- Хребтовка морен и зубцы обрывов сделаны через `line-offset` + `line-dasharray`.
  Это визуально близко, но на резких изломах линии штрихи могут расходиться;
  в Garmin то же делается настоящими TYP-битмапами.
- Спрайт растровый (как в TYP). Если понадобится резкость на 3x-экранах,
  паттерны придётся перерисовать в SVG.
