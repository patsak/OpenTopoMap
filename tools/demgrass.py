"""Shared plumbing for the DEM -> GRASS -> vector tools in this directory.

dem2ridges.py and dem2rivers.py differ only in the hydrology they run: both take
the same DEM (a bbox out of GEDTM, or local tiles), warp it into a metric CRS,
work in a throwaway GRASS project, generalize the result and write it as
GPKG/GeoJSON/Shapefile or as a JOSM reference layer. That common half lives here.
"""

from __future__ import annotations

import contextlib
import itertools
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterator
from xml.etree import ElementTree as ET

from osgeo import gdal, ogr, osr

gdal.UseExceptions()
osr.UseExceptions()

REPO_ROOT = Path(__file__).resolve().parent.parent
DEM_SUFFIXES = (".tif", ".tiff", ".vrt", ".hgt", ".img", ".dem", ".dt1", ".dt2", ".asc")

OGR_DRIVERS = {
    ".gpkg": "GPKG",
    ".geojson": "GeoJSON",
    ".json": "GeoJSON",
    ".shp": "ESRI Shapefile",
    ".sqlite": "SQLite",
}

_TEMP_SEQ = itertools.count(1)


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- DEM


def gedtm_source() -> str:
    """The global 30 m DTM the services already use (also sets GDAL's http tuning)."""
    sys.path.insert(0, str(REPO_ROOT / "www"))
    try:
        from otmlib import dem as otm_dem
    except ImportError as exc:  # numpy/otmlib missing -> say what to install
        raise SystemExit(
            f"cannot import otmlib.dem ({exc}); install its deps (numpy, gdal) "
            f"or pass --dem with a local DEM"
        ) from exc
    return otm_dem.GEDTM_VSI_URL


def local_source(path: Path, work_dir: Path) -> str:
    """A single raster, or a VRT mosaic of every raster in a directory."""
    if path.is_file():
        return str(path)
    if not path.is_dir():
        raise SystemExit(f"--dem {path} is neither a file nor a directory")
    files = sorted(
        str(p) for p in path.rglob("*") if p.suffix.lower() in DEM_SUFFIXES and p.is_file()
    )
    if not files:
        raise SystemExit(f"no DEM tiles ({', '.join(DEM_SUFFIXES)}) under {path}")
    vrt_path = work_dir / "source.vrt"
    log(f"DEM source: {len(files)} tiles from {path} -> {vrt_path.name}")
    vrt = gdal.BuildVRT(str(vrt_path), files)
    if vrt is None:
        raise SystemExit(f"gdal.BuildVRT failed for {path}")
    vrt.FlushCache()
    del vrt
    return str(vrt_path)


def source_center_lonlat(source: str) -> tuple[float, float]:
    ds = gdal.Open(source)
    if ds is None:
        raise SystemExit(f"cannot open DEM source {source}")
    gt = ds.GetGeoTransform()
    x = gt[0] + gt[1] * ds.RasterXSize / 2 + gt[2] * ds.RasterYSize / 2
    y = gt[3] + gt[4] * ds.RasterXSize / 2 + gt[5] * ds.RasterYSize / 2
    wkt = ds.GetProjection()
    del ds
    if not wkt:  # no CRS on the DEM: assume it is already lon/lat
        return x, y
    srs = osr.SpatialReference(wkt=wkt)
    wgs84 = osr.SpatialReference()
    wgs84.ImportFromEPSG(4326)
    wgs84.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    if srs.IsSame(wgs84):
        return x, y
    lon, lat, _ = osr.CoordinateTransformation(srs, wgs84).TransformPoint(x, y)
    return lon, lat


def utm_epsg(lon: float, lat: float) -> int:
    zone = int(math.floor((lon + 180.0) / 6.0)) % 60 + 1
    return (32600 if lat >= 0 else 32700) + zone


def prepare_dem(
    source: str,
    bbox: tuple[float, float, float, float] | None,
    crs: str | None,
    resolution: float | None,
    out_tif: Path,
) -> Path:
    """Crop the source to the bbox and warp it into the working CRS."""
    kwargs: dict = {
        "format": "GTiff",
        "resampleAlg": gdal.GRA_Bilinear,
        "multithread": True,
        "creationOptions": ["COMPRESS=DEFLATE", "TILED=YES", "BIGTIFF=IF_SAFER"],
    }
    if bbox is not None:
        west, south, east, north = bbox
        kwargs["outputBounds"] = [west, south, east, north]
        kwargs["outputBoundsSRS"] = "EPSG:4326"
    if crs is not None:
        kwargs["dstSRS"] = crs
    if resolution is not None:
        kwargs["xRes"] = resolution
        kwargs["yRes"] = resolution
    log(f"DEM warp -> {out_tif.name} (crs={crs or 'source'}, res={resolution or 'auto'})")
    ds = gdal.Warp(str(out_tif), source, options=gdal.WarpOptions(**kwargs))
    if ds is None:
        raise SystemExit("gdal.Warp failed while preparing the DEM")
    gt = ds.GetGeoTransform()
    log(f"DEM ready: {ds.RasterXSize}x{ds.RasterYSize} px, cell {abs(gt[1]):.2f}x{abs(gt[5]):.2f}")
    ds.FlushCache()
    del ds
    return out_tif


