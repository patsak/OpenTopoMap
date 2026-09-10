#!/usr/bin/env python3
"""What s2veg.py and lc2veg.py share: the class list, the ready-made global land
cover maps, and the way a class raster is turned into polygons.

It is not a tool. The two that are - one classifies a Sentinel-2 scene, the other
just cuts a global map to a bbox - have to agree on what "wood" means, on how a
speckled raster is cleaned up, and on what the output file looks like, so that
part lives here instead of in both of them.

The global maps are read as data, not as pictures. Both are published as class
rasters, and both are reachable without an account:

    lcfm        ESA LCFM LCM-10, 10 m, 2020. The COGs behind services.terrascope.be
                need a Terrascope login, but the same pixels come out of the public
                titiler in front of them - /bbox/... .tif returns the class codes
                themselves, not a rendered PNG, so nothing has to be read back out
                of a colour table.
    worldcover  ESA WorldCover v200, 10 m, 2021, straight out of its open S3 bucket.

Their legends are coarser than a topographic map wants: cropland counts as the
herbaceous class, mangroves as trees, and built-up, snow and unclassifiable are
dropped rather than guessed at. Moss and lichen is kept apart from bare ground -
above the treeline the two look alike from below but are not the same surface,
and the map styles them differently.
"""

from __future__ import annotations

import json
import math
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
from osgeo import gdal, ogr, osr

gdal.UseExceptions()

OGR_DRIVERS = {
    ".gpkg": "GPKG",
    ".geojson": "GeoJSON",
    ".json": "GeoJSON",
    ".shp": "ESRI Shapefile",
    ".sqlite": "SQLite",
}
RASTER_SUFFIXES = (".tif", ".tiff")

CLASSES = {"nodata": 0, "water": 1, "bare": 2, "grass": 3, "scrub": 4, "wood": 5,
           "wetland": 6, "moss": 7}
NAMES = {code: name for name, code in CLASSES.items()}
VEGETATION = ("grass", "scrub", "wood", "wetland", "moss")
COLORS = {
    0: (0, 0, 0, 0), 1: (70, 130, 180, 255), 2: (200, 190, 170, 255),
    3: (196, 222, 122, 255), 4: (150, 195, 110, 255), 5: (40, 120, 60, 255),
    6: (140, 195, 190, 255), 7: (250, 230, 160, 255),
}
# What a traced polygon is tagged with in OSM, printed at the end of a run.
# moss is the lichen and moss ground above the treeline, which vector/tilemaker
# already styles as natural=fell; natural=tundra is its arctic-lowland synonym,
# and which of the two fits is a decision for the mapper, not for the raster.
OSM_TAGS = {
    "wood": "natural=wood", "scrub": "natural=scrub", "grass": "natural=grassland",
    "wetland": "natural=wetland", "water": "natural=water",
    "bare": "natural=bare_rock / scree", "moss": "natural=fell / tundra",
}

# Both products tile the world in 3-degree squares named after their SW corner,
# 36000 x 36000 pixels each, so one pixel is 1/12000 of a degree.
TILE_DEGREES = 3
PIXELS_PER_DEGREE = 12000

COVER_SOURCES = {
    "lcfm": {
        "title": "ESA LCFM LCM-10 v100 (2020), 10 m",
        "stac": "https://stac.terrascope.be",
        "collection": "lcfm-lcm-10",
        "titiler": "https://titiler.terrascope.be",
        "asset": "MAP",
        # 60 mangroves are trees; 90 built-up, 110 snow and 254 unclassifiable
        # have no class here and are dropped.
        "classes": {10: "wood", 20: "scrub", 30: "grass", 40: "grass", 50: "wetland",
                    60: "wood", 70: "moss", 80: "bare", 100: "water"},
    },
    "worldcover": {
        "title": "ESA WorldCover v200 (2021), 10 m",
        "url": ("https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/"
                "ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"),
        # WorldCover numbers its legend differently: 60 is bare/sparse and 100 is
        # moss and lichen, the other way round from LCFM.
        "classes": {10: "wood", 20: "scrub", 30: "grass", 40: "grass", 60: "bare",
                    80: "water", 90: "wetland", 100: "moss"},
    },
}


