#!/usr/bin/env python3
"""Wood / dwarf pine / grass for a bbox, classified from Sentinel scenes of one season.

lc2veg.py cuts somebody else's finished map to a bbox. This tool does the
classification itself, over imagery fetched for the bbox that was asked for, so
the result is as current as the last cloud-free pass and - given hand-drawn
training polygons - can hold classes the global maps simply do not have. Dwarf
pine (stlanik, Pinus pumila) is exactly such a class: LCFM and WorldCover both
fold it into tree cover or into moss.

    ./s2veg.py --bbox 114.0 56.20 114.2 56.35 --year 2025 -o veg.gpkg
    ./s2veg.py --bbox 114.0 56.20 114.2 56.35 --labels stlanik.gpkg -o veg.gpkg
    ./s2veg.py --bbox 114.0 56.20 114.2 56.35 --method kmeans --name 4=scrub -o veg.gpkg

Everything happens on the bbox: the scene search, the composites, the training
sample and the classification are all cut to it before a pixel is read, and
nothing outside it is ever fetched or predicted.

What each feature is for - the three classes do not separate on colour alone:

    summer reflectance  B2 B3 B4 B8 B11 B12 of a cloud-free high-summer composite
                        and NDVI = (B8 - B4) / (B8 + B4). Green, and how green.
    spring NDVI         the same index over the snowmelt window. Larch and pine
                        already stand above the snow and their NDVI is up by then;
                        dwarf pine is still lying underneath it and reads near
                        zero. This is the feature that tells stlanik from forest,
                        so the spring composite deliberately keeps snow pixels -
                        only clouds are masked out of it.
    SAR backscatter     Sentinel-1 gamma0 VV and VH in decibels and the
                        dimensionless VH/VV ratio, from the summer
                        mosaic. A tree crown scatters through its volume and
                        depolarizes; a mat of dwarf pine or a meadow does not, so
                        VH/VV drops. Crown density and roughness, in other words,
                        which optical bands cannot see at all.

Pipeline:

    1. search     CDSE STAC (pystac_client) for the S2 L2A scenes over the bbox in
                  the summer and spring windows, and the S1 monthly mosaic tiles
    2. composite  per MGRS tile the least cloudy scenes, SCL-masked, per-band
                  median; mosaicked onto the UTM grid of the bbox centre
    3. features   the 11 numbers above, stacked per pixel
    4. split      --method knn trains a kNN on samples drawn from the labels:
                  either a global map (lcfm / worldcover, eroded so only blob
                  interiors train) or a hand-drawn GPKG with a class or code
                  field. --method kmeans instead splits the bbox on the features
                  alone and only asks the labels what to call each cluster
    5. classify   every pixel with a complete feature vector
    6. clean      majority filter (--smooth), sieve under --min-area, polygonize
                  and write GPKG/GeoJSON/Shapefile, exactly as lc2veg.py does

Access: the pixels live in the CDSE object store, which needs S3 keys of your own
(https://eodata-s3keysmanager.dataspace.copernicus.eu, free with a Copernicus
account). Export them before the run:

    export CDSE_S3_ACCESS_KEY=... CDSE_S3_SECRET_KEY=...

The STAC search itself is open, so --dry-run lists what would be used without any
credentials at all.

Two ways to get a stlanik class, since no global map has one:

    --labels   draw twenty patches per class you are sure of in JOSM or QGIS and
               train on those. Exact, and exactly as much work as it sounds.
    --method kmeans
               let k-means split the bbox on the features alone - nothing has to
               have been mapped before for the split to happen, so dwarf pine
               comes out as its own cluster - and name the clusters from the
               global map, which is good at naming and bad only at splitting.
               The cluster the map calls wood while its NDVI_spring sits near
               zero is the dwarf pine, and --name 4=scrub makes it one.

What the result is worth: eleven numbers and a distance are a drafting aid, not a
land cover product. Classify the valley, then correct it by hand.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import warnings
from pathlib import Path

import numpy as np
from osgeo import gdal, ogr

import veglib
from veglib import (CLASSES, COVER_SOURCES, NAMES, OGR_DRIVERS, RASTER_SUFFIXES,
                    TOOLS_DIR, log)

gdal.UseExceptions()

CDSE_STAC = "https://stac.dataspace.copernicus.eu/v1"
CDSE_S3_ENDPOINT = "eodata.dataspace.copernicus.eu"
S2_COLLECTION = "sentinel-2-l2a"
S1_COLLECTION = "sentinel-1-global-mosaics"

# The reflectance bands asked for, and the resolutions L2A publishes each at.
# A 10 m band is a 100 MB jp2 and a 20 m one a quarter of that, so a run at
# --res 20 has no business reading the 10 m copy and throwing three quarters of
# it away. B08 is the exception: it exists at 10 m only.
S2_BANDS = {"B02": (10, 20, 60), "B03": (10, 20, 60), "B04": (10, 20, 60),
            "B08": (10,), "B8A": (20, 60), "B11": (20, 60), "B12": (20, 60)}

# The near infrared NDVI is computed from, in order of preference. B08 is the
# broad 842 nm band and the only one L2A publishes at 10 m; B8A is the narrow
# 865 nm one, published at 20 m, which is a quarter of the bytes and for
# telling a canopy from a meadow just as good. A run at --res 20 that asks for
# B08 is paying 10 m prices for a 20 m answer.
NIR_BANDS = ("B08", "B8A")

# Scene classification: 4 vegetation, 5 bare, 6 water, 7 unclassified are land to
# look at; 8/9/10 cloud and 3 cloud shadow never are. 11 snow is dropped in
# summer and kept in spring, where lying snow is half the signal.
SCL_LAND = {4, 5, 6, 7}
SCL_SNOW = 11
# 2 is "dark area". In the mountains on a clear day it is the slope facing
# away from the sun, not a cloud artefact, and s2rock keeps it: over a
# Caucasus bbox it held 86-95 % of the bare ground lit at cos(i) under 0.25.
SCL_DARK = 2

# Since processing baseline 04.00 the jp2 carry an offset of -1000 DN, so the
# same reflectance comes out 0.1 lower unless it is added back.
BOA_SCALE = 10000.0

# Every way S3 says "not with these keys". The message GDAL raises carries the
# code but not the HTTP status, so matching on "403" alone misses most of them -
# SignatureDoesNotMatch, which a secret changed mid-run produces, went through
# that way once and turned into seventeen "could not be read" scenes.
S3_AUTH_ERRORS = ("InvalidAccessKeyId", "SignatureDoesNotMatch", "AccessDenied",
                  "InvalidToken", "ExpiredToken", "TokenRefreshRequired", " 403")
BOA_OFFSET_BASELINE = 400


class Grid:
    """The raster everything is resampled onto: metric, cut to the bbox."""

    def __init__(self, crs: str, bounds: tuple[float, float, float, float], res: float):
        self.crs, self.bounds, self.res = crs, bounds, res
        self.width = max(int(round((bounds[2] - bounds[0]) / res)), 1)
        self.height = max(int(round((bounds[3] - bounds[1]) / res)), 1)
        self.wkt = veglib.spatial_reference(crs).ExportToWkt()

    @property
    def geotransform(self):
        return (self.bounds[0], self.res, 0.0, self.bounds[3], 0.0, -self.res)

    @property
    def lonlat_bbox(self) -> tuple[float, float, float, float]:
        return veglib.lonlat_bbox(self.wkt, self.bounds)

    def reference(self) -> gdal.Dataset:
        """An empty dataset carrying the georeferencing, for veglib's helpers."""
        mem = gdal.GetDriverByName("MEM").Create("", self.width, self.height, 1, gdal.GDT_Byte)
        mem.SetGeoTransform(self.geotransform)
        mem.SetProjection(self.wkt)
        return mem


# --------------------------------------------------------------------- reading


def open_s3() -> None:
    """Point GDAL at the CDSE object store, and fail now rather than on the first
    band if there are no keys to point it with."""
    access = os.environ.get("CDSE_S3_ACCESS_KEY") or os.environ.get("AWS_ACCESS_KEY_ID")
    secret = os.environ.get("CDSE_S3_SECRET_KEY") or os.environ.get("AWS_SECRET_ACCESS_KEY")
    if not access or not secret:
        raise SystemExit(
            "no CDSE object store credentials. Create a pair at\n"
            "    https://eodata-s3keysmanager.dataspace.copernicus.eu\n"
            "and export them:\n"
            "    export CDSE_S3_ACCESS_KEY=... CDSE_S3_SECRET_KEY=...\n"
            "(--dry-run needs none of this: the STAC search is open.)")
    for key, value in {
        "AWS_ACCESS_KEY_ID": access,
        "AWS_SECRET_ACCESS_KEY": secret,
        "AWS_S3_ENDPOINT": CDSE_S3_ENDPOINT,
        "AWS_VIRTUAL_HOSTING": "FALSE",
        "AWS_HTTPS": "YES",
        "AWS_REGION": "default",
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".jp2,.tif,.tiff",
        "GDAL_HTTP_MAX_RETRY": "3",
        "GDAL_HTTP_RETRY_DELAY": "2",
        # An L2A band is one 100+ MB jp2 per tile and the bbox is a small window
        # in the middle of it. Left at its defaults GDAL asks for that window in
        # 16 kB ranges through a 16 MB cache, which for a codestream that has to
        # be walked is thousands of round trips and minutes per band. Big chunks
        # and a cache that can hold them turn the same read into seconds.
        "VSI_CACHE": "TRUE",
        "VSI_CACHE_SIZE": "134217728",            # 128 MB per file
        "CPL_VSIL_CURL_CHUNK_SIZE": "10485760",   # 10 MB per range request
        "CPL_VSIL_CURL_CACHE_SIZE": "536870912",  # 512 MB shared
        "GDAL_CACHEMAX": "1024",
        "GDAL_NUM_THREADS": "ALL_CPUS",
        "GDAL_HTTP_VERSION": "2",
        "GDAL_HTTP_MULTIPLEX": "YES",
    }.items():
        gdal.SetConfigOption(key, value)