def dem_for_args(args, bbox, work_dir: Path) -> Path:
    """The whole DEM half of a run: pick the source, pick the CRS, warp."""
    source = local_source(args.dem, work_dir) if args.dem else gedtm_source()
    if args.dem is None:
        log(f"DEM source: GEDTM 30 m, bbox {bbox}")

    if args.crs == "source":
        crs = None
    elif args.crs == "auto":
        lon, lat = ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2) if bbox \
            else source_center_lonlat(source)
        crs = f"EPSG:{utm_epsg(lon, lat)}"
        log(f"working CRS: {crs} (UTM for {lon:.3f},{lat:.3f})")
    else:
        crs = args.crs

    return prepare_dem(source, bbox, crs, args.res, work_dir / "dem.tif")


# ------------------------------------------------------------------------- GRASS


def grass_executable() -> str:
    env = os.environ.get("GRASS_BIN", "").strip()
    if env:
        return env
    for name in ("grass", "grass85", "grass84", "grass83"):
        found = shutil.which(name)
        if found:
            return found
    for app in sorted(Path("/Applications").glob("GRASS-*.app"), reverse=True):
        candidate = app / "Contents/Resources/bin/grass"
        if candidate.is_file():
            return str(candidate)
    raise SystemExit(
        "GRASS not found: install it (brew install --cask grass or grass-gis) "
        "or point GRASS_BIN at the grass launcher"
    )


