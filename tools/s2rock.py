#!/usr/bin/env python3
"""Bare rock and scree for a bbox, split from one summer of Sentinel-1 and -2.

s2veg.py separates forest, dwarf pine and meadow, and for that it needs the
snowmelt window: dwarf pine is still under the snow when the larch is not.
Rock and scree have no such difference. Scree here is any clast surface: the
talus under a wall and a moraine ridge, which is the same cover and can lie
far from any cliff, on gentle ground. The two are the same class because the
surface is the same, so neither the neighbourhood of a cliff nor the slope
can be a feature - a threshold on either would keep the talus and drop the
moraine. A second season, and any clay or iron index, would be a second copy
of a quantity the distance would then count twice. This tool fetches one
high-summer Sentinel-2 composite and the summer Sentinel-1 mosaics, the same
machinery as s2veg.py, and classifies on the surface instead of the colour.

What enters the distance, and why:

    NDVI            both surfaces sit low. Inside the map's bare class it is
                    what still pulls lichen and sparse grass, which the map
                    calls bare, away from the two surfaces we want. It does
                    not separate those two from each other.
    BSI             ((B11 + red) - (NIR + blue)) / ((B11 + red) + (NIR + blue)),
                    the SWIR side of the same gate. The redundancy line says
                    when it has collapsed into NDVI; --drop BSI then.
    VV_db           C-band roughness. Loose debris, talus or a moraine ridge,
                    is clasts far larger than the 5 cm wavelength and scatters
                    brightly, wherever it sits. A rock face is specular: dark
                    unless it happens to face the radar.
    VH_VV           linear ratio, as in s2veg.py. Both bare surfaces depolarize
                    weakly (scree on the Kodar run sat near -9.6 dB, forest
                    near -6), so this keeps a dwarf-pine mat the map called
                    bare from joining either class. A mat and bare rock
                    depolarize alike; the ratio is not the split.
    B04_sd          local standard deviation of red over a 3 x 3 window. A
                    cliff at 10 m is an edge, bright face against shadow, so
                    the window is uneven. Scree, whether under a wall or a
                    moraine out in the valley, is stones smaller than the
                    pixel, so the same window is flat. The near infrared is
                    not taken as well: hillshade drives it the same way, and
                    s2veg.py already measured that this texture is not blind
                    to the relief.
    VV_sd           the same window on VV. A monthly mosaic blends look
                    directions, so the mean backscatter of a facet is a
                    muddle, but the muddle is spatially uneven on a cliff and
                    even on a debris surface. That unevenness is what the
                    mosaic still contains.
    slope           degrees, from the DEM (--dem, default GEDTM 30 m) on the
                    10 m grid. A face is steep and a talus lies near the
                    angle of repose, and on the shaded side, where reflectance
                    and texture both go flat, this is the dimension that does
                    not change with the sun. It is one dimension of seven, not
                    a threshold: a moraine ridge is scree on gentle ground,
                    so the training polygons have to include gentle moraine
                    or kNN will learn that scree is steep. A cliff narrower
                    than the DEM's 30 m comes out less steep than it is.
                    --no-slope leaves it out.

Fetched and written to --features, left out of the distance unless --parents:

    B02 B04 B08/B8A B11    parents of the indices and of B04_sd. In the
                           distance, after standardizing, they split sun from
                           shade, and a shaded cliff and shaded debris are
                           the same shadow.
    VH_db                  the third copy of one scattering, beside VV_db and
                           VH_VV.

Left out on purpose:

    a second season, NDVI_spring    nothing about either surface changes with
                                    it
    B12, mineral indices            debris and the bedrock it came from share
                                    a lithology; on the Kodar run B12 was
                                    already B11 (r=+0.94)
    distance to a cliff             a moraine ridge is scree and can lie far
                                    from any wall. Neighbourhood would keep
                                    the talus and drop the ridge
    coherence                       both surfaces are stable, and it needs SLC
                                    pairs these mosaics do not carry
    --flatten-sar                   s2veg.py flattens VV so vegetation is not
                                    sorted by aspect. Here the aspect
                                    dependence of a facet is the cliff, and
                                    removing it would delete the signal. The
                                    ratio barely moves under that correction
                                    anyway
    elevation                       a column in the cluster table, never a
                                    feature, for the reason s2veg.py gives

Shaded slopes: the scene classification marks a slope facing away from the
sun as "dark area" (SCL 2) and s2veg.py drops it with the clouds. On a clear
August day over Bezengi that was 86-95 % of the bare ground at cos(i) under
0.25, so the shaded side was not misclassified, it was missing. s2rock keeps
SCL 2; --mask-shadow drops it again for a cloudy window, where dark area can
also be cloud shadow the mask did not catch.

The global maps have one class, bare, for both, so the default is k-means
inside that class and the vote names every cluster bare. The split is --name,
read off the table: scree is the low B04_sd and VV_sd with high VV_db, rock is
the high deviations. A moraine ridge far from any cliff falls on the scree
side of that, which is what the class means. k=4 leaves room for sun, shade
and the vegetated leftover to fall out as their own clusters. Slope is in the
table as a feature and is not part of the reading.

Only bare_rock is taken from the classifier by default (--scree map). Scree is
then whatever the global map in --labels calls bare and the classifier did not
call bare_rock: the map is good at the outline of the bare ground and has no
opinion on bedrock, the classifier the other way round. Scree the classifier
finds outside the map's bare is dropped, so a moraine the map called moss is
lost; --scree model keeps the classifier's own scree, as before.

    ./s2rock.py --bbox 114.0 56.20 114.2 56.35 --year 2025 --dry-run
    ./s2rock.py --bbox 114.0 56.20 114.2 56.35 --year 2025 --features stack.tif \\
                --cluster-raster ids.tif -o rock.gpkg
    ./s2rock.py --bbox 114.0 56.20 114.2 56.35 --features-in stack.tif \\
                --name 2=scree,3=bare_rock -o rock.gpkg
    ./s2rock.py --bbox 114.0 56.20 114.2 56.35 --labels lcfm,train.gpkg \\
                --method knn -o rock.gpkg

Resolution stays 10 m and the window stays 3 pixels. At 20 m that window is
60-100 m and swallows a cliff, which is only a few pixels wide, into whatever
debris or vegetation is beside it. A moraine ridge is wide enough either way;
the cliff is the thing that sets the scale.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import numpy as np
from osgeo import gdal, ogr

import s2veg
import veglib
from veglib import CLASSES, COVER_SOURCES, NAMES, log

# Raw reflectance and the spare polarization. They are computed, because the
# indices and the texture are made from them and because --features is there
# to be looked at, and they stay out of the distance: k-means and kNN both
# measure plain distance, so sun versus shade would otherwise outweigh the
# texture the split actually is. VH_db next to VV_db and VH_VV is one
# scattering counted a second time.
PARENTS = {"B02", "B03", "B04", "B08", "B8A", "B11", "B12", "VH_db"}

DEM_SUFFIXES = (".tif", ".tiff", ".vrt", ".hgt", ".img", ".dem", ".dt1", ".dt2", ".asc")

# Below this, a cluster is still a candidate for rock or scree. Dwarf pine on
# the Kodar run sat at NDVI 0.52 and the bare clusters at 0.26-0.34, so 0.45
# keeps both surfaces and sets the vegetated leftover aside.
BARE_NDVI = 0.45


def bsi(names: list[str], features: np.ndarray) -> np.ndarray | None:
    """Bare soil index from the four bands it is defined on, or None if one is
    missing. A ratio, so the reflectance scale cancels."""
    nir = next((band for band in s2veg.NIR_BANDS if band in names), None)
    if nir is None or not {"B02", "B04", "B11"} <= set(names):
        return None

    def layer(name: str) -> np.ndarray:
        return features[names.index(name)]

    swir, red, near, blue = layer("B11"), layer("B04"), layer(nir), layer("B02")
    with np.errstate(invalid="ignore", divide="ignore"):
        return ((swir + red - near - blue) / (swir + red + near + blue)).astype(np.float32)


def add_bsi(names: list[str], features: np.ndarray) -> tuple[list[str], np.ndarray]:
    if "BSI" in names:
        return names, features
    index = bsi(names, features)
    if index is None:
        log("BSI needs B02, B04, B11 and a near infrared; the stack has "
            + ", ".join(names) + ", so it is skipped")
        return names, features
    log("BSI from B11, red, near infrared, B02")
    return names + ["BSI"], np.concatenate([features, index[None]])


def add_surface_texture(names: list[str], features: np.ndarray,
                        window: int) -> tuple[list[str], np.ndarray]:
    """Local deviation of red and of VV. See the module docstring for why these
    two and not NDVI, and why the near infrared is not a third."""
    if window < 2:
        return names, features
    layers, added = [], []
    for source, name in (("B04", "B04_sd"), ("VV_db", "VV_sd")):
        if name in names or source not in names:
            continue
        added.append(name)
        layers.append(s2veg.local_sd(features[names.index(source)], window))
    if not layers:
        have = [name for _, name in (("B04", "B04_sd"), ("VV_db", "VV_sd")) if name in names]
        if not have:
            log("--texture: neither B04 nor VV_db is in the stack (" + ", ".join(names) + ")")
        return names, features
    missing = [name for source, name in (("B04", "B04_sd"), ("VV_db", "VV_sd"))
               if name not in added and name not in names]
    if missing:
        log(f"! no {', '.join(missing)}: the stack has no "
            + ("VV" if "VV_sd" in missing else "red") + " to take it on")
    log(f"texture over a {window}x{window} window: {', '.join(added)}")
    return names + added, np.concatenate([features, np.stack(layers)])


def dem_source(args) -> str:
    """--dem as one GDAL source: a raster, or a VRT over every DEM tile in a
    directory. Without --dem, the global GEDTM 30 m the DEM tools use."""
    if args.dem is None:
        sys.path.insert(0, str(veglib.TOOLS_DIR.parent / "www"))
        from otmlib import dem as otm_dem
        return otm_dem.GEDTM_VSI_URL
    path = args.dem.expanduser()
    if path.is_file():
        return str(path)
    if not path.is_dir():
        raise SystemExit(f"--dem {path} is neither a file nor a directory")
    tiles = sorted(str(p) for p in path.rglob("*")
                   if p.is_file() and p.suffix.lower() in DEM_SUFFIXES)
    if not tiles:
        raise SystemExit(f"--dem: no DEM tiles ({', '.join(DEM_SUFFIXES)}) under {path}")
    vrt = args.work / "dem.vrt"
    gdal.BuildVRT(str(vrt), tiles)
    log(f"DEM: {len(tiles)} tiles under {path}")
    return str(vrt)


def dem_slope(grid: s2veg.Grid, args) -> np.ndarray:
    """Slope in degrees on the working grid, NaN where the DEM has no value.

    The DEM is warped cubic rather than bilinear: the gradient of a bilinear
    surface is constant across each source cell, and at 10 m on a 30 m DEM that
    prints the DEM's own grid into the feature.
    """
    source = dem_source(args)
    log(f"slope from {'GEDTM 30 m' if args.dem is None else args.dem}")
    height = s2veg.warp(source, grid, "cubic")
    if height is None:
        raise SystemExit("the DEM has nothing over the bbox - pass --dem with tiles that "
                         "cover it, or --no-slope")
    height = height.astype(np.float32)
    height[height < -1000] = np.nan
    finite = np.isfinite(height)
    if not finite.any():
        raise SystemExit("the DEM is void over the whole bbox - pass another --dem or --no-slope")
    filled = np.where(finite, height, np.nanmedian(height))
    slope = np.degrees(s2veg.terrain(grid, filled)[0]).astype(np.float32)
    slope[~finite] = np.nan
    log(f"  slope median {np.nanmedian(slope):.0f}°, p90 {np.nanpercentile(slope, 90):.0f}°")
    return slope


def exclude(names: list[str], features: np.ndarray,
            args) -> tuple[list[str], np.ndarray]:
    """Take the parents, and anything --drop names, out of the distance."""
    aside = [name for name in names if name in PARENTS] if not args.parents else []
    unknown = [name for name in args.dropped if name not in names]
    if unknown:
        raise SystemExit(f"--drop: {', '.join(unknown)} is not in the stack, which has "
                         + ", ".join(names))
    dropped = list(dict.fromkeys([*aside, *args.dropped]))
    if not dropped:
        log(f"classifying on {', '.join(names)}")
        return names, features
    keep = [index for index, name in enumerate(names) if name not in dropped]
    if not keep:
        raise SystemExit("every feature was dropped; nothing left to classify on")
    kept = [names[index] for index in keep]
    log(f"left out of the distance: {', '.join(dropped)}")
    log(f"classifying on {', '.join(kept)}")
    return kept, features[keep]


def drawn_classes(path: str) -> set[str]:
    """Class names in a hand-drawn label file, from 'class' or from 'code'."""
    source = ogr.Open(path)
    if source is None:
        raise SystemExit(f"--labels: cannot open {path}")
    layer = source.GetLayer(0)
    definition = layer.GetLayerDefn()
    fields = {definition.GetFieldDefn(i).GetName().lower(): definition.GetFieldDefn(i).GetName()
              for i in range(definition.GetFieldCount())}
    if "class" in fields:
        return {str(feature.GetField(fields["class"])) for feature in layer}
    if "code" in fields:
        codes = {int(feature.GetField(fields["code"])) for feature in layer}
        return {NAMES[code] for code in codes if code in NAMES}
    raise SystemExit(f"--labels: {path} has neither a 'code' nor a 'class' field")


def require_drawn_split(args) -> None:
    """kNN cannot learn a split the map does not contain. Fail before the download."""
    if args.method != "knn" or args.model:
        return
    found: set[str] = set()
    for source in args.labels:
        if source not in COVER_SOURCES:
            found |= drawn_classes(source)
    missing = [name for name in ("bare_rock", "scree") if name not in found]
    if missing:
        raise SystemExit(
            "kNN needs hand-drawn polygons of " + " and ".join(missing) + ".\n"
            "    The global map has a single bare class for both, so it cannot teach\n"
            "    the split. Draw the two and pass --labels lcfm,train.gpkg --method knn,\n"
            "    or leave --method kmeans and name the clusters from the table.\n"
            "    nothing has been downloaded")


def training_domain(labels: np.ndarray, args) -> np.ndarray:
    """Where a kNN prediction is allowed to land.

    Inside the map's bare class, plus any rock or scree already drawn - those
    pixels are no longer labelled bare, and dropping them would punch holes
    exactly where the training polygons are. Captured before bare is silenced,
    because silencing it is what stops the union being taught as its own class.
    """
    if args.within:
        domain = np.isin(labels, [CLASSES[name] for name in args.within])
    else:
        domain = np.ones(labels.shape, dtype=bool)
    return domain | np.isin(labels, [CLASSES["bare_rock"], CLASSES["scree"]])


def silence_bare(labels: np.ndarray) -> np.ndarray:
    """Drop the map's bare class from the sample.

    bare is rock and scree together. Left in, a few hundred drawn polygons are
    drowned by it, which is the same failure s2veg.py describes for a dozen
    hand-drawn wood polygons merged into a map that calls 60% of the bbox trees.
    Wood, moss, grass and water stay, so a pixel that is none of the two has
    somewhere else to go.
    """
    count = int((labels == CLASSES["bare"]).sum())
    if not count:
        return labels
    labels = labels.copy()
    labels[labels == CLASSES["bare"]] = 0
    log(f"not teaching bare ({count} pixels): it is rock and scree together, and a "
        "class that large would drown the polygons drawn to split it")
    return labels


def map_cover(grid: s2veg.Grid, args) -> np.ndarray | None:
    """The global maps in --labels on their own, without the polygons drawn on
    top of them; None when --labels has no global map."""
    maps = [source for source in args.labels if source in COVER_SOURCES]
    if not maps:
        return None
    every, args.labels = args.labels, maps
    try:
        return s2veg.load_labels(grid, args)
    finally:
        args.labels = every


def scree_from_map(predicted: np.ndarray, cover: np.ndarray, args) -> np.ndarray:
    """--scree map: bare_rock as classified, scree wherever the map says bare
    and the classifier did not say bare_rock.

    The classifier is trusted with the one thing the map cannot tell, which part
    of its bare class is bedrock, and the map keeps its own outline of the bare
    ground. Scree the classifier finds outside that outline is dropped, and
    whatever else it put inside the outline becomes scree.
    """
    if args.scree != "map":
        return predicted
    rock = predicted == CLASSES["bare_rock"]
    if not rock.any():
        log("! nothing is bare_rock, so all of the map's bare becomes scree"
            + (" - name a rock cluster with --name" if args.method == "kmeans" and not args.model
               else ""))
    out = np.where(predicted == CLASSES["scree"], 0, predicted)
    out = np.where((cover == CLASSES["bare"]) & ~rock, CLASSES["scree"], out).astype(np.uint8)
    maps = ", ".join(source for source in args.labels if source in COVER_SOURCES)
    log(f"scree is the bare of {maps} that is not bare_rock: {s2veg.shares(out)}")
    return out


def read_split(names: list[str], features: np.ndarray, ids: np.ndarray,
               clusters: int) -> tuple[int, int] | None:
    """(scree cluster, rock cluster) from the texture, or None when it does not
    separate the low-NDVI clusters.

    Ranks, not the raw deviations: B04_sd is a reflectance and VV_sd is decibels,
    and averaging the two would hand the decision to whichever has the larger
    number. A cluster at NDVI 0.45 or above is the vegetated leftover and is
    not a candidate.
    """
    texture = [name for name in ("B04_sd", "VV_sd") if name in names]
    if "B04_sd" not in texture:
        return None
    present = [cluster for cluster in range(1, clusters + 1) if (ids == cluster).any()]
    flat = features.reshape(len(names), -1)
    medians = {}
    for cluster in present:
        mask = (ids == cluster).ravel()
        medians[cluster] = {name: float(np.nanmedian(flat[names.index(name), mask]))
                            for name in names}
    if "NDVI" in names:
        candidates = [cluster for cluster in present if medians[cluster]["NDVI"] < BARE_NDVI]
    else:
        candidates = present
    if len(candidates) < 2:
        return None

    def separates(name: str) -> bool:
        values = [medians[cluster][name] for cluster in candidates]
        low, high = min(values), max(values)
        return high > 1.4 * max(low, 1e-6)

    # Only a texture that actually separates gets a vote. Ranking a flat
    # VV_sd as well would give its noise the same weight as a real gap in B04_sd.
    live = [name for name in texture if separates(name)]
    if not live:
        return None
    score = {cluster: 0 for cluster in candidates}
    for name in live:
        order = sorted(candidates, key=lambda cluster: medians[cluster][name])
        for rank, cluster in enumerate(order):
            score[cluster] += rank
    scree = min(candidates, key=lambda cluster: (score[cluster], cluster))
    rock = max(candidates, key=lambda cluster: (score[cluster], -cluster))
    if scree == rock or score[rock] == score[scree]:
        return None
    return scree, rock


def suggest_names(names: list[str], features: np.ndarray, ids: np.ndarray, args) -> None:
    """Say which cluster reads as which surface. The table already has the
    numbers; this is the reading of them, and it is not applied."""
    if args.named:
        return
    if "B04_sd" not in names:
        log("no B04_sd in the stack, so there is no texture to read a cliff off")
        return
    split = read_split(names, features, ids, args.clusters)
    if split is None:
        log("texture does not separate the low-NDVI clusters on this bbox. "
            "Slope will not finish it: a moraine ridge is scree too, and it can be "
            "gentle and far from any cliff. Widen the domain with --within bare,moss "
            "where the map has called the debris moss, and check the cluster raster.")
        return
    scree, rock = split
    log(f"reading, not applied: cluster {scree} is the even one (scree), "
        f"cluster {rock} is the broken one (bare_rock).")
    log(f"  --name {scree}=scree,{rock}=bare_rock")
    log("  confirm on the cluster raster first. Scree is that even, bright-VV "
        "surface wherever it sits: a talus and a moraine ridge are the same class, "
        "and the ridge may lie far from any cliff. Slope does not name it. "
        "A high deviation along the edge of the bare mask is the boundary with "
        "vegetation, and sun and shade split into their own clusters first.")


def parse_args(argv=None):
    today = dt.date.today()
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Classes: " + ", ".join(name for name in CLASSES if name != "nodata"))
    parser.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("W", "S", "E", "N"),
                        help="area in EPSG:4326 - everything is fetched and classified inside it")
    parser.add_argument("-o", "--output", type=Path, default=Path("rock.gpkg"),
                        help="output polygons; driver from the extension, .tif writes only "
                             "the class raster (default: rock.gpkg)")
    parser.add_argument("--raster", type=Path, help="also write the class raster here (GeoTIFF)")
    parser.add_argument("--features", type=Path, metavar="PATH",
                        help="also write the feature stack, one band per feature, including the "
                             "raw bands that are left out of the distance")
    parser.add_argument("--features-in", type=Path, metavar="PATH",
                        help="read a stack a previous run wrote instead of fetching again. "
                             "The cheap way to iterate on --name, and it needs no credentials")
    parser.add_argument("--year", type=int, default=today.year - (today.month < 10),
                        help=f"season to classify (default: {today.year - (today.month < 10)})")
    parser.add_argument("--summer", default="06-15:09-15", metavar="MM-DD:MM-DD",
                        help="the one optical window (default: 06-15:09-15). There is no "
                             "spring window: rock and scree do not change with the season")
    parser.add_argument("--sar", default="auto", choices=("auto", "off"),
                        help="use the Sentinel-1 mosaics, or do without them (default: auto)")
    parser.add_argument("--sar-months", default="06-01:09-30", metavar="MM-DD:MM-DD",
                        help="window the monthly S1 mosaics are taken from (default: 06-01:09-30)")
    parser.add_argument("--max-cloud", type=float, default=40.0, metavar="PCT",
                        help="skip scenes cloudier than this (default: 40)")
    parser.add_argument("--bands", default="B02,B04,B08,B11",
                        help="reflectance to fetch. B02 and B11 are there for BSI, B04 for the "
                             "texture, one near infrared for NDVI. B12 is not fetched: it "
                             "repeats B11. At --res 20 or coarser the default swaps B08 for "
                             "B8A (default: B02,B04,B08,B11)")
    parser.add_argument("--parents", action="store_true",
                        help="classify on the raw bands and on VH_db as well. Left out by "
                             "default, because after standardizing they split sun from shade "
                             "and count one scattering twice")
    parser.add_argument("--workers", type=int, default=6, metavar="N",
                        help="scenes read at once (default: 6)")
    parser.add_argument("--scenes", type=int, default=4, metavar="N",
                        help="clear looks wanted at every pixel (default: 4)")
    parser.add_argument("--labels", default="lcfm",
                        help="what the map calls the ground, so clustering can be limited to "
                             "its bare class: 'lcfm', 'worldcover', or a vector with a 'code' "
                             "or 'class' field. For kNN, 'lcfm,train.gpkg' with bare_rock and "
                             "scree polygons drawn on it (default: lcfm)")
    parser.add_argument("--samples", type=int, default=3000, metavar="N",
                        help="training pixels drawn per class (default: 3000)")
    parser.add_argument("--min-samples", type=int, default=50, metavar="N",
                        help="a class with fewer labelled pixels than this is not taught "
                             "(default: 50)")
    parser.add_argument("--label-erode", type=int, default=2, metavar="PX",
                        help="shrink each labelled blob by this much before sampling "
                             "(default: 2)")
    parser.add_argument("--method", default="kmeans", choices=("knn", "kmeans"),
                        help="kmeans splits the bare class on the features and waits for "
                             "--name; knn learns bare_rock and scree from hand-drawn polygons "
                             "(default: kmeans)")
    parser.add_argument("--neighbors", type=int, default=7, metavar="K",
                        help="k of the kNN (default: 7)")
    parser.add_argument("--clusters", type=int, default=4, metavar="N",
                        help="clusters k-means splits the bare class into. 4 leaves room for "
                             "sun, shade and a vegetated leftover beside the two surfaces "
                             "(default: 4)")
    parser.add_argument("--name", default="", metavar="N=CLASS,...",
                        help="name clusters yourself. The map calls every one of them bare, "
                             "so this is the split: --name 2=scree,3=bare_rock")
    parser.add_argument("--cluster-raster", type=Path, metavar="PATH",
                        help="write the raw cluster numbers, to look at next to the table")
    parser.add_argument("--drop", default="", metavar="FEATURE,...",
                        help="further features to leave out of the distance, on top of the "
                             "raw bands and VH_db. They are still written to --features")
    parser.add_argument("--within", default="bare",
                        help="cluster, and for kNN predict, only inside these label classes. "
                             "'off' uses the whole bbox. bare,moss is the right widening where "
                             "the map has called debris moss, including a moraine "
                             "ridge far from any cliff (default: bare)")
    parser.add_argument("--scree", default="map", choices=("map", "model"),
                        help="map: scree is everything the global map in --labels calls bare "
                             "that the classifier did not call bare_rock; model: scree is "
                             "what the classifier calls scree (default: map)")
    parser.add_argument("--replace", default="", metavar="CLASS,...",
                        help="which classes a hand-drawn file takes over from the map entirely")
    parser.add_argument("--save-model", type=Path, metavar="PATH",
                        help="save the trained kNN to apply to another bbox with --model")
    parser.add_argument("--model", type=Path, metavar="PATH",
                        help="classify with a model saved by an earlier run; no labels")
    parser.add_argument("--texture", type=int, default=3, metavar="PX",
                        help="window for B04_sd and VV_sd, in pixels. 3 at 10 m is 30 m, "
                             "wide enough to see a face against its shadow and narrower than "
                             "a scree field (default: 3; 0 or 1 turns it off)")
    parser.add_argument("--dem", type=Path, metavar="PATH",
                        help="DEM for the slope feature: a raster, or a directory of tiles "
                             "(.hgt, .tif, ...) mosaicked as a VRT. Default: GEDTM 30 m, read "
                             "remotely. www/mapsvc/data/dem-cache/hgt is the same data on disk")
    parser.add_argument("--no-slope", dest="slope", action="store_false",
                        help="leave slope out of the features. With it in, gentle moraine has "
                             "to be among the scree polygons, or kNN learns that scree is steep")
    parser.add_argument("--mask-shadow", action="store_true",
                        help="drop the scene classification's dark area (SCL 2) the way "
                             "s2veg.py does. Kept by default, because on a clear day it is the "
                             "shaded slope; worth dropping for a cloudy window")
    parser.add_argument("--no-elevation", dest="elevation", action="store_false",
                        help="skip the elevation column in the cluster table")
    parser.add_argument("--fit-sample", type=int, default=100_000, metavar="N",
                        help="pixels k-means is fitted on (default: 100000)")
    parser.add_argument("--seed", type=int, default=0, help="random seed (default: 0)")
    parser.add_argument("--classes", default="bare_rock,scree",
                        help="classes to polygonize (default: bare_rock,scree)")
    parser.add_argument("--res", type=float, default=10.0, metavar="M",
                        help="working cell size in metres. 10, because a cliff is a few pixels "
                             "wide (default: 10)")
    parser.add_argument("--crs", default="auto",
                        help="working CRS: 'auto' is the UTM zone of the centre (default: auto)")
    parser.add_argument("--out-crs", default="EPSG:4326", help="output CRS (default: EPSG:4326)")
    parser.add_argument("--smooth", type=int, default=3, metavar="PX",
                        help="majority filter window. Smaller than s2veg.py's, so a cliff a "
                             "few pixels wide survives (default: 3; 0 or 1 keeps the raw result)")
    parser.add_argument("--min-area", type=float, default=0.1, metavar="HA",
                        help="drop polygons smaller than this. A rock band narrower than this "
                             "survives in --raster and dies as a polygon (default: 0.1)")
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
    if args.texture < 0:
        parser.error("--texture must be >= 0")
    for name in ("summer", "sar_months"):
        if len(getattr(args, name).split(":")) != 2:
            parser.error(f"--{name.replace('_', '-')} must look like MM-DD:MM-DD")
    args.bbox = veglib.normalize_bbox(parser, args.bbox)
    args.wanted = veglib.parse_classes(args.classes)
    default_bands = ["B02", "B04", "B08", "B11"]
    args.bands = [band.strip().upper() for band in args.bands.split(",") if band.strip()]
    unknown = [band for band in args.bands if band not in s2veg.S2_BANDS]
    if unknown:
        parser.error(f"--bands: unknown {', '.join(unknown)}; known are "
                     + ", ".join(s2veg.S2_BANDS))
    if "B04" not in args.bands or not any(band in args.bands for band in s2veg.NIR_BANDS):
        parser.error("--bands must keep B04 and one of " + " / ".join(s2veg.NIR_BANDS))
    if len(set(args.bands) & set(s2veg.NIR_BANDS)) > 1:
        parser.error("--bands: pick one near infrared, " + " or ".join(s2veg.NIR_BANDS))
    if args.bands == default_bands and args.res >= 20:
        args.bands = ["B02", "B04", "B8A", "B11"]
        log("at 20 m and coarser the near infrared is B8A: B08 is published at 10 m only")
    # feature_stack and the saved model both look at this. There is no spring
    # composite, and the SAR is not flattened: see the module docstring.
    args.spring = "off"
    args.flatten_sar = False
    args.keep_dark = not args.mask_shadow
    # With slope a feature its median is already a row of the table.
    args.report_slope = not args.slope
    args.replaced = veglib.parse_classes(args.replace) if args.replace else None
    args.labels = [source.strip() for source in args.labels.split(",") if source.strip()]
    if not args.labels:
        parser.error("--labels needs at least one source")
    if args.scree == "map" and not any(source in COVER_SOURCES for source in args.labels):
        parser.error("--scree map takes scree from a global map: put lcfm or worldcover "
                     "in --labels, or pass --scree model")
    args.dropped = [name.strip() for name in args.drop.split(",") if name.strip()]
    if args.within == "off":
        args.within = []
    else:
        args.within = veglib.parse_classes(args.within) if args.within else []
    args.named = s2veg.parse_names(args.name)
    unknown = [cluster for cluster in args.named if not 1 <= cluster <= args.clusters]
    if unknown:
        parser.error(f"--name: cluster {unknown} is outside 1..{args.clusters}")
    args.simplify = 1.5 * args.res if args.simplify < 0 else args.simplify
    return args


def build_stack(names: list[str], features: np.ndarray, grid: s2veg.Grid,
                args) -> tuple[list[str], np.ndarray]:
    """BSI, the two textures and slope on top of s2veg's summer stack. Any of
    them may already be there when the stack was read back from --features-in."""
    names, features = add_bsi(names, features)
    if args.texture > 1:
        names, features = add_surface_texture(names, features, args.texture)
    if args.slope and "slope" not in names:
        names, features = names + ["slope"], np.concatenate([features,
                                                             dem_slope(grid, args)[None]])
    elif not args.slope and "slope" in names:
        keep = [index for index, name in enumerate(names) if name != "slope"]
        names, features = [names[index] for index in keep], features[keep]
    return names, features


def main(argv=None) -> int:
    args = parse_args(argv)
    veglib.require("pystac_client", "sklearn", "scipy")
    require_drawn_split(args)

    crs, bounds = veglib.working_grid(args.bbox, args.crs, args.res)
    grid = s2veg.Grid(crs, bounds, args.res)
    args.grid = grid
    log(f"area: {(bounds[2] - bounds[0]) / 1000:.1f} x {(bounds[3] - bounds[1]) / 1000:.1f} km "
        f"in {crs} at {args.res:g} m ({grid.width} x {grid.height} px)")
    log("one summer window only: rock and scree do not change with the season, "
        "so there is no spring composite")
    if args.keep_dark:
        log("shaded slopes kept: SCL dark area is composited as ground (--mask-shadow drops it)")

    if args.dry_run:
        s2veg.list_scenes(s2veg.stac_client(), grid, args)
        return 0

    args.work = veglib.work_directory(args)
    args.cover_cache = {}
    if args.model:
        s2veg.check_model(args.model, args)
    else:
        s2veg.check_labels(grid, args)
    if args.features_in:
        names, features = s2veg.read_feature_stack(args.features_in, grid)
    else:
        s2veg.open_s3()
        names, features = s2veg.feature_stack(s2veg.stac_client(), grid, args)
    names, features = build_stack(names, features, grid, args)
    reference = grid.reference()
    if args.features:
        s2veg.write_features(args.features, names, features, reference)
    names, features = exclude(names, features, args)

    if args.model:
        saved = s2veg.load_model(args.model, names, grid, args)
        usable = np.all(np.isfinite(features), axis=0)
        log(f"pixels with a complete feature vector: {100 * usable.mean():.1f}%")
        s2veg.report_novelty(saved["model"], saved["centre"], saved["spread"], features,
                             usable, saved)
        predicted = s2veg.predict_all(saved["model"], saved["centre"], saved["spread"],
                                      features, usable)
        log(f"classified: {s2veg.shares(predicted)}")
        # The same domain the model was trained in. Without it a forest pixel
        # that happens to sit near a rock sample is called rock, which inside
        # the training run --within never allowed.
        cover = map_cover(grid, args)
        if args.within and cover is not None:
            predicted = np.where(training_domain(cover, args), predicted, 0).astype(np.uint8)
            maps = ", ".join(source for source in args.labels if source in COVER_SOURCES)
            log(f"inside {', '.join(args.within)} of {maps}: {s2veg.shares(predicted)}")
        elif args.within:
            log(f"! --within {','.join(args.within)} needs a global map in --labels "
                "to apply to a saved model; predicting everywhere")
        if cover is not None:
            predicted = scree_from_map(predicted, cover, args)
        return s2veg.finish(predicted, grid, reference, args)

    labels = s2veg.load_labels(grid, args)
    log(f"labels ({', '.join(args.labels)}): {s2veg.shares(labels)}")

    if args.method == "kmeans":
        predicted, ids = s2veg.cluster(names, features, labels, grid, args)
        suggest_names(names, features, ids, args)
        if args.cluster_raster:
            s2veg.write_clusters(args.cluster_raster, ids, reference)
    else:
        domain = training_domain(labels, args)
        taught = {NAMES[int(code)] for code in np.unique(labels) if code}
        if not (taught - {"bare_rock", "scree", "bare"}):
            log("! no other class is taught, so the vote must call every pixel inside "
                "the domain rock or scree. Pass the global map as well: "
                "--labels lcfm,train.gpkg")
        labels = silence_bare(labels)
        predicted = s2veg.classify(names, features, labels, args)
        predicted = np.where(domain, predicted, 0).astype(np.uint8)
    log(f"classified: {s2veg.shares(predicted)}")
    cover = map_cover(grid, args)
    if cover is not None:
        predicted = scree_from_map(predicted, cover, args)
    if not ((predicted == CLASSES["bare_rock"]) | (predicted == CLASSES["scree"])).any():
        log("no pixel is bare_rock or scree yet. The map names every cluster bare "
            "until --name says otherwise.")

    return s2veg.finish(predicted, grid, reference, args)


if __name__ == "__main__":
    sys.exit(main())