def vsis3(href: str) -> str:
    """s3://eodata/... as GDAL says it. The https alternates in the same items
    want an OIDC token per request, which is a worse deal for random reads."""
    return "/vsis3/" + href[len("s3://"):] if href.startswith("s3://") else href


def resampling(source_res: float, grid: Grid) -> str:
    """Averaging on the way down, bilinear on the way up - a 20 m band asked for
    at 10 m is interpolated, a 10 m band asked for at 30 m is averaged."""
    return "average" if grid.res > source_res * 1.5 else "bilinear"


def warp(sources, grid: Grid, resample: str, nodata=None) -> np.ndarray | None:
    """One or more rasters cut and reprojected onto the grid, as an array.

    A list of sources is mosaicked in one pass, which is what several MGRS tiles
    over one bbox need. None when nothing of the source lands on the grid.
    """
    try:
        warped = gdal.Warp("", sources, format="MEM", dstSRS=grid.crs,
                           outputBounds=grid.bounds, xRes=grid.res, yRes=grid.res,
                           resampleAlg=resample, srcNodata=nodata, dstNodata=nodata,
                           multithread=True, warpMemoryLimit=256 * 1024 * 1024)
    except RuntimeError as failure:
        # A rejected key answers 403 on every object, which looks exactly like a
        # scene with no usable pixels - so without this the run grinds through
        # the whole catalogue and then blames the weather. Credentials are not a
        # per-scene problem and there is no point continuing.
        text = str(failure)
        refused = next((code for code in S3_AUTH_ERRORS if code in text), None)
        if refused:
            raise SystemExit(
                f"the CDSE object store rejected the credentials (403 {refused}).\n"
                "    the keys in the environment are not valid any more - revoked, "
                "expired, or never exported in this shell.\n"
                "    make a new pair at https://eodata-s3keysmanager.dataspace.copernicus.eu\n"
                "    and export CDSE_S3_ACCESS_KEY / CDSE_S3_SECRET_KEY before running") from None
        log(f"  ! {failure}")
        return None
    return warped.ReadAsArray() if warped is not None else None


def boa_offset(item_id: str) -> float:
    """The additive offset of this scene's processing baseline, in DN."""
    for part in item_id.split("_"):
        if len(part) == 5 and part[0] == "N" and part[1:].isdigit():
            return -1000.0 if int(part[1:]) >= BOA_OFFSET_BASELINE else 0.0
    return 0.0


def asset_resolution(band: str, grid: Grid) -> int:
    """The coarsest published copy of a band that is still finer than the grid."""
    published = S2_BANDS[band]
    usable = [res for res in published if res <= grid.res]
    return max(usable) if usable else min(published)


def read_reflectance(item, band: str, grid: Grid) -> np.ndarray | None:
    """One band of one scene as surface reflectance, NaN outside the scene."""
    source_res = asset_resolution(band, grid)
    asset = item.assets.get(f"{band}_{source_res}m")
    if asset is None:
        return None
    raw = warp(vsis3(asset.href), grid, resampling(source_res, grid), nodata=0)
    if raw is None:
        return None
    values = raw.astype(np.float32)
    values[raw == 0] = np.nan        # 0 is the scene's own nodata, not a dark pixel
    return (values + boa_offset(item.id)) / BOA_SCALE


def scene_mask(item, grid: Grid, keep_snow: bool,
               keep_dark: bool = False) -> np.ndarray | None:
    """Which pixels of this scene are worth compositing, from its own SCL band."""
    asset = item.assets.get("SCL_20m")
    if asset is None:
        return None
    scl = warp(vsis3(asset.href), grid, "near", nodata=0)
    if scl is None:
        return None
    keep = (SCL_LAND | ({SCL_SNOW} if keep_snow else set())
            | ({SCL_DARK} if keep_dark else set()))
    return np.isin(scl, sorted(keep))


# ------------------------------------------------------------------ STAC search


def stac_client():
    from pystac_client import Client

    return Client.open(CDSE_STAC)


def collect(search, attempts: int = 5) -> list:
    """Every page of a search, with the catalogue's rate limiter waited out.

    CDSE answers 429 through its WAF rather than blocking, and a paged search of
    a busy tile hits it often enough that failing the whole run over it would be
    silly. --limit is set high for the same reason: fewer pages, fewer requests.
    """
    from pystac_client.exceptions import APIError

    delay = 5.0
    for attempt in range(attempts):
        try:
            return list(search.items())
        except APIError as failure:
            if "429" not in str(failure) and "Rate limit" not in str(failure):
                raise SystemExit(f"the CDSE catalogue refused the search: {failure}") from None
            log(f"  the catalogue is rate limiting; waiting {delay:.0f}s")
            time.sleep(delay)
            delay *= 2
    raise SystemExit("the CDSE catalogue kept rate limiting the search - try again later")


def window(year: int, span: str) -> str:
    """'06-15:09-15' of a year, as STAC wants the interval written."""
    start, end = span.split(":")
    return f"{year}-{start}/{year}-{end}"


# The scene-classification shares the catalogue publishes per item. Anything in
# UNUSABLE is thrown away by the SCL mask anyway; what is left over from 100 is
# snow, which has no key of its own and which the spring window wants to keep.
USABLE_LAND = ("vegetation", "not_vegetated", "water", "unclassified")
UNUSABLE = ("nodata", "saturated_defective", "dark_area", "cloud_shadow",
            "thin_cirrus", "medium_proba_clouds", "high_proba_clouds")


def usable_share(item, keep_snow: bool) -> float:
    """How much of this scene the composite could actually use, per the catalogue.

    Better than eo:cloud_cover for picking scenes, and not only by a little: a
    granule that is 100 % saturated_defective is published as 0.0 % cloud, which
    sorts it first and wastes a --scenes slot on a scene with nothing in it.
    """
    statistics = item.properties.get("statistics")
    if not statistics:
        return 100.0 - item.properties.get("eo:cloud_cover", 100.0)
    if keep_snow:
        return 100.0 - sum(statistics.get(key, 0.0) for key in UNUSABLE)
    return sum(statistics.get(key, 0.0) for key in USABLE_LAND)


def footprint_share(item, bbox) -> float:
    """How much of the bbox this scene's footprint covers, from the catalogue."""
    west, south, east, north = bbox
    box = ogr.CreateGeometryFromWkt(f"POLYGON(({west} {south},{east} {south},{east} {north},"
                                    f"{west} {north},{west} {south}))")
    try:
        shape = ogr.CreateGeometryFromJson(json.dumps(item.geometry))
        return shape.Intersection(box).GetArea() / box.GetArea()
    except (RuntimeError, AttributeError, TypeError):
        return 1.0      # no usable geometry: let the mask decide


def search_s2(client, bbox, year: int, span: str, max_cloud: float,
              keep_snow: bool, limit: int = 60) -> list:
    """Every scene that could help over the bbox, best first - a candidate list.

    Which of them are used is decided later by composite(), from the scenes' own
    cloud masks, not here. This used to pick a few scenes per MGRS tile by the
    catalogue's usable share, and that went badly wrong at the edge of an orbit:
    a scene that caught only a strip of its tile carries 92-97 % nodata, the
    class shares it does have are reported over the valid strip, and so a sliver
    ranked as a 100 % usable scene. Over a Baikal-shore bbox the picks covered
    17.6 % of it between them. The ranking here therefore weights by how much of
    the bbox the footprint actually covers, and only shortlists.
    """
    found = client.search(collections=[S2_COLLECTION], bbox=list(bbox),
                          datetime=window(year, span),
                          query={"eo:cloud_cover": {"lt": max_cloud}},
                          limit=100, max_items=400)
    scored = []
    for item in collect(found):
        cover = footprint_share(item, bbox)
        if cover < 0.005:
            continue
        clear = 1.0 - item.properties.get("eo:cloud_cover", 50.0) / 100.0
        scored.append((cover * clear, item))
    scored.sort(key=lambda pair: -pair[0])
    return [item for _, item in scored[:limit]]


def search_s1(client, bbox, year: int, span: str) -> list:
    """The monthly Sentinel-1 mosaic tiles over the bbox.

    The mosaics rather than the GRD scenes on purpose: a GRD is in ground range
    with GCPs, and turning it into something comparable between slopes means
    calibration and terrain flattening against a DEM - a SNAP-sized job, and in
    the mountains where dwarf pine grows layover would dominate whatever came out
    of it. The L3 mosaics are already orthorectified gamma0 on the MGRS grid.
    """
    found = client.search(collections=[S1_COLLECTION], bbox=list(bbox),
                          datetime=window(year, span), limit=100, max_items=200)
    return [item for item in collect(found)
            if {"VV", "VH"} <= set(item.assets) and item.properties.get("gsd", 20) <= 40]


# ------------------------------------------------------------------- composites