def import_grass_script(grass_bin: str | None = None):
    """Put GRASS' own python package on sys.path and import grass.script."""
    grass_bin = grass_bin or grass_executable()
    try:
        python_path = subprocess.run(
            [grass_bin, "--config", "python_path"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"cannot query {grass_bin} --config python_path: {exc}") from exc
    if python_path not in sys.path:
        sys.path.append(python_path)
    import grass.script as gs  # noqa: PLC0415  (only importable once the path is set)

    return gs


@contextlib.contextmanager
def dem_project(gs, dem_tif: Path, work_dir: Path, name: str) -> Iterator[dict]:
    """A throwaway GRASS project holding the DEM as raster ``dem``.

    Yields the region so the caller can turn cell counts into square kilometres.
    """
    grassdata = work_dir / "grassdata"
    grassdata.mkdir(parents=True, exist_ok=True)
    project = grassdata / name
    if project.exists():
        shutil.rmtree(project)
    gs.create_project(str(project), filename=str(dem_tif))
    with gs.setup.init(str(project)):
        gs.run_command("r.in.gdal", input=str(dem_tif), output="dem", overwrite=True)
        gs.run_command("g.region", raster="dem")
        yield gs.region()


def flow_accumulation(gs, elevation: str, output: str, args) -> None:
    """r.watershed, plus the log line that makes the thresholds tunable."""
    # -a keeps accumulation positive at the region edge, where part of the
    # catchment lies outside and the value would otherwise come out negative.
    flags = "a"
    if args.sfd:
        flags += "s"
    if args.swap_memory:
        flags += "m"
    log(f"r.watershed (flags -{flags}, convergence {args.convergence}, {args.memory} MB)")
    gs.run_command(
        "r.watershed",
        elevation=elevation,
        accumulation=output,
        convergence=args.convergence,
        memory=args.memory,
        flags=flags,
        overwrite=True,
    )


def accumulation_max(gs, accumulation: str) -> float:
    return float(gs.parse_command("r.univar", map=accumulation, flags="g")["max"])


def lines_only(gs, vector: str) -> str:
    """r.stream.extract puts the outlet points in the same map; everything
    downstream - statistics, generalization, export - is about lines."""
    output = f"{vector}_lines"
    gs.run_command(
        "v.extract", input=vector, output=output,
        type="line", where="cat > 0", overwrite=True,
    )
    return output


def vertex_count(gs, vector: str) -> int:
    name = f"vertices_tmp_{next(_TEMP_SEQ)}"
    gs.run_command("v.to.points", input=vector, output=name, use="vertex", flags="t")
    return gs.vector_info_topo(name)["points"]


def resolve_simplify(value: str, res: float) -> float:
    """--simplify in CRS units; 'auto' is 1.5 cells, which is where the vertex
    count collapses while every kept vertex still sits on a traced cell."""
    if value == "auto":
        return 1.5 * res
    try:
        threshold = float(value)
    except ValueError:
        raise SystemExit(f"--simplify wants a number or 'auto', not {value!r}") from None
    if threshold < 0:
        raise SystemExit("--simplify cannot be negative")
    return threshold


def generalize(gs, vector: str, simplify: float) -> str:
    """Douglas-Peucker only ever drops vertices, so what is left still lies on
    the cells the raster traced; smoothing methods round the staircase but walk
    the line off the feature, which is worse than a slightly angular line."""
    if simplify <= 0:
        return vector
    before = vertex_count(gs, vector)
    output = f"{vector}_gen"
    gs.run_command(
        "v.generalize", input=vector, output=output,
        method="douglas", threshold=simplify, overwrite=True,
    )
    log(f"v.generalize douglas {simplify:.1f}: {before} -> {vertex_count(gs, output)} vertices")
    return output


def export_lines(gs, vector: str, path: Path) -> None:
    path.unlink(missing_ok=True)
    gs.run_command(
        "v.out.ogr", input=vector, output=str(path),
        format="GPKG", type="line", overwrite=True,
    )


# ------------------------------------------------------------------------ output


def reproject(source: Path, output: Path, crs: str, driver: str, layer: str | None = None) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    options: dict = {"format": driver, "dstSRS": crs, "reproject": True}
    if layer:
        options["layerName"] = layer
    ds = gdal.VectorTranslate(str(output), str(source),
                              options=gdal.VectorTranslateOptions(**options))
    if ds is None:
        raise SystemExit(f"gdal.VectorTranslate failed writing {output}")
    del ds


def write_osm(
    exported: Path,
    output: Path,
    work_dir: Path,
    *,
    generator: str,
    tags_for: Callable[[ogr.Feature], dict[str, str]],
    precision: int = 7,
) -> None:
    """Write a JOSM reference layer: WGS84, negative ids, upload blocked.

    upload="never" is JOSM's own marker for data that must not go up; the layer
    opens as an ordinary editable layer to trace over, but the upload button
    stays off. DEM-derived lines are a drawing aid, not survey data, and
    uploading them wholesale would be an unreviewed import.
    """
    wgs84 = work_dir / f"{output.stem}_4326.gpkg"
    reproject(exported, wgs84, "EPSG:4326", "GPKG")

    osm = ET.Element("osm", version="0.6", generator=generator, upload="never")
    nodes: dict[tuple[str, str], int] = {}
    ways = 0
    tag_counts: dict[str, int] = {}
    source = ogr.Open(str(wgs84))
    for feature in source.GetLayer(0):
        geometry = feature.GetGeometryRef()
        if geometry is None or geometry.GetPointCount() < 2:
            continue
        refs: list[int] = []
        for index in range(geometry.GetPointCount()):
            lon, lat = geometry.GetPoint_2D(index)
            # Segments meet at junctions: rounding to the written precision is
            # what makes those shared nodes one node, so the network stays connected.
            key = (f"{lat:.{precision}f}", f"{lon:.{precision}f}")
            node_id = nodes.get(key)
            if node_id is None:
                node_id = -(len(nodes) + 1)
                nodes[key] = node_id
                ET.SubElement(osm, "node", id=str(node_id), lat=key[0], lon=key[1])
            if not refs or refs[-1] != node_id:
                refs.append(node_id)
        if len(refs) < 2:
            continue
        ways += 1
        way = ET.SubElement(osm, "way", id=str(-ways))
        for ref in refs:
            ET.SubElement(way, "nd", ref=str(ref))
        for key, value in tags_for(feature).items():
            ET.SubElement(way, "tag", k=key, v=value)
            tag_counts[f"{key}={value}"] = tag_counts.get(f"{key}={value}", 0) + 1
    del source

    output.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(osm).write(output, encoding="UTF-8", xml_declaration=True)
    interesting = sorted(
        (t for t in tag_counts.items() if not t[0].startswith("strahler=")),
        key=lambda item: -item[1],
    )
    log(f"{ways} ways, {len(nodes)} nodes, tagged "
        + ", ".join(f"{tag} x{count}" for tag, count in interesting))


def validate_output(output: Path, *, osm: bool) -> None:
    """Reject an extension nothing can write - before a run, not after it."""
    suffix = output.suffix.lower()
    if suffix in OGR_DRIVERS or (suffix == ".osm" and osm):
        return
    supported = ", ".join(sorted(OGR_DRIVERS))
    if suffix == ".osm":
        raise SystemExit(
            f"{Path(sys.argv[0]).name} has no OSM tagging yet; write one of {supported}"
        )
    raise SystemExit(
        f"unsupported output extension {output.suffix!r}; use "
        + (f".osm or one of {supported}" if osm else supported)
    )


def write_output(
    exported: Path,
    output: Path,
    out_crs: str,
    layer: str,
    work_dir: Path,
    *,
    generator: str | None = None,
    tags_for: Callable[[ogr.Feature], dict[str, str]] | None = None,
) -> None:
    """Write the final file. A tool that knows how to tag its lines for OSM
    passes generator/tags_for and gets .osm support; the others do not."""
    osm = tags_for is not None and generator is not None
    validate_output(output, osm=osm)
    if output.suffix.lower() == ".osm":
        if out_crs != "EPSG:4326":
            log(f"--out-crs {out_crs} ignored: OSM data is always WGS84")
        write_osm(exported, output, work_dir, generator=generator, tags_for=tags_for)
        return
    reproject(exported, output, out_crs, OGR_DRIVERS[output.suffix.lower()], layer=layer)


# --------------------------------------------------------------------------- CLI


def add_common_arguments(parser, *, default_output: str, feature: str, osm: bool = False) -> None:
    """The arguments both tools share, worded for whichever feature is traced.

    osm=True when the calling tool knows how to tag its lines for OSM.
    """
    parser.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"),
                        help="area in EPSG:4326; required unless --dem covers exactly it")
    parser.add_argument("--dem", type=Path,
                        help="local DEM file or directory of tiles (default: download GEDTM 30 m)")
    parser.add_argument("-o", "--output", type=Path, default=Path(default_output),
                        help="output vector; driver from the extension"
                             + (", .osm writes a JOSM reference layer" if osm else "")
                             + f" (default: {default_output})")
    parser.add_argument("--crs", default="auto",
                        help="working CRS: 'auto' UTM zone, an EPSG:xxxxx, or 'source' "
                             "to keep the DEM's own (default: auto)")
    parser.add_argument("--res", type=float, metavar="M",
                        help="working cell size in CRS units (default: whatever the warp picks)")
    parser.add_argument("--out-crs", default="EPSG:4326", help="output CRS (default: EPSG:4326)")
    parser.add_argument("--convergence", type=int, default=5,
                        help="r.watershed convergence factor, 1..10 (default: 5)")
    parser.add_argument("--sfd", action="store_true",
                        help=f"r.watershed -s: single flow direction (D8), crisper {feature}")
    parser.add_argument("--swap-memory", action="store_true",
                        help="r.watershed -m: disk swap mode for regions bigger than RAM")
    parser.add_argument("--memory", type=int, default=2000,
                        help="MB per GRASS module (default: 2000)")
    parser.add_argument("--min-cells", type=int, default=0,
                        help=f"drop {feature} shorter than this many cells (default: 0)")
    parser.add_argument("--simplify", default="auto", metavar="D",
                        help="v.generalize douglas tolerance in CRS units; 'auto' is "
                             "1.5 cells, 0 keeps every raster vertex (default: auto)")
    parser.add_argument("--work-dir", type=Path,
                        help="where the intermediates go (default: a temp dir next to the output)")
    parser.add_argument("--keep-work", action="store_true", help="do not delete the work dir")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="let the GRASS modules print their progress")


def normalize_bbox(parser, args) -> tuple[float, float, float, float] | None:
    if args.bbox is None and args.dem is None:
        parser.error("either --bbox or --dem is required")
    if args.bbox is None:
        return None
    west, south, east, north = args.bbox
    if east < west:
        west, east = east, west
    if north < south:
        south, north = north, south
    if west == east or south == north:
        parser.error("--bbox must have non-zero width and height")
    return west, south, east, north


def quiet_grass(verbose: bool) -> None:
    """GRASS modules are chatty by default; the runs log their own steps."""
    os.environ.setdefault("GRASS_MESSAGE_FORMAT", "plain")
    if not verbose:
        os.environ["GRASS_VERBOSE"] = "0"


def work_directory(args) -> Path:
    work_dir = args.work_dir or args.output.resolve().parent / f".{args.output.stem}-work"
    work_dir.mkdir(parents=True, exist_ok=True)
    return work_dir


def cleanup(work_dir: Path, keep: bool) -> None:
    if keep:
        log(f"work dir kept: {work_dir}")
    else:
        shutil.rmtree(work_dir, ignore_errors=True)
