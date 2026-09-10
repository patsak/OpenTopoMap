#!/usr/bin/env python3
"""Forest / scrub / grass polygons for a bbox, out of a ready-made global land cover map.

Somebody has already classified the whole planet at 10 m. This tool does not
repeat the work: it cuts their raster to a bbox, translates their legend into the
handful of classes a topographic map cares about, cleans the speckle and hands
back polygons.

    ./lc2veg.py --bbox 114.0 56.20 114.2 56.35 -o veg.gpkg
    ./lc2veg.py --bbox 114.0 56.20 114.2 56.35 --source worldcover --classes all \\
                -o veg.gpkg --raster veg.tif

    1. fetch   the source tiles over the bbox, as class codes - not as a picture
    2. warp    to the UTM zone of the bbox centre, so a hectare is a hectare
    3. reclass their legend to wood / scrub / grass / moss / bare / water / wetland
    4. clean   majority filter (--smooth), then sieve out what is under --min-area
    5. polygonize, simplify, write GPKG/GeoJSON/Shapefile in EPSG:4326

Two sources, both open, both 10 m (see veglib.py for the legends):

    lcfm        ESA LCFM LCM-10, 2020 - the newer map, and the default
    worldcover  ESA WorldCover v200, 2021

Note on the WMS: titiler.terrascope.be serves these products over WMS too, and a
WMS answers with a rendered PNG - a picture of the classes, not the classes. It
can be polygonized only by reading the colours back out of the palette, which
JPEG artefacts and resampling quietly corrupt. The same server's /bbox/....tif
endpoint hands over the values themselves, so that is what this tool asks for.

What the result is worth: LCFM and WorldCover are honest about their own accuracy
(around 75 % overall, class by class much less), they are a year or four old, and
in the boreal zone both map almost no shrub - dwarf pine ends up inside tree
cover or moss. So this is a drafting aid for natural=wood / natural=scrub /
natural=grassland, not an import candidate. For a current date and this year's
clear-cuts, classify an actual Sentinel-2 scene with s2veg.py, which can take
either of these maps as its training labels.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from osgeo import gdal

import veglib
from veglib import COVER_SOURCES, NAMES, RASTER_SUFFIXES, OGR_DRIVERS, log

gdal.UseExceptions()


def working_grid(bbox, args) -> tuple[str, tuple[float, float, float, float]]:
    """The CRS the classes are cut in, and the bbox expressed in it.

    Metric, because --min-area is hectares and --simplify is metres; UTM of the
    centre unless --crs says otherwise.
    """
    if args.crs == "auto":
        centre = ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)
        crs = f"EPSG:{veglib.utm_epsg(*centre)}"
    else:
        crs = args.crs
    bounds = veglib.reproject_bbox(bbox, veglib.spatial_reference("EPSG:4326"),
                                   veglib.spatial_reference(crs))
    snap = lambda value, up: (np.ceil if up else np.floor)(value / args.res) * args.res
    return crs, (snap(bounds[0], False), snap(bounds[1], False),
                 snap(bounds[2], True), snap(bounds[3], True))


def cut_cover(bbox, args) -> Path:
    """The source tiles, fetched and warped onto the working grid."""
    source = args.source_file or veglib.fetch_cover(args.source, veglib.snap_bbox(bbox),
                                                   args.work, args.chunk, args.timeout)
    crs, bounds = working_grid(bbox, args)
    width = (bounds[2] - bounds[0]) / 1000
    height = (bounds[3] - bounds[1]) / 1000
    log(f"area: {width:.1f} x {height:.1f} km in {crs} at {args.res:g} m")
    warped = args.work / "cover.tif"
    # mode, not nearest: below the source's own 10 m one output cell covers
    # several source cells, and the majority of them is the honest answer.
    gdal.Warp(str(warped), str(source), dstSRS=crs, xRes=args.res, yRes=args.res,
              outputBounds=bounds, resampleAlg="mode",
              creationOptions=["TILED=YES", "COMPRESS=DEFLATE"])
    return warped


def shares(labels: np.ndarray) -> str:
    total = max(int((labels > 0).sum()), 1)
    return ", ".join(f"{NAMES[code]} {100 * int((labels == code).sum()) / total:.1f}%"
                     for code in sorted(NAMES) if code and (labels == code).any())


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Classes: " + ", ".join(name for name in veglib.CLASSES if name != "nodata"))
    parser.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("W", "S", "E", "N"),
                        help="area in EPSG:4326")
    parser.add_argument("-o", "--output", type=Path, default=Path("veg.gpkg"),
                        help="output polygons; driver from the extension, .tif writes only "
                             "the class raster (default: veg.gpkg)")
    parser.add_argument("--raster", type=Path, help="also write the class raster here (GeoTIFF)")
    parser.add_argument("--source", default="lcfm", choices=list(COVER_SOURCES),
                        help="which global map to cut up (default: lcfm)")
    parser.add_argument("--source-file", type=Path, metavar="PATH",
                        help="use an already downloaded copy of that map instead of fetching it")
    parser.add_argument("--classes", default="wood,scrub,grass,moss,bare,water",
                        help="classes to polygonize, or 'all' to add wetland "
                             "(default: wood,scrub,grass,moss,bare,water)")
    parser.add_argument("--res", type=float, default=10.0, metavar="M",
                        help="working cell size in metres; the sources are 10 m (default: 10)")
    parser.add_argument("--crs", default="auto",
                        help="working CRS: 'auto' is the UTM zone of the centre (default: auto)")
    parser.add_argument("--out-crs", default="EPSG:4326", help="output CRS (default: EPSG:4326)")
    parser.add_argument("--smooth", type=int, default=3, metavar="PX",
                        help="majority filter window, 0 keeps the source pixels as they are "
                             "(default: 3)")
    parser.add_argument("--min-area", type=float, default=1.0, metavar="HA",
                        help="drop polygons smaller than this (default: 1.0)")
    parser.add_argument("--simplify", type=float, default=-1.0, metavar="D",
                        help="Douglas-Peucker tolerance in metres, -1 is 1.5 cells (default: -1)")
    parser.add_argument("--chunk", type=int, default=4000, metavar="PX",
                        help="tile server requests are cut into chunks this big (default: 4000)")
    parser.add_argument("--timeout", type=int, default=300, metavar="S",
                        help="seconds to wait for one chunk (default: 300)")
    parser.add_argument("--work-dir", type=Path,
                        help="where the intermediates go (default: a temp dir next to the output)")
    parser.add_argument("--keep-work", action="store_true", help="do not delete the work dir")
    args = parser.parse_args(argv)

    veglib.validate_output(args.output)
    if args.res <= 0:
        parser.error("--res must be positive")
    args.bbox = veglib.normalize_bbox(parser, args.bbox)
    args.wanted = veglib.parse_classes(args.classes)
    args.simplify = 1.5 * args.res if args.simplify < 0 else args.simplify
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.smooth > 1:
        veglib.require("scipy")      # the only dependency, and only for the filter
    args.work = veglib.work_directory(args)

    cover = cut_cover(args.bbox, args)
    reference = gdal.Open(str(cover))
    labels = veglib.reclass(reference.ReadAsArray(), args.source)
    log(f"{args.source}: {shares(labels)}")

    valid = labels > 0
    if args.smooth > 1:
        labels = veglib.majority_of(labels, valid, args.smooth)
    labels = veglib.sieve(labels, reference, int(args.min_area * 10000 / (args.res * args.res)))
    log(f"cleaned: {shares(labels)}")

    raster = args.output if args.output.suffix.lower() in RASTER_SUFFIXES else args.raster
    if raster:
        veglib.write_raster(raster, labels, reference)
    if args.output.suffix.lower() in OGR_DRIVERS:
        raw = veglib.polygonize(labels, reference, args.wanted, args)
        veglib.write_vector(raw, args.output, "landcover", args)

    veglib.cleanup(args.work, args.keep_work)
    return 0


if __name__ == "__main__":
    sys.exit(main())