def read_band(masks, band: str, grid: Grid, args) -> list[np.ndarray]:
    """One band of every scene, several scenes at a time.

    The reads are network-bound - one 10 m jp2 out of the CDSE store runs at a
    megabyte or two a second - so they are waited for in parallel rather than one
    after another. GDAL drops the GIL around the transfer and each Warp has its
    own datasets, so nothing is shared but the curl cache, which is built for it.
    """
    from concurrent.futures import ThreadPoolExecutor

    def one(pair):
        item, mask = pair
        values = read_reflectance(item, band, grid)
        if values is None:
            return None
        values[~mask] = np.nan
        return values

    done = 0
    layers = []
    with ThreadPoolExecutor(max_workers=max(args.workers, 1)) as pool:
        for values in pool.map(one, masks):
            done += 1
            log(f"  {band} {done}/{len(masks)}")
            if values is not None:
                layers.append(values)
    return layers


def composite(items, grid: Grid, bands, keep_snow: bool, label: str, args,
              required: bool = True) -> dict[str, np.ndarray] | None:
    """Per-band median of the masked scenes - the usual defence against a cloud
    edge or a shadow that SCL missed, and it fills the holes of one scene with
    another pass over the same ground."""
    # Scenes are taken greedily, in the order search_s2 ranked them, for as long
    # as they add usable pixels where the bbox still has fewer than --scenes clear
    # looks. A scene's mask is a 20 m jp2 and costs a second or two, which is
    # cheap next to the reflectance bands it decides whether to download.
    target = max(args.scenes, 1)
    looks = np.zeros((grid.height, grid.width), dtype=np.uint8)
    masks, tried = [], 0
    for item in items:
        if (looks >= target).mean() >= 0.995:
            break
        tried += 1
        started = time.monotonic()
        mask = scene_mask(item, grid, keep_snow, getattr(args, "keep_dark", False))
        if mask is None or not mask.any():
            log(f"  {item.id}: nothing usable over the bbox")
            continue
        gain = (mask & (looks < target)).mean()
        if gain < 0.005:
            continue        # clear, but only where there are enough looks already
        looks += mask
        log(f"  {item.id}  {100 * mask.mean():4.1f}% of the bbox clear, "
            f"+{100 * gain:4.1f}% where looks were short, "
            f"{time.monotonic() - started:.0f}s")
        masks.append((item, mask))
    if masks:
        log(f"  {len(masks)} of {tried} scenes looked at: "
            f"{100 * (looks >= 1).mean():.1f}% of the bbox seen clear at least once, "
            f"{100 * (looks >= target).mean():.1f}% at least {target} times")
    if not masks:
        if not required:
            return None
        raise SystemExit(f"no usable {label} scene over the bbox - widen the window "
                         "or raise --max-cloud")

    out = {}
    for band in bands:
        started = time.monotonic()
        layers = read_band(masks, band, grid, args)
        if not layers:
            if not required:
                return None
            raise SystemExit(f"{label}: band {band} could not be read from any scene")
        with warnings.catch_warnings():       # a pixel cloudy in every scene is all-NaN
            warnings.simplefilter("ignore", RuntimeWarning)
            out[band] = np.nanmedian(np.stack(layers), axis=0).astype(np.float32)
        log(f"  {band} at {asset_resolution(band, grid)} m: {len(layers)} scenes "
            f"in {time.monotonic() - started:.0f}s")
        del layers
    filled = np.isfinite(out[bands[0]]).mean()
    log(f"{label} composite: {len(masks)} scenes, {100 * filled:.1f}% of the bbox covered")
    return out


def nir_band(bands) -> str:
    for band in NIR_BANDS:
        if band in bands:
            return band
    raise SystemExit(f"--bands needs one of {' or '.join(NIR_BANDS)} for NDVI")


def ndvi(bands: dict[str, np.ndarray]) -> np.ndarray:
    nir, red = bands[nir_band(bands)], bands["B04"]
    with np.errstate(invalid="ignore", divide="ignore"):
        return ((nir - red) / (nir + red)).astype(np.float32)


def sar_composite(items, grid: Grid) -> dict[str, np.ndarray] | None:
    """VV and VH in dB, averaged over the monthly mosaics, and the VH/VV ratio.

    The mosaics hold linear gamma0, so the mean is taken there and only then
    turned into decibels; averaging decibels would quietly weight the quiet
    pixels. The ratio is formed in linear power too and stays there - the
    dimensionless VH/VV, not its logarithm. In decibels it would be exactly
    VH_db minus VV_db, an exact linear combination of the two features beside
    it, so distance would count the same physical property twice; the linear
    ratio does not.
    """
    by_month: dict[str, list] = {}
    for item in items:
        by_month.setdefault(item.properties["datetime"][:7], []).append(item)
    out, power = {}, {}
    for polarization in ("VV", "VH"):
        months = []
        for month, group in sorted(by_month.items()):
            hrefs = [vsis3(item.assets[polarization].href) for item in group]
            tiles = warp(hrefs, grid, resampling(20, grid), nodata=-32768)
            if tiles is None:
                continue
            values = tiles.astype(np.float32)
            values[~np.isfinite(values) | (values <= 0)] = np.nan
            months.append(values)
            log(f"  {polarization} {month}: {len(group)} tiles, "
                f"{100 * np.isfinite(values).mean():4.1f}% of the bbox")
        if not months:
            return None
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            power[polarization] = np.nanmean(np.stack(months), axis=0)
        out[polarization] = (10.0 * np.log10(power[polarization])).astype(np.float32)
    with np.errstate(invalid="ignore", divide="ignore"):
        out["VH_VV"] = (power["VH"] / power["VV"]).astype(np.float32)
    return out


def read_feature_stack(path: Path, grid: Grid) -> tuple[list[str], np.ndarray]:
    """A stack written by an earlier run, instead of fetching everything again.

    Naming clusters is a loop - look at the table, --name one of them, look
    again - and paying for the whole download on every turn of it would be
    absurd. It also means the second and later runs need no credentials at all.
    """
    stack = gdal.Open(str(path))
    if (stack.RasterXSize, stack.RasterYSize) != (grid.width, grid.height) or \
            not np.allclose(stack.GetGeoTransform(), grid.geotransform):
        raise SystemExit(f"{path} is {stack.RasterXSize}x{stack.RasterYSize} on a different "
                         f"grid than this run's {grid.width}x{grid.height} - pass the same "
                         "--bbox, --res and --crs it was written with")
    names = [stack.GetRasterBand(i + 1).GetDescription() or f"band{i + 1}"
             for i in range(stack.RasterCount)]
    log(f"features from {path}: {', '.join(names)}")
    return names, stack.ReadAsArray().astype(np.float32)


def feature_stack(client, grid: Grid, args) -> tuple[list[str], np.ndarray]:
    """Every feature of every pixel: (features, height, width), NaN where unknown."""
    bbox = grid.lonlat_bbox
    names, layers = [], []

    if "B08" in args.bands and grid.res >= 20:
        log("! B08 exists at 10 m only, so at this resolution three quarters of every "
            "byte of it is thrown away on arrival - --bands "
            + ",".join("B8A" if b == "B08" else b for b in args.bands) + " is the same "
            "answer for a quarter of the download")
    log(f"summer {window(args.year, args.summer)}, under {args.max_cloud:g}% cloud:")
    summer_items = search_s2(client, bbox, args.year, args.summer, args.max_cloud,
                             keep_snow=False)
    if not summer_items:
        raise SystemExit("the catalogue has no summer scene over the bbox under "
                         f"{args.max_cloud:g}% cloud - widen --summer or raise --max-cloud")
    summer = composite(summer_items, grid, args.bands, keep_snow=False, label="summer",
                       args=args)
    for band in args.bands:
        names.append(band)
        layers.append(summer[band])
    names.append("NDVI")
    layers.append(ndvi(summer))

    if args.spring != "off":
        log(f"spring {window(args.year, args.spring)}, snow kept:")
        spring_items = search_s2(client, bbox, args.year, args.spring, args.max_cloud,
                                 keep_snow=True)
        # Only the red and near infrared: the spring scene is here for one index.
        spring = composite(spring_items, grid, ["B04", nir_band(args.bands)], keep_snow=True,
                           label="spring", args=args,
                           required=False) if spring_items else None
        if spring is None:
            log("! no usable spring scene over the bbox - dropping NDVI_spring, which is "
                "the feature that tells dwarf pine from forest")
        else:
            names.append("NDVI_spring")
            layers.append(ndvi(spring))

    if args.sar != "off":
        log(f"sentinel-1 mosaics {window(args.year, args.sar_months)}:")
        sar_items = search_s1(client, bbox, args.year, args.sar_months)
        sar = sar_composite(sar_items, grid) if sar_items else None
        if sar is None:
            log("! no Sentinel-1 mosaic over the bbox in that window - dropping the SAR "
                "features (coverage is thin over inner Siberia before 2025)")
        else:
            for key, name in (("VV", "VV_db"), ("VH", "VH_db"), ("VH_VV", "VH_VV")):
                names.append(name)
                layers.append(sar[key])

    log(f"features: {', '.join(names)}")
    return names, np.stack(layers)


# ---------------------------------------------------------------------- labels