TOOLS_DIR = Path(__file__).resolve().parent
PIP_NAMES = {"sklearn": "scikit-learn", "skimage": "scikit-image"}


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def require(*modules: str) -> None:
    """Fail at the start with the command that fixes it, not halfway through a run
    with an ImportError from inside some function."""
    import importlib.util

    missing = [name for name in modules if importlib.util.find_spec(name) is None]
    if not missing:
        return
    names = ", ".join(PIP_NAMES.get(name, name) for name in missing)
    venv = TOOLS_DIR / ".venv"
    call = f"{venv}/bin/python {TOOLS_DIR / Path(sys.argv[0]).name} " + " ".join(sys.argv[1:])
    if (venv / "bin" / "python").exists():
        raise SystemExit(f"missing {names} in {Path(sys.executable).parent}, but the venv next "
                         f"to the tool has them:\n    {call}")
    raise SystemExit(
        f"missing Python packages: {names}\n"
        f"    python3 -m venv --system-site-packages {venv}\n"
        f"    {venv}/bin/pip install -r {TOOLS_DIR / 'requirements.txt'}\n"
        f"    {call}")


# ---------------------------------------------------------------- global cover


def utm_epsg(lon: float, lat: float) -> int:
    """The UTM zone a point falls in - a metric CRS, so that a hectare is a hectare."""
    return (32600 if lat >= 0 else 32700) + int((lon + 180) / 6) % 60 + 1