def rasterize_labels(path: Path, grid: Grid) -> np.ndarray:
    """Hand-drawn training polygons burnt onto the grid.

    Either a 'code' field holding veglib's class numbers - which is what this tool
    and lc2veg.py both write - or a 'class' field holding their names.
    """
    source = ogr.Open(str(path))
    if source is None:
        raise SystemExit(f"--labels: cannot open {path}")
    layer = source.GetLayer(0)
    definition = layer.GetLayerDefn()
    fields = {definition.GetFieldDefn(i).GetName().lower(): definition.GetFieldDefn(i).GetName()
              for i in range(definition.GetFieldCount())}
    target = grid.reference()
    if "code" in fields:
        gdal.Rasterize(target, str(path), attribute=fields["code"], layers=[layer.GetName()])
    elif "class" in fields:
        # Every distinct value is looked at first. Burning only the names that
        # happen to match would drop the rest without a word, and a polygon drawn
        # and then silently ignored is the worst way for this to fail: the run
        # succeeds, the class is simply missing, and nothing says why.
        seen = {feature.GetField(fields["class"]) for feature in layer}
        layer.ResetReading()
        unknown = sorted(str(value) for value in seen if value not in CLASSES)
        if unknown:
            raise SystemExit(
                f"--labels: {path} uses class names that are not in the legend: "
                + ", ".join(unknown) + "\n    the classes are "
                + ", ".join(n for n in CLASSES if n != "nodata")
                + "\n    grassland is 'grass'; bedrock is 'bare_rock' and loose debris "
                "(talus or a moraine ridge) is 'scree'; the unsplit map class is 'bare'")
        for name, code in CLASSES.items():
            if code:
                gdal.Rasterize(target, str(path), burnValues=[code], layers=[layer.GetName()],
                               where=f"\"{fields['class']}\" = '{name}'")
    else:
        raise SystemExit(f"--labels: {path} has neither a 'code' nor a 'class' field; "
                         "the classes are " + ", ".join(n for n in CLASSES if n != "nodata"))
    labels = target.ReadAsArray()
    unknown = set(np.unique(labels)) - set(NAMES)
    if unknown:
        raise SystemExit(f"--labels: unknown class codes {sorted(unknown)} in {path}")
    return labels


def check_labels(grid: Grid, args) -> None:
    """That the hand-drawn labels are over this bbox at all - before the download.

    Labels are read after the imagery, so a file drawn over the wrong valley
    would otherwise announce itself at the end of a twenty minute fetch. The
    extents are in the file header; comparing them costs nothing.
    """
    west, south, east, north = grid.lonlat_bbox
    for source in args.labels:
        if source in COVER_SOURCES:
            continue
        data = ogr.Open(source)
        if data is None:
            raise SystemExit(f"--labels: cannot open {source}")
        layer = data.GetLayer(0)
        extent = layer.GetExtent()      # minX, maxX, minY, maxY
        reference = layer.GetSpatialRef()
        if reference is not None and not reference.IsSame(veglib.spatial_reference("EPSG:4326")):
            extent = veglib.reproject_bbox((extent[0], extent[2], extent[1], extent[3]),
                                           reference, veglib.spatial_reference("EPSG:4326"))
            extent = (extent[0], extent[2], extent[1], extent[3])
        if extent[1] < west or extent[0] > east or extent[3] < south or extent[2] > north:
            raise SystemExit(
                f"--labels: {source} does not overlap --bbox.\n"
                f"    the polygons are over {extent[0]:.4f} {extent[2]:.4f} "
                f"{extent[1]:.4f} {extent[3]:.4f}\n"
                f"    the bbox is        {west:.4f} {south:.4f} {east:.4f} {north:.4f}\n"
                "    nothing has been downloaded; fix one of the two and run again")
        log(f"labels {source}: {layer.GetFeatureCount()} features over the bbox")


def load_labels(grid: Grid, args) -> np.ndarray:
    """The training labels, one source laid over the next.

    A list rather than a single source because the two kinds of label are good at
    different things and the honest thing is to use both: a global map knows
    where the water and the scree are and is reliable about them over the whole
    bbox, and it is wrong about exactly the one class that matters. So the map
    goes down first and the hand-drawn polygons go on top of it, which is much
    less work than drawing all six classes by hand to get one of them right.
    """
    labels = np.zeros((grid.height, grid.width), dtype=np.uint8)
    # A caller that reads the map more than once in a run sets args.cover_cache,
    # so the remote map is fetched and warped once.
    cache = getattr(args, "cover_cache", None)
    for source in args.labels:
        if source in COVER_SOURCES and cache is not None and source in cache:
            layer = cache[source]
        elif source in COVER_SOURCES:
            cover = veglib.fetch_cover(source, veglib.snap_bbox(grid.lonlat_bbox),
                                       args.work, args.chunk, args.timeout)
            layer = veglib.reclass(warp(str(cover), grid, "mode"), source)
            if cache is not None:
                cache[source] = layer
        else:
            layer = rasterize_labels(Path(source), grid)
            drawn = int((layer > 0).sum())
            if not drawn:
                raise SystemExit(f"--labels: {source} has nothing over this bbox. Its own "
                                 "extent and --bbox may not overlap at all - check them "
                                 "against each other before blaming the polygons.")
            log(f"  {source}: {drawn} pixels drawn by hand, "
                f"{100 * drawn / labels.size:.2f}% of the bbox")
        # A class the later source has an opinion about is taken from it entirely
        # rather than merged: otherwise a dozen careful polygons of wood are
        # drowned by the map's own sixty per cent of it and never affect the
        # sample, and the point of drawing them was to disagree with the map.
        #
        # That is right for a class you are correcting and wrong for one you are
        # merely adding examples of - four patches of lichen cannot define the
        # class over a whole range the way the map's own quarter of the bbox can.
        # --replace says which are which; by default every class drawn is treated
        # as a correction.
        replaced = [code for code in np.unique(layer) if code
                    and (args.replaced is None or NAMES[int(code)] in args.replaced)]
        if replaced and labels.any():
            log("  " + ", ".join(NAMES[int(c)] for c in replaced)
                + f" now come only from {source}")
            labels = np.where(np.isin(labels, replaced), 0, labels)
        labels = np.where(layer > 0, layer, labels)
    return labels


def erode_labels(labels: np.ndarray, pixels: int) -> np.ndarray:
    """Only the interior of each labelled blob trains.

    A label raster and an image are never registered to the pixel, and a class
    boundary is mixed ground anyway; training on edges teaches the classifier the
    average of two classes.
    """
    if pixels < 1:
        return labels
    from scipy.ndimage import binary_erosion

    window = np.ones((2 * pixels + 1, 2 * pixels + 1), dtype=bool)
    out = np.zeros_like(labels)
    for code in sorted(NAMES):
        if code:
            inside = binary_erosion(labels == code, window, border_value=0)
            out[inside] = code
    return out


def sample(features: np.ndarray, labels: np.ndarray, usable: np.ndarray, args):
    """An equal-ish number of complete feature vectors per class, drawn at random."""
    rng = np.random.default_rng(args.seed)
    rows, targets = [], []
    taught, too_few = {}, {}
    flat = features.reshape(features.shape[0], -1)
    for code in sorted(NAMES):
        if not code:
            continue
        where = np.flatnonzero((labels == code).ravel() & usable.ravel())
        if where.size < args.min_samples:
            if where.size:
                too_few[NAMES[code]] = where.size
            continue
        taught[NAMES[code]] = min(where.size, args.samples)
        if where.size > args.samples:
            where = rng.choice(where, args.samples, replace=False)
        rows.append(flat[:, where].T)
        targets.append(np.full(where.size, code, dtype=np.uint8))
    if not rows:
        raise SystemExit("no class has enough training pixels over this bbox - "
                         "lower --min-samples, or draw the polygons yourself and "
                         "pass them as --labels")
    log("training pixels: " + ", ".join(f"{name} {n}" for name, n in sorted(taught.items())))
    if too_few:
        # Said out loud, because a class nobody teaches cannot come out of the
        # classifier and its ground will be handed to whichever class did train.
        log("  not taught, under --min-samples: "
            + ", ".join(f"{name} {n}" for name, n in sorted(too_few.items())))
    return np.vstack(rows), np.concatenate(targets)


def report_redundancy(names: list[str], X: np.ndarray) -> None:
    """Which features are saying the same thing twice.

    kNN and k-means both measure plain distance, so a quantity that is present
    three times counts three times. SWIR2 is nearly SWIR1 plus red,
    for one - left in, it quietly doubles the weight of one physical property
    against everything else. --drop is the answer; this
    is how the run says which ones to give it.
    """
    if len(names) < 2:
        return
    spread = X.std(axis=0)
    keep = spread > 0
    matrix = np.corrcoef(X[:, keep].T)
    live = [name for name, ok in zip(names, keep) if ok]
    duplicates = [(live[i], live[j], matrix[i, j])
                  for i in range(len(live)) for j in range(i + 1, len(live))
                  if abs(matrix[i, j]) > 0.9]
    eigenvalues = np.sort(np.linalg.eigvalsh(matrix))[::-1]
    share = np.cumsum(eigenvalues) / eigenvalues.sum()
    enough = int(np.searchsorted(share, 0.95)) + 1
    log(f"feature redundancy: {enough} of {len(live)} dimensions carry 95% of the variance")
    for first, second, correlation in duplicates:
        log(f"  {first} and {second} are the same thing (r={correlation:+.2f})")
    if duplicates:
        log("  distance counts a repeated quantity repeatedly - consider --drop")


def report_separation(names: list[str], X: np.ndarray, y: np.ndarray) -> None:
    """The median of every feature in every class - which is where one sees
    whether the spring NDVI is in fact doing its job on this bbox."""
    codes = sorted(set(y.tolist()))
    header = "".join(f"{NAMES[code]:>9}" for code in codes)
    log(f"class medians  {'':<12}{header}")
    for index, name in enumerate(names):
        row = "".join(f"{np.median(X[y == code, index]):>9.3f}" for code in codes)
        log(f"               {name:<12}{row}")


# ------------------------------------------------------------------- classifier


def predict_all(model, centre, spread, features: np.ndarray, usable: np.ndarray) -> np.ndarray:
    """Every pixel with a complete feature vector, in chunks."""
    flat = features.reshape(features.shape[0], -1).T
    out = np.zeros(flat.shape[0], dtype=np.uint8)
    rows = np.flatnonzero(usable.ravel())
    step = 200_000
    for start in range(0, rows.size, step):
        piece = rows[start:start + step]
        out[piece] = model.predict((flat[piece] - centre) / spread)
        log(f"  classified {min(start + step, rows.size)}/{rows.size} pixels")
    return out.reshape(features.shape[1:])


def report_novelty(model, centre, spread, features: np.ndarray, usable: np.ndarray,
                   trained: dict) -> None:
    """How far this ground is from anything the model was trained on.

    A saved classifier applied somewhere else is an extrapolation until proven
    otherwise, and kNN can say how far: the distance to the nearest training
    point. Compared against the same figure measured where it was trained, it
    separates "the same kind of country" from "nothing like it, but the vote has
    to return something anyway".
    """
    flat = features.reshape(features.shape[0], -1).T
    rows = np.flatnonzero(usable.ravel())
    if rows.size == 0:
        return
    sample = rows if rows.size <= 20_000 else np.random.default_rng(0).choice(
        rows, 20_000, replace=False)
    distances, _ = model.kneighbors((flat[sample] - centre) / spread, n_neighbors=1)
    here = np.percentile(distances, [50, 90])
    was = trained.get("distance_percentiles")
    log(f"distance to the nearest training pixel: median {here[0]:.2f}, p90 {here[1]:.2f}")
    if was:
        log(f"  where it was trained it was:          median {was[0]:.2f}, p90 {was[1]:.2f}")
        if here[1] > 3 * was[1]:
            log("  ! this ground is much further from the training set than the training "
                "set is from itself - the classes it returns here are guesses")
        elif here[1] > 1.5 * was[1]:
            log("  ! noticeably further out than where it was trained; check the result "
                "against imagery before trusting it")


def save_model(path: Path, model, centre, spread, names: list[str], grid: Grid,
               distances, args) -> None:
    import joblib

    joblib.dump({
        "features": names, "centre": centre, "spread": spread, "model": model,
        "distance_percentiles": list(distances),
        "res": args.res, "bands": args.bands, "year": args.year,
        "summer": args.summer, "spring": args.spring, "sar_months": args.sar_months,
        "texture": args.texture, "flatten_sar": args.flatten_sar,
        "parents": getattr(args, "parents", False), "slope": getattr(args, "slope", False),
        "trained_on": list(grid.lonlat_bbox), "labels": args.labels,
    }, path)
    log(f"model: {path}")


def check_model(path: Path, args) -> None:
    """That this run will build the vector the model wants - before the download.

    The feature names are only known once the stack exists, so the full check
    happens after the imagery is in hand. The settings that decide those names
    are known from the command line, and comparing them costs nothing: getting
    --texture wrong should not be worth twenty minutes of downloading.
    """
    import joblib

    if not path.exists():
        raise SystemExit(f"--model: {path} does not exist")
    saved = joblib.load(path)
    compared = [("bands", args.bands), ("texture", args.texture),
                ("flatten_sar", args.flatten_sar)]
    # s2rock.py's own switches. A model trained by it has them stored; an older
    # file simply lacks them.
    for key in ("parents", "slope"):
        if key in saved:
            compared.append((key, getattr(args, key, False)))
    wrong = [(key, saved.get(key), mine) for key, mine in compared
             if saved.get(key) != mine]
    if wrong:
        lines = "\n".join(f"    --{key.replace('_', '-')}: trained with {was}, "
                           f"this run would use {mine}" for key, was, mine in wrong)
        raise SystemExit(f"{path} would not get the feature vector it was trained on:\n"
                         + lines + "\n    nothing has been downloaded")


def load_model(path: Path, names: list[str], grid: Grid, args):
    """A classifier trained elsewhere, with the comparisons that make it valid.

    The feature vector has to be the same quantities in the same order, which is
    checked and refused outright. The rest - resolution, windows, year - only
    warns, because a different season is a judgement about the ground rather than
    a broken array, and it is the user's to make.
    """
    import joblib

    saved = joblib.load(path)
    if saved["features"] != names:
        raise SystemExit(
            f"{path} was trained on a different feature vector.\n"
            f"    it wants  {', '.join(saved['features'])}\n"
            f"    this run has {', '.join(names)}\n"
            "    match --bands, --texture, --flatten-sar (and s2rock's --no-slope) "
            "to the training run")
    for key, mine in (("res", args.res), ("year", args.year), ("summer", args.summer),
                      ("spring", args.spring), ("sar_months", args.sar_months)):
        if saved.get(key) != mine:
            log(f"! {key}: trained with {saved.get(key)}, this run uses {mine}")
    west, south, east, north = saved["trained_on"]
    log(f"model from {path}, trained over {west:.3f} {south:.3f} {east:.3f} {north:.3f} "
        f"on {', '.join(saved['labels'])}")
    return saved


def classify(names: list[str], features: np.ndarray, labels: np.ndarray, args) -> np.ndarray:
    from sklearn.model_selection import train_test_split
    from sklearn.neighbors import KNeighborsClassifier

    usable = np.all(np.isfinite(features), axis=0)
    log(f"pixels with a complete feature vector: {100 * usable.mean():.1f}%")
    X, y = sample(features, erode_labels(labels, args.label_erode), usable, args)
    report_redundancy(names, X)
    report_separation(names, X, y)

    # kNN measures distance, so a band in reflectance units and a backscatter in
    # decibels have to be brought onto one scale first or the decibels decide
    # everything on their own.
    centre, spread = X.mean(axis=0), X.std(axis=0)
    spread[spread == 0] = 1.0
    X = (X - centre) / spread

    X_fit, X_test, y_fit, y_test = train_test_split(
        X, y, test_size=0.25, random_state=args.seed, stratify=y)
    model = KNeighborsClassifier(n_neighbors=args.neighbors, weights="distance", n_jobs=-1)
    model.fit(X_fit, y_fit)
    predicted = model.predict(X_test)
    log(f"kNN k={args.neighbors}: {100 * (predicted == y_test).mean():.1f}% of the held-out "
        "training pixels classified as they were labelled")
    for code in sorted(set(y_test.tolist())):
        held = y_test == code
        log(f"  {NAMES[code]:<8} recall {100 * (predicted[held] == code).mean():5.1f}%"
            f"  ({held.sum()} pixels)")
    log("  (this is agreement with the labels, not accuracy on the ground - labels "
        "drawn from a global map carry that map's mistakes into the score)")

    if args.save_model:
        # How far the training pixels sit from each other, kept with the model so
        # that applying it elsewhere can be compared against it.
        spacing, _ = model.kneighbors(X_fit, n_neighbors=2)
        save_model(args.save_model, model, centre, spread, names, args.grid,
                   np.percentile(spacing[:, 1], [50, 90]), args)
    return predict_all(model, centre, spread, features, usable)


def elevation(grid: Grid) -> np.ndarray | None:
    """The bbox out of the global 30 m DTM, for the report only.

    Deliberately not a feature. Height is the single most useful thing to judge a
    cluster by in the mountains - dwarf pine sits in a belt between where the
    forest stops and where the goltsy begin - but feeding it to the classifier
    would let it draw contour lines and call them vegetation. As a column next to
    the medians it is an independent witness; as a feature it would be a
    self-fulfilling one.
    """
    sys.path.insert(0, str(TOOLS_DIR.parent / "www"))
    try:
        from otmlib import dem as otm_dem
    except ImportError as failure:
        log(f"  (no elevation column: {failure})")
        return None
    dem = warp(otm_dem.GEDTM_VSI_URL, grid, "bilinear")
    return None if dem is None else dem.astype(np.float32)


SAR_FEATURES = ("VV_db", "VH_db", "VH_VV")