def snap_bbox(bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    """Grow the bbox out to whole source pixels, so nothing is resampled to fetch it."""
    west, south, east, north = bbox
    step = 1 / PIXELS_PER_DEGREE
    return (math.floor(west / step) * step, math.floor(south / step) * step,
            math.ceil(east / step) * step, math.ceil(north / step) * step)


def spatial_reference(crs: str | None = None, wkt: str | None = None) -> osr.SpatialReference:
    """Always in lon/lat order, whatever the EPSG registry says the axes are."""
    srs = osr.SpatialReference(wkt=wkt) if wkt else osr.SpatialReference()
    if crs:
        srs.SetFromUserInput(crs)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def reproject_bbox(bbox, source: osr.SpatialReference,
                   target: osr.SpatialReference) -> tuple[float, float, float, float]:
    """The bounding box of the four transformed corners - a rotated rectangle does
    not stay a rectangle, so its envelope is what can be asked for."""
    transform = osr.CoordinateTransformation(source, target)
    corners = [transform.TransformPoint(x, y)[:2]
               for x in (bbox[0], bbox[2]) for y in (bbox[1], bbox[3])]
    xs, ys = [c[0] for c in corners], [c[1] for c in corners]
    return min(xs), min(ys), max(xs), max(ys)


def lonlat_bbox(projection: str, bounds) -> tuple[float, float, float, float]:
    """A bbox given in some projected CRS, as lon/lat - what both products are tiled in."""
    return reproject_bbox(bounds, spatial_reference(wkt=projection),
                          spatial_reference("EPSG:4326"))


def cover_tiles(bbox: tuple[float, float, float, float]) -> list[str]:
    """The 3-degree tiles a bbox falls into, named the way both products name them."""
    west, south, east, north = bbox
    names = []
    for lat in range(math.floor(south / TILE_DEGREES) * TILE_DEGREES,
                     math.floor(north / TILE_DEGREES) * TILE_DEGREES + 1, TILE_DEGREES):
        for lon in range(math.floor(west / TILE_DEGREES) * TILE_DEGREES,
                         math.floor(east / TILE_DEGREES) * TILE_DEGREES + 1, TILE_DEGREES):
            names.append(f"{'N' if lat >= 0 else 'S'}{abs(lat):02d}"
                         f"{'E' if lon >= 0 else 'W'}{abs(lon):03d}")
    return names


def fetch_json(url: str, timeout: int = 60) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def stac_items(source: dict, bbox) -> list[dict]:
    """Ask the STAC API which items cover the bbox."""
    query = urllib.parse.urlencode({
        "collections": source["collection"],
        "bbox": ",".join(f"{v:.6f}" for v in bbox),
        "limit": 100,
    })
    found = fetch_json(f"{source['stac']}/search?{query}")
    return found.get("features", [])


def fetch_chunk(url: str, out: Path, timeout: int) -> None:
    with urllib.request.urlopen(url, timeout=timeout) as response, out.open("wb") as sink:
        shutil.copyfileobj(response, sink)


def fetch_titiler(source: dict, bbox, work: Path, chunk: int, timeout: int) -> list[Path]:
    """One GeoTIFF of class codes per chunk, straight out of the tile server.

    /bbox/....tif with no colormap hands back the values themselves. The request
    is cut into chunks of at most --chunk pixels a side because the response is an
    uncompressed GeoTIFF: a whole degree at 10 m is 288 MB and a minute and a half.
    """
    step = 1 / PIXELS_PER_DEGREE
    pieces = []
    for item in stac_items(source, bbox):
        item_bbox = item["bbox"]
        west, south = max(bbox[0], item_bbox[0]), max(bbox[1], item_bbox[1])
        east, north = min(bbox[2], item_bbox[2]), min(bbox[3], item_bbox[3])
        if west >= east or south >= north:
            continue
        west, south, east, north = snap_bbox((west, south, east, north))
        columns = max(int(round((east - west) / step)), 1)
        rows = max(int(round((north - south) / step)), 1)
        base = f"{source['titiler']}/collections/{source['collection']}/items/{item['id']}"
        for x0 in range(0, columns, chunk):
            for y0 in range(0, rows, chunk):
                width, height = min(chunk, columns - x0), min(chunk, rows - y0)
                piece_bbox = (west + x0 * step, north - (y0 + height) * step,
                              west + (x0 + width) * step, north - y0 * step)
                out = work / f"cover_{item['id']}_{x0}_{y0}.tif"
                url = (f"{base}/bbox/{','.join(f'{v:.8f}' for v in piece_bbox)}"
                       f"/{width}x{height}.tif?assets={source['asset']}")
                log(f"  {item['id']} {width}x{height} at {piece_bbox[0]:.3f},{piece_bbox[3]:.3f}")
                try:
                    fetch_chunk(url, out, timeout)
                except (urllib.error.URLError, TimeoutError) as failure:
                    raise SystemExit(f"{source['titiler']} did not answer: {failure}") from None
                pieces.append(out)
    return pieces


def fetch_bucket(source: dict, bbox, work: Path) -> list[Path]:
    """WorldCover needs no tile server: its COGs are public, GDAL reads them in place."""
    gdal.SetConfigOption("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    gdal.SetConfigOption("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif")
    urls = []
    for tile in cover_tiles(bbox):
        url = "/vsicurl/" + source["url"].format(tile=tile)
        try:
            gdal.Open(url)
        except RuntimeError:
            continue          # ocean tiles are simply not published
        urls.append(url)
    return urls


def fetch_cover(name: str, bbox, work: Path, chunk: int = 4000, timeout: int = 300) -> Path:
    """The land cover map for a bbox as one GeoTIFF of class codes in EPSG:4326."""
    source = COVER_SOURCES[name]
    log(f"{name}: {source['title']}")
    pieces = (fetch_bucket(source, bbox, work) if "url" in source
              else fetch_titiler(source, bbox, work, chunk, timeout))
    if not pieces:
        raise SystemExit(f"{name} has nothing over {bbox}: outside its coverage, or "
                         "the service is down")
    mosaic = work / f"{name}.vrt"
    gdal.BuildVRT(str(mosaic), [str(p) for p in pieces], bandList=[1])
    return mosaic


def reclass(cover: np.ndarray, name: str) -> np.ndarray:
    """Source class codes to this map's, everything unmapped to nodata."""
    classes = COVER_SOURCES[name]["classes"]
    out = np.zeros(cover.shape, dtype=np.uint8)
    for code, target in classes.items():
        out[cover == code] = CLASSES[target]
    return out


# --------------------------------------------------------------------- cleanup


def majority_of(labels: np.ndarray, valid: np.ndarray, window: int) -> np.ndarray:
    """Salt and pepper goes away when every pixel takes the local plurality."""
    from scipy.ndimage import uniform_filter

    best = np.zeros(labels.shape, dtype=np.float32)
    winner = np.zeros(labels.shape, dtype=np.uint8)
    for code in sorted(NAMES):
        if code == 0:
            continue
        share = uniform_filter((labels == code).astype(np.float32), window, mode="nearest")
        take = share > best
        best[take], winner[take] = share[take], code
    return np.where(valid, winner, 0).astype(np.uint8)


def sieve(labels: np.ndarray, reference: gdal.Dataset, min_px: int) -> np.ndarray:
    """gdal.SieveFilter: blobs under min_px are swallowed by the biggest neighbour."""
    if min_px < 2:
        return labels
    mem = gdal.GetDriverByName("MEM").Create("", labels.shape[1], labels.shape[0], 1, gdal.GDT_Byte)
    mem.SetGeoTransform(reference.GetGeoTransform())
    mem.SetProjection(reference.GetProjection())
    band = mem.GetRasterBand(1)
    band.WriteArray(labels)
    band.SetNoDataValue(0)
    gdal.SieveFilter(band, band.GetMaskBand(), band, min_px, 8)
    return band.ReadAsArray()


# ---------------------------------------------------------------------- output


def write_raster(path: Path, labels: np.ndarray, reference: gdal.Dataset) -> None:
    out = gdal.GetDriverByName("GTiff").Create(
        str(path), labels.shape[1], labels.shape[0], 1, gdal.GDT_Byte,
        ["TILED=YES", "COMPRESS=DEFLATE"])
    out.SetGeoTransform(reference.GetGeoTransform())
    out.SetProjection(reference.GetProjection())
    band = out.GetRasterBand(1)
    # The palette has to be in place before any pixel is written: GTiff fixes
    # PhotometricInterpretation on the first write.
    table = gdal.ColorTable()
    for code, color in COLORS.items():
        table.SetColorEntry(code, color)
    band.SetRasterColorTable(table)
    band.SetNoDataValue(0)
    band.WriteArray(labels)
    band.SetRasterCategoryNames([NAMES.get(code, "") for code in range(max(NAMES) + 1)])
    del out
    log(f"class raster: {path}")


def polygonize(labels: np.ndarray, reference: gdal.Dataset, wanted: list[str], args) -> Path:
    """One polygon per contiguous run of a class, in the scene's UTM."""
    mem = gdal.GetDriverByName("MEM").Create("", labels.shape[1], labels.shape[0], 1, gdal.GDT_Byte)
    mem.SetGeoTransform(reference.GetGeoTransform())
    mem.SetProjection(reference.GetProjection())
    keep = np.isin(labels, [CLASSES[name] for name in wanted])
    band = mem.GetRasterBand(1)
    band.WriteArray(np.where(keep, labels, 0))
    band.SetNoDataValue(0)

    raw = args.work / "polygons.gpkg"
    raw.unlink(missing_ok=True)
    sink = ogr.GetDriverByName("GPKG").CreateDataSource(str(raw))
    srs = osr.SpatialReference(wkt=reference.GetProjection())
    layer = sink.CreateLayer("raw", srs=srs, geom_type=ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("code", ogr.OFTInteger))
    gdal.Polygonize(band, band.GetMaskBand(), layer, 0, ["8CONNECTED=8"])
    del sink
    return raw


def write_vector(raw: Path, output: Path, layer_name: str, args) -> None:
    """Filter by area, simplify, reproject and write the file the user asked for."""
    driver_name = OGR_DRIVERS[output.suffix.lower()]
    source = ogr.Open(str(raw))
    layer = source.GetLayer(0)
    srs = layer.GetSpatialRef()
    target_srs = osr.SpatialReference()
    target_srs.SetFromUserInput(args.out_crs)
    target_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    transform = osr.CoordinateTransformation(srs, target_srs) if not srs.IsSame(target_srs) else None

    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    sink = ogr.GetDriverByName(driver_name).CreateDataSource(str(output))
    out_layer = sink.CreateLayer(layer_name, srs=target_srs, geom_type=ogr.wkbMultiPolygon)
    out_layer.CreateField(ogr.FieldDefn("class", ogr.OFTString))
    out_layer.CreateField(ogr.FieldDefn("code", ogr.OFTInteger))
    area_field = ogr.FieldDefn("area_ha", ogr.OFTReal)
    area_field.SetPrecision(3)
    out_layer.CreateField(area_field)

    min_area = args.min_area * 10000.0
    kept, dropped, invalid, area_by_class = 0, 0, 0, {}
    out_layer.StartTransaction()
    for feature in layer:
        geometry = feature.GetGeometryRef()
        area = geometry.GetArea()
        if area < min_area:
            dropped += 1
            continue
        if args.simplify > 0:
            geometry = geometry.SimplifyPreserveTopology(args.simplify)
        # Two pixels meeting at a corner give 8-connected polygonize a ring that
        # touches itself, which is not a valid polygon anywhere downstream.
        gdal.PushErrorHandler("CPLQuietErrorHandler")   # IsValid says why, at length
        valid_geometry = geometry.IsValid()
        gdal.PopErrorHandler()
        geometry = geometry if valid_geometry else geometry.MakeValid()
        if geometry is None:
            invalid += 1
            continue
        geometry = ogr.ForceToMultiPolygon(geometry)
        if transform is not None:
            geometry = geometry.Clone()
            geometry.Transform(transform)
        name = NAMES[feature.GetField("code")]
        out_feature = ogr.Feature(out_layer.GetLayerDefn())
        out_feature.SetGeometry(geometry)
        out_feature.SetField("class", name)
        out_feature.SetField("code", feature.GetField("code"))
        out_feature.SetField("area_ha", area / 10000.0)
        out_layer.CreateFeature(out_feature)
        area_by_class[name] = area_by_class.get(name, 0.0) + area
        kept += 1
    out_layer.CommitTransaction()
    del sink

    log(f"polygons: {kept} kept, {dropped} under {args.min_area:g} ha dropped"
        + (f", {invalid} unrepairable" if invalid else ""))
    for name, area in sorted(area_by_class.items(), key=lambda p: -p[1]):
        log(f"  {name:<8} {area / 1e6:>9.1f} km2   {OSM_TAGS.get(name, '')}")
    log(f"wrote {output}")


# ------------------------------------------------------------------------- CLI


def validate_output(output: Path) -> None:
    """Reject an extension nothing can write - before a run, not after it."""
    suffix = output.suffix.lower()
    if suffix not in OGR_DRIVERS and suffix not in RASTER_SUFFIXES:
        raise SystemExit(f"unsupported output extension {output.suffix!r}; use "
                         + ", ".join(sorted(OGR_DRIVERS)) + " or .tif")


def parse_classes(value: str) -> list[str]:
    wanted = (list(VEGETATION) + ["water", "bare"] if value == "all"
              else [name.strip() for name in value.split(",") if name.strip()])
    unknown = [name for name in wanted if name not in CLASSES or name == "nodata"]
    if unknown:
        raise SystemExit(f"--classes: unknown {', '.join(unknown)}")
    return wanted


def normalize_bbox(parser, bbox) -> tuple[float, float, float, float]:
    west, south, east, north = bbox
    if east < west:
        west, east = east, west
    if north < south:
        south, north = north, south
    if west == east or south == north:
        parser.error("--bbox must have non-zero width and height")
    return west, south, east, north


def work_directory(args) -> Path:
    work = args.work_dir or args.output.resolve().parent / f".{args.output.stem}-work"
    work.mkdir(parents=True, exist_ok=True)
    return work


def cleanup(work: Path, keep: bool) -> None:
    if keep:
        log(f"work dir kept: {work}")
    else:
        shutil.rmtree(work, ignore_errors=True)