def terrain(grid: Grid, height: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Slope and aspect in radians, from the DTM already on the working grid."""
    dy, dx = np.gradient(height, grid.res)
    return np.arctan(np.hypot(dx, dy)), np.arctan2(-dx, dy)


def flatten_sar(names: list[str], features: np.ndarray, grid: Grid,
                args) -> np.ndarray:
    """Take the relief out of the SAR features, as far as a DEM can.

    Why not the textbook cosine correction: it multiplies both polarizations by
    the same function of the incidence angle, and in VH/VV that factor cancels
    exactly. Whatever aspect dependence the ratio still has is therefore not a
    gain - it is the polarization-dependent part of the angular response, and it
    has to be measured rather than derived.

    So it is measured, by least squares against slope and aspect, with NDVI in
    the model as a covariate and only the geometric terms subtracted afterwards.
    The covariate is the whole point: vegetation is itself organised by aspect in
    the mountains (here NDVI runs 0.43 on north-facing ground against 0.72 on
    south-facing), so a fit without it would charge that difference to geometry
    and normalize away the signal being looked for. The level is preserved - the
    mean of the geometric term is added back - so only its variation goes.

    The residual assumption, and it is a real one: that the angular response is
    additive and the same over the whole bbox. A monthly mosaic mixes passes and
    look directions, so this is an average correction, not a per-pixel one. A
    per-pixel one needs the acquisition geometry, which means terrain-flattened
    RTC from GRD or SLC rather than these mosaics.
    """
    present = [name for name in names if name in SAR_FEATURES]
    if not present:
        log("--flatten-sar: no SAR features in the stack, nothing to flatten")
        return features
    height = elevation(grid)
    if height is None:
        log("! --flatten-sar needs the DTM and could not read it; leaving the SAR as it is")
        return features

    slope, aspect = terrain(grid, height)
    steep = slope > np.radians(5)
    columns = [np.ones(slope.size), np.cos(aspect).ravel(), np.sin(aspect).ravel(),
               slope.ravel(), (slope ** 2).ravel()]
    geometric = slice(1, 5)       # everything but the intercept is relief
    if "NDVI" in names:
        columns.append(features[names.index("NDVI")].ravel())
    else:
        log("  (no NDVI in the stack to hold vegetation constant - the correction will "
            "absorb some of it)")
    design = np.column_stack(columns)

    quadrants = {"N": (aspect >= np.radians(135)) | (aspect < np.radians(-135)),
                 "E": (aspect >= np.radians(-135)) & (aspect < np.radians(-45)),
                 "S": (aspect >= np.radians(-45)) & (aspect < np.radians(45)),
                 "W": (aspect >= np.radians(45)) & (aspect < np.radians(135))}

    def aspect_spread(values: np.ndarray) -> float:
        medians = [np.nanmedian(values[steep & mask]) for mask in quadrants.values()
                   if (steep & mask & np.isfinite(values)).sum() > 300]
        return max(medians) - min(medians) if len(medians) > 2 else float("nan")

    out = features.copy()
    for name in present:
        index = names.index(name)
        values = features[index]
        fit = np.isfinite(values.ravel()) & np.all(np.isfinite(design), axis=1) & steep.ravel()
        if fit.sum() < 1000:
            log(f"  {name}: too little steep ground to fit a correction, left alone")
            continue
        coefficients, *_ = np.linalg.lstsq(design[fit], values.ravel()[fit], rcond=None)
        relief = (design[:, geometric] @ coefficients[geometric]).reshape(values.shape)
        corrected = values - relief + np.nanmean(relief[fit.reshape(values.shape)])
        before, after = aspect_spread(values), aspect_spread(corrected)
        log(f"  {name}: aspect spread {before:.3f} -> {after:.3f}")
        out[index] = corrected.astype(np.float32)
    return out


def local_sd(values: np.ndarray, window: int) -> np.ndarray:
    """Standard deviation in a window, ignoring the holes.

    sqrt(E[x2] - E[x]2) over a box filter, but with the count of finite
    neighbours as the denominator, so a pixel beside a cloud gap is not quietly
    pulled towards zero. Under a third of the window present and it is nodata.
    """
    from scipy.ndimage import uniform_filter

    good = np.isfinite(values)
    filled = np.where(good, values, 0.0).astype(np.float32)
    count = uniform_filter(good.astype(np.float32), window, mode="nearest")
    with np.errstate(invalid="ignore", divide="ignore"):
        first = uniform_filter(filled, window, mode="nearest") / count
        second = uniform_filter(filled * filled, window, mode="nearest") / count
        out = np.sqrt(np.clip(second - first * first, 0.0, None))
    out[count < 1.0 / 3.0] = np.nan
    return out.astype(np.float32)


def add_texture(names: list[str], features: np.ndarray,
                window: int) -> tuple[list[str], np.ndarray]:
    """How mixed the ground is inside a small window.

    The obvious argument for this - a forest canopy is lumpy at 10-20 m and a mat
    of dwarf pine is smooth - turns out to be backwards, and it is worth writing
    down which way round it actually goes. Over the Kodar bbox the median NDVI
    spread in a 5 x 5 window is 0.031 under closed forest, 0.030 under meadow and
    0.092 in the dwarf pine belt: three times either. Closed forest is uniform at
    this scale and so is a meadow; what is not uniform is the belt between them,
    where dwarf pine, scree and patches of tundra sit side by side inside one
    20 m pixel. So the feature finds an ecotone, not a canopy - useful for exactly
    this job, but for a different reason than the one it looks like.

    The other tempting claim, that texture is blind to the terrain the way the
    SAR ratio is not, is also false. With NDVI held constant on steep ground the
    medians swing 46 % of their own level between aspects (63 % for the infrared),
    against 17 % for VH/VV - hillshade makes texture. It is still usable, because
    the gap between the classes is about twice that swing, but it is not clean.

    Taken on NDVI and on the near infrared.
    """
    layers, added = [], []
    for source in ("NDVI", *NIR_BANDS):
        name = f"{source}_sd"
        if source not in names or name in names:
            continue
        added.append(name)
        layers.append(local_sd(features[names.index(source)], window))
    if not layers:
        log(f"--texture: nothing to take it on; the stack has {', '.join(names)}")
        return names, features
    log(f"texture over a {window}x{window} window: {', '.join(added)}")
    return names + added, np.concatenate([features, np.stack(layers)])


def parse_names(value: str) -> dict[int, int]:
    """'3=scrub,5=wood' - which cluster is which class, when the labels are wrong
    about it or say nothing at all."""
    naming = {}
    for pair in (piece.strip() for piece in value.split(",") if piece.strip()):
        number, _, name = pair.partition("=")
        if not number.strip().isdigit() or name.strip() not in CLASSES:
            raise SystemExit(f"--name: expected CLUSTER=CLASS, got {pair!r}; the classes "
                             "are " + ", ".join(n for n in CLASSES if n != "nodata"))
        naming[int(number)] = CLASSES[name.strip()]
    return naming


def name_clusters(ids: np.ndarray, labels: np.ndarray, args) -> dict[int, int]:
    """What each cluster is called: what --name says, else what the labels call
    most of it.

    k-means splits the ground; it cannot say which half is wood. The global map
    is good enough at naming - it is only at splitting that it fails, which is
    exactly the other way round, so the two fit together.
    """
    naming = dict(args.named)
    for cluster_id in range(1, args.clusters + 1):
        if cluster_id in naming:
            continue
        inside = labels[(ids == cluster_id) & (labels > 0)]
        if inside.size:
            codes, counts = np.unique(inside, return_counts=True)
            naming[cluster_id] = int(codes[counts.argmax()])
    unnamed = [c for c in range(1, args.clusters + 1) if c not in naming]
    if unnamed:
        log(f"! clusters {unnamed} fall on no labelled pixel at all and stay nodata - "
            "name them with --name")
    return naming


def report_clusters(names: list[str], features: np.ndarray, ids: np.ndarray,
                    labels: np.ndarray, naming: dict[int, int], grid: Grid, args) -> None:
    """Every cluster's size, what the labels call it, and its median features.

    This table is the whole point of clustering instead of classifying: a cluster
    the map calls wood but whose NDVI_spring sits near zero and whose VH/VV is low
    is dwarf pine, and --name turns it into one.
    """
    present = [c for c in range(1, args.clusters + 1) if (ids == c).any()]
    flat = features.reshape(features.shape[0], -1)
    header = "".join(f"{c:>9}" for c in present)
    log(f"clusters       {'':<12}{header}")
    log(f"               {'share %':<12}"
        + "".join(f"{100 * (ids == c).mean():>9.1f}" for c in present))
    log(f"               {'named':<12}"
        + "".join(f"{NAMES.get(naming.get(c, 0), '-'):>9}" for c in present))
    height = elevation(grid) if args.elevation else None
    if height is not None:
        for label, percentile in (("elev p25 m", 25), ("elev med m", 50), ("elev p75 m", 75)):
            log(f"               {label:<12}" + "".join(
                f"{np.percentile(height[ids == c], percentile):>9.0f}" for c in present))
        # Printed for s2rock, and kept out of the distance. A moraine ridge is
        # scree on gentle ground far from any cliff, so the column describes
        # the landform and does not name the cluster.
        if getattr(args, "report_slope", False):
            finite = np.isfinite(height)
            fill = np.nanmedian(height[finite]) if finite.any() else 0.0
            slope_deg = np.degrees(terrain(grid, np.where(finite, height, fill))[0])
            slope_deg = np.where(finite, slope_deg, np.nan)
            for label, percentile in (("slope p25 °", 25), ("slope med °", 50),
                                      ("slope p75 °", 75)):
                log(f"               {label:<12}" + "".join(
                    f"{np.nanpercentile(slope_deg[ids == c], percentile):>9.0f}"
                    for c in present))
    for index, name in enumerate(names):
        row = ""
        for cluster_id in present:
            inside = flat[index][(ids == cluster_id).ravel()]
            row += f"{np.median(inside):>9.3f}" if inside.size else f"{'-':>9}"
        log(f"               {name:<12}{row}")
    for cluster_id in present:
        inside = labels[ids == cluster_id]
        inside = inside[inside > 0]
        if not inside.size:
            continue
        codes, counts = np.unique(inside, return_counts=True)
        order = np.argsort(-counts)[:3]
        log(f"  cluster {cluster_id} is labelled " + ", ".join(
            f"{NAMES[int(codes[i])]} {100 * counts[i] / counts.sum():.0f}%" for i in order))


def cluster(names: list[str], features: np.ndarray, labels: np.ndarray, grid: Grid,
            args) -> tuple[np.ndarray, np.ndarray]:
    """k-means over the same features, and a class per cluster.

    Unsupervised where it helps - nothing has to have been mapped before for the
    split to happen, so dwarf pine can come out as its own cluster even though no
    global map contains it - and supervised only at the naming, which is the part
    a global map is actually good at.
    """
    from sklearn.cluster import KMeans

    usable = np.all(np.isfinite(features), axis=0)
    log(f"pixels with a complete feature vector: {100 * usable.mean():.1f}%")
    if args.within:
        # Split one of the map's own classes instead of the whole bbox: take its
        # word for where the trees are, and only ask the imagery which of them
        # are not trees at all. Everything outside keeps the class it was given.
        inside = np.isin(labels, [CLASSES[name] for name in args.within])
        usable &= inside
        log(f"clustering only inside {', '.join(args.within)}: "
            f"{100 * inside.mean():.1f}% of the bbox, {100 * usable.mean():.1f}% of it usable")
    flat = features.reshape(features.shape[0], -1).T
    rows = np.flatnonzero(usable.ravel())
    if rows.size < args.clusters * 10:
        raise SystemExit(f"only {rows.size} pixels have every feature - not enough to "
                         f"split into {args.clusters} clusters")

    # Fitted on a sample: k-means on every pixel of a 10 m bbox costs minutes and
    # moves no centre that 100k pixels have not already found.
    rng = np.random.default_rng(args.seed)
    fit_rows = rng.choice(rows, min(args.fit_sample, rows.size), replace=False)
    X = flat[fit_rows]
    centre, spread = X.mean(axis=0), X.std(axis=0)
    spread[spread == 0] = 1.0
    report_redundancy(names, X)
    model = KMeans(n_clusters=args.clusters, n_init=10, random_state=args.seed)
    model.fit((X - centre) / spread)
    log(f"k-means k={args.clusters}: fitted on {fit_rows.size} pixels, "
        f"inertia {model.inertia_ / fit_rows.size:.3f} per pixel")

    ids = np.zeros(flat.shape[0], dtype=np.uint8)
    step = 200_000
    for start in range(0, rows.size, step):
        piece = rows[start:start + step]
        ids[piece] = model.predict((flat[piece] - centre) / spread) + 1   # 0 stays nodata
        log(f"  clustered {min(start + step, rows.size)}/{rows.size} pixels")
    ids = ids.reshape(features.shape[1:])

    naming = name_clusters(ids, labels, args)
    report_clusters(names, features, ids, labels, naming, grid, args)
    # Outside the split the labels stand as they are; inside it the clusters do.
    out = np.where(np.isin(labels, [CLASSES[n] for n in args.within]) if args.within
                   else False, 0, labels).astype(np.uint8)
    for cluster_id, code in naming.items():
        out[ids == cluster_id] = code
    return out, ids


def shares(labels: np.ndarray) -> str:
    total = max(int((labels > 0).sum()), 1)
    return ", ".join(f"{NAMES[code]} {100 * int((labels == code).sum()) / total:.1f}%"
                     for code in sorted(NAMES) if code and (labels == code).any())


# ------------------------------------------------------------------------- CLI


def parse_args(argv=None):
    today = dt.date.today()
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Classes: " + ", ".join(name for name in CLASSES if name != "nodata"))
    parser.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("W", "S", "E", "N"),
                        help="area in EPSG:4326 - everything is fetched and classified "
                             "inside it")
    parser.add_argument("-o", "--output", type=Path, default=Path("veg.gpkg"),
                        help="output polygons; driver from the extension, .tif writes only "
                             "the class raster (default: veg.gpkg)")
    parser.add_argument("--raster", type=Path, help="also write the class raster here (GeoTIFF)")
    parser.add_argument("--features", type=Path, metavar="PATH",
                        help="also write the feature stack here, one band per feature, for "
                             "looking at in QGIS before blaming the classifier")
    parser.add_argument("--features-in", type=Path, metavar="PATH",
                        help="read the stack a previous run wrote instead of fetching the "
                             "imagery again - the cheap way to iterate on --name, and it "
                             "needs no credentials")
    parser.add_argument("--year", type=int, default=today.year - (today.month < 10),
                        help=f"season to classify (default: {today.year - (today.month < 10)})")
    parser.add_argument("--summer", default="06-15:09-15", metavar="MM-DD:MM-DD",
                        help="high summer window, full canopy (default: 06-15:09-15)")
    parser.add_argument("--spring", default="05-01:06-15", metavar="MM-DD:MM-DD",
                        help="snowmelt window for NDVI_spring, 'off' to skip it "
                             "(default: 05-01:06-15)")
    parser.add_argument("--sar", default="auto", choices=("auto", "off"),
                        help="use the Sentinel-1 mosaics, or do without them (default: auto)")
    parser.add_argument("--sar-months", default="06-01:09-30", metavar="MM-DD:MM-DD",
                        help="window the monthly S1 mosaics are taken from (default: 06-01:09-30)")
    parser.add_argument("--max-cloud", type=float, default=40.0, metavar="PCT",
                        help="skip scenes cloudier than this (default: 40)")
    parser.add_argument("--bands", default="B04,B08,B11,B12",
                        help="reflectance bands to fetch: red, a near infrared and the two "
                             "SWIR by default. B02 and B03 add little a canopy can be told "
                             "apart by and cost a third of the download; at --res 20 or "
                             "coarser swap B08 for B8A (default: B04,B08,B11,B12)")
    parser.add_argument("--workers", type=int, default=6, metavar="N",
                        help="scenes read from the store at once; the reads are network "
                             "bound, so this is what sets the wall clock (default: 6)")
    parser.add_argument("--scenes", type=int, default=4, metavar="N",
                        help="clear looks wanted at every pixel: scenes are added, best "
                             "first, until the bbox has this many cloud-free observations "
                             "or the candidates run out (default: 4)")
    parser.add_argument("--labels", default="lcfm",
                        help="training labels, comma separated and laid over each other in "
                             "order: 'lcfm', 'worldcover', or a vector file with a 'code' or "
                             "'class' field. 'worldcover,mine.gpkg' takes the map for the "
                             "classes it gets right and your polygons for the ones it does "
                             "not, which beats drawing all six by hand (default: lcfm)")
    parser.add_argument("--samples", type=int, default=3000, metavar="N",
                        help="training pixels drawn per class (default: 3000)")
    parser.add_argument("--min-samples", type=int, default=50, metavar="N",
                        help="a class with fewer labelled pixels than this is not taught at all "
                             "(default: 50)")
    parser.add_argument("--label-erode", type=int, default=2, metavar="PX",
                        help="shrink each labelled blob by this much before sampling, so only "
                             "its interior trains (default: 2)")
    parser.add_argument("--method", default="knn", choices=("knn", "kmeans"),
                        help="knn learns the classes from the labels; kmeans splits the "
                             "bbox on the features alone and only asks the labels what to "
                             "call each cluster (default: knn)")
    parser.add_argument("--neighbors", type=int, default=7, metavar="K",
                        help="k of the kNN (default: 7)")
    parser.add_argument("--clusters", type=int, default=8, metavar="N",
                        help="clusters k-means splits the bbox into (default: 8)")
    parser.add_argument("--name", default="", metavar="N=CLASS,...",
                        help="name clusters yourself, overriding what the labels call them "
                             "- this is how a cluster becomes scrub")
    parser.add_argument("--cluster-raster", type=Path, metavar="PATH",
                        help="write the raw cluster numbers here, to look at next to the "
                             "table before deciding what --name to give them")
    parser.add_argument("--drop", default="", metavar="FEATURE,...",
                        help="features to leave out of the classification - they are still "
                             "written to --features. B12 is nearly B11 plus B04, for one, "
                             "so distance counts that colour twice unless it is "
                             "dropped; the run names the pairs it finds")
    parser.add_argument("--within", default="",
                        help="with --method kmeans, cluster only the pixels the labels put "
                             "in these classes and leave the rest as labelled - 'wood' "
                             "splits a map's tree cover into real forest and whatever else "
                             "it had put there")
    parser.add_argument("--replace", default="", metavar="CLASS,...",
                        help="which classes a hand-drawn --labels file takes over from the "
                             "map entirely; the rest of its polygons are merely added to "
                             "what the map already says. Default: every class it contains, "
                             "which is right when you are correcting the map and wrong when "
                             "you are only adding a few examples")
    parser.add_argument("--save-model", type=Path, metavar="PATH",
                        help="save the trained kNN, its feature list and its scaling here, "
                             "to apply to another bbox with --model")
    parser.add_argument("--model", type=Path, metavar="PATH",
                        help="classify with a model saved by an earlier run instead of "
                             "training: no labels are needed or fetched, and the run "
                             "reports how far this ground is from what it was trained on")
    parser.add_argument("--texture", type=int, default=0, metavar="PX",
                        help="add the local standard deviation of NDVI and the near "
                             "infrared over a PX window as features: closed forest and "
                             "open meadow are both uniform at this scale, the belt between "
                             "them is not, so it picks out the ecotone the dwarf pine "
                             "lives in (default: 0, off; 5 is a reasonable window)")
    parser.add_argument("--flatten-sar", action="store_true",
                        help="take the relief out of the SAR features before classifying: "
                             "least squares against slope and aspect with NDVI held "
                             "constant, geometric part subtracted. In this relief slope and "
                             "aspect account for about a fifth of the variance of VH/VV, "
                             "and a plain cosine correction cannot touch a ratio")
    parser.add_argument("--no-elevation", dest="elevation", action="store_false",
                        help="skip the elevation column in the cluster table (it is read "
                             "from the same global 30 m DTM the DEM tools use, and it is a "
                             "report column only, never a feature)")
    parser.add_argument("--fit-sample", type=int, default=100_000, metavar="N",
                        help="pixels k-means is fitted on (default: 100000)")
    parser.add_argument("--seed", type=int, default=0, help="random seed (default: 0)")
    parser.add_argument("--classes", default="wood,scrub,grass",
                        help="classes to polygonize; everything else is still classified, so "
                             "a lake is not called grass (default: wood,scrub,grass)")
    parser.add_argument("--res", type=float, default=10.0, metavar="M",
                        help="working cell size in metres (default: 10)")
    parser.add_argument("--crs", default="auto",
                        help="working CRS: 'auto' is the UTM zone of the centre (default: auto)")
    parser.add_argument("--out-crs", default="EPSG:4326", help="output CRS (default: EPSG:4326)")
    parser.add_argument("--smooth", type=int, default=5, metavar="PX",
                        help="majority filter window, 0 keeps the raw classification "
                             "(default: 5)")
    parser.add_argument("--min-area", type=float, default=1.0, metavar="HA",
                        help="drop polygons smaller than this (default: 1.0)")
    parser.add_argument("--simplify", type=float, default=-1.0, metavar="D",
                        help="simplification tolerance in metres, applied to shared edges so "
                             "neighbours stay gap-free; -1 is 1.5 cells (default: -1)")
    parser.add_argument("--dry-run", action="store_true",
                        help="list the scenes that would be used and stop - needs no credentials")
    parser.add_argument("--chunk", type=int, default=4000, metavar="PX",
                        help="label tile server requests are cut into chunks this big "
                             "(default: 4000)")
    parser.add_argument("--timeout", type=int, default=300, metavar="S",
                        help="seconds to wait for one chunk (default: 300)")
    parser.add_argument("--work-dir", type=Path,
                        help="where the intermediates go (default: a temp dir next to the output)")
    parser.add_argument("--keep-work", action="store_true", help="do not delete the work dir")
    args = parser.parse_args(argv)

    veglib.validate_output(args.output)
    if args.res <= 0:
        parser.error("--res must be positive")
    if args.neighbors < 1:
        parser.error("--neighbors must be at least 1")
    if args.clusters < 2 or args.clusters > 254:
        parser.error("--clusters must be between 2 and 254")
    for name in ("summer", "spring", "sar_months"):
        value = getattr(args, name)
        if value != "off" and len(value.split(":")) != 2:
            parser.error(f"--{name.replace('_', '-')} must look like MM-DD:MM-DD")
    args.bbox = veglib.normalize_bbox(parser, args.bbox)
    args.wanted = veglib.parse_classes(args.classes)
    args.bands = [band.strip().upper() for band in args.bands.split(",") if band.strip()]
    unknown = [band for band in args.bands if band not in S2_BANDS]
    if unknown:
        parser.error(f"--bands: unknown {', '.join(unknown)}; known are "
                     + ", ".join(S2_BANDS))
    if "B04" not in args.bands or not any(b in args.bands for b in NIR_BANDS):
        parser.error("--bands must keep B04 and one of " + " / ".join(NIR_BANDS)
                     + " - NDVI is computed from them")
    if len(set(args.bands) & set(NIR_BANDS)) > 1:
        parser.error("--bands: pick one near infrared, " + " or ".join(NIR_BANDS))
    args.replaced = veglib.parse_classes(args.replace) if args.replace else None
    args.labels = [source.strip() for source in args.labels.split(",") if source.strip()]
    if not args.labels:
        parser.error("--labels needs at least one source")
    args.dropped = [name.strip() for name in args.drop.split(",") if name.strip()]
    args.within = veglib.parse_classes(args.within) if args.within else []
    args.named = parse_names(args.name)
    unknown = [c for c in args.named if not 1 <= c <= args.clusters]
    if unknown:
        parser.error(f"--name: cluster {unknown} is outside 1..{args.clusters}")
    args.simplify = 1.5 * args.res if args.simplify < 0 else args.simplify
    return args


def list_scenes(client, grid: Grid, args) -> None:
    for label, span, snow in (("summer", args.summer, False), ("spring", args.spring, True),
                              ("sentinel-1", args.sar_months, None)):
        if span == "off" or (snow is None and args.sar == "off"):
            continue
        bbox = grid.lonlat_bbox
        items = (search_s1(client, bbox, args.year, span) if snow is None else
                 search_s2(client, bbox, args.year, span, args.max_cloud, snow))
        log(f"{label} {window(args.year, span)}: {len(items)} items")
        for item in items:
            share = "" if snow is None else f"  footprint {100 * footprint_share(item, bbox):.0f}% of the bbox"
            log(f"  {item.id}{share}")


def main(argv=None) -> int:
    args = parse_args(argv)
    veglib.require("pystac_client", "sklearn", "scipy")

    crs, bounds = veglib.working_grid(args.bbox, args.crs, args.res)
    grid = Grid(crs, bounds, args.res)
    args.grid = grid
    log(f"area: {(bounds[2] - bounds[0]) / 1000:.1f} x {(bounds[3] - bounds[1]) / 1000:.1f} km "
        f"in {crs} at {args.res:g} m ({grid.width} x {grid.height} px)")

    if args.dry_run:
        list_scenes(stac_client(), grid, args)
        return 0

    args.work = veglib.work_directory(args)
    if args.model:
        check_model(args.model, args)
    else:
        check_labels(grid, args)
    if args.features_in:
        names, features = read_feature_stack(args.features_in, grid)
    else:
        open_s3()
        names, features = feature_stack(stac_client(), grid, args)
    reference = grid.reference()
    if args.texture > 1:
        veglib.require("scipy")
        names, features = add_texture(names, features, args.texture)
    if args.features:
        write_features(args.features, names, features, reference)
    if args.flatten_sar:
        log("flattening the SAR features against slope and aspect:")
        features = flatten_sar(names, features, grid, args)
    if args.dropped:
        unknown = [name for name in args.dropped if name not in names]
        if unknown:
            raise SystemExit(f"--drop: {', '.join(unknown)} is not in the stack, which has "
                             + ", ".join(names))
        keep = [index for index, name in enumerate(names) if name not in args.dropped]
        names, features = [names[i] for i in keep], features[keep]
        log(f"dropped {', '.join(args.dropped)}; classifying on {', '.join(names)}")

    if args.model:
        saved = load_model(args.model, names, grid, args)
        usable = np.all(np.isfinite(features), axis=0)
        log(f"pixels with a complete feature vector: {100 * usable.mean():.1f}%")
        report_novelty(saved["model"], saved["centre"], saved["spread"], features,
                       usable, saved)
        predicted = predict_all(saved["model"], saved["centre"], saved["spread"],
                                features, usable)
        log(f"classified: {shares(predicted)}")
        return finish(predicted, grid, reference, args)

    labels = load_labels(grid, args)
    log(f"labels ({', '.join(args.labels)}): {shares(labels)}")
    if ("scrub" in args.wanted and args.method == "knn"
            and all(source in COVER_SOURCES for source in args.labels)):
        log("! neither global map maps much shrub in the boreal zone, so a scrub class "
            "trained on one will be nearly empty. Draw a few dwarf pine patches by hand "
            "and pass them as --labels, or split the bbox yourself with --method kmeans.")

    if args.method == "kmeans":
        predicted, ids = cluster(names, features, labels, grid, args)
        if args.cluster_raster:
            write_clusters(args.cluster_raster, ids, reference)
    else:
        predicted = classify(names, features, labels, args)
    log(f"classified: {shares(predicted)}")

    return finish(predicted, grid, reference, args)


def finish(predicted: np.ndarray, grid: Grid, reference: gdal.Dataset, args) -> int:
    """Clean up the classification and write what was asked for."""
    valid = predicted > 0
    if args.smooth > 1:
        predicted = veglib.majority_of(predicted, valid, args.smooth)
    predicted = veglib.sieve(predicted, reference,
                             int(args.min_area * 10000 / (args.res * args.res)))
    log(f"cleaned: {shares(predicted)}")

    raster = args.output if args.output.suffix.lower() in RASTER_SUFFIXES else args.raster
    if raster:
        veglib.write_raster(raster, predicted, reference)
    if args.output.suffix.lower() in OGR_DRIVERS:
        raw = veglib.polygonize(predicted, reference, args.wanted, args)
        veglib.write_vector(raw, args.output, "landcover", args)

    veglib.cleanup(args.work, args.keep_work)
    return 0


def write_clusters(path: Path, ids: np.ndarray, reference: gdal.Dataset) -> None:
    """The raw cluster numbers - no palette, because veglib's colours mean classes
    and these are not classes yet."""
    out = gdal.GetDriverByName("GTiff").Create(
        str(path), ids.shape[1], ids.shape[0], 1, gdal.GDT_Byte,
        ["TILED=YES", "COMPRESS=DEFLATE"])
    out.SetGeoTransform(reference.GetGeoTransform())
    out.SetProjection(reference.GetProjection())
    band = out.GetRasterBand(1)
    band.SetNoDataValue(0)
    band.WriteArray(ids)
    del out
    log(f"cluster raster: {path}")


def write_features(path: Path, names: list[str], features: np.ndarray,
                   reference: gdal.Dataset) -> None:
    out = gdal.GetDriverByName("GTiff").Create(
        str(path), features.shape[2], features.shape[1], features.shape[0], gdal.GDT_Float32,
        ["TILED=YES", "COMPRESS=DEFLATE", "PREDICTOR=3"])
    out.SetGeoTransform(reference.GetGeoTransform())
    out.SetProjection(reference.GetProjection())
    for index, name in enumerate(names):
        band = out.GetRasterBand(index + 1)
        band.SetDescription(name)
        band.SetNoDataValue(float("nan"))
        band.WriteArray(features[index])
    del out
    log(f"feature stack: {path}")


if __name__ == "__main__":
    sys.exit(main())
