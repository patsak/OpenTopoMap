#!/usr/bin/env python3
"""Draw ridge lines (khrebtovka) from a DEM with GRASS hydrology.

A ridge is a watershed of the inverted relief: flip the DEM upside down and the
crests turn into valleys, so the ordinary stream-extraction chain draws them.

    1. DEM for the bbox: GEDTM 30 m cropped through /vsicurl (the same source
       www/otmlib/dem.py feeds the tile and Garmin builds), or --dem <file|dir>
    2. reproject to a metric CRS (auto UTM) so cells are square metres
    3. r.mapcalc      inverted = -1.0 * dem
    4. r.watershed    flow accumulation on the inverted DEM
    5. r.mapcalc      seeds = if(accumulation > threshold, 1, 0)
    6. r.stream.extract on the inverted DEM, with those seeds as accumulation
    7. drop the lines that run over smooth ground, keeping sharp crests
    8. vectorize, generalize and write GPKG/GeoJSON/Shapefile/.osm in EPSG:4326

    ./dem2ridges.py --bbox 42.3 43.0 43.5 43.5 -o ridges.gpkg
    ./dem2ridges.py --dem ~/dem/caucasus --bbox 42.3 43.0 43.5 43.5 -o r.geojson
    ./dem2ridges.py --bbox 42.3 43.0 43.5 43.5 -o ridges.osm   # JOSM underlay

The accumulation threshold is a cell count, so it only means the same thing at
the same resolution: 18000 cells of 30 m is a 16 km2 catchment, i.e. main
crests only. The run prints the accumulation range and the km2 equivalent, and
--threshold tunes it (lower = denser, more spurs).

Hydrology alone also finds the watershed of a smooth dome (the Elbrus cone is
the classic case): a divide, but no crest to draw. Step 7 measures how far each
line stands above its surroundings - the median of dem minus a 500 m moving
average along the line - and keeps only the lines above --sharpness metres.

The result is a cartographic aid for drawing natural=ridge by hand, not an
import candidate: DEM-derived lines are not survey data.

Rivers are the same chain without the inversion: see dem2rivers.py.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import demgrass
from demgrass import log

# What a traced ridge is tagged with in OSM: arete is the rocky, glacier-carved
# kind, and telling the two apart is a job for the mapper, not for the DEM.
OSM_TAGS = {"natural": "ridge"}


def extract_ridges(gs, dem_tif: Path, work_dir: Path, args) -> tuple[str, str] | None:
    """Run the GRASS chain in a throwaway project; return (exported gpkg, map)."""
    with demgrass.dem_project(gs, dem_tif, work_dir, "ridges") as region:
        cell_km2 = region["nsres"] * region["ewres"] / 1e6

        # Ridges are the drainage network of the upside-down relief.
        gs.mapcalc("inverted = -1.0 * dem", overwrite=True)
        demgrass.flow_accumulation(gs, "inverted", "accum", args)
        peak = demgrass.accumulation_max(gs, "accum")
        log(f"accumulation: max {peak:.0f} cells ({peak * cell_km2:.1f} km2), "
            f"threshold {args.threshold} = {args.threshold * cell_km2:.1f} km2")

        gs.mapcalc(f"seeds = if(accum > {args.threshold}, 1, 0)", overwrite=True)
        seeded = int(float(gs.parse_command("r.univar", map="seeds", flags="g")["sum"]))
        log(f"seed cells above the threshold: {seeded}")
        if seeded == 0:
            log("nothing above the threshold - lower --threshold")
            return None

        # The 0/1 seed raster stands in for accumulation, so threshold=1 keeps
        # exactly those cells and r.stream.extract only has to thin and connect
        # them into a topologically clean network.
        log(f"r.stream.extract (min segment {args.min_cells} cells)")
        gs.run_command(
            "r.stream.extract",
            elevation="inverted",
            accumulation="seeds",
            threshold=1,
            stream_length=args.min_cells,
            memory=args.memory,
            stream_raster="ridges_rast",
            stream_vector="ridges",
            overwrite=True,
        )

        vector = "ridges"
        if args.vectorizer == "r.to.vect":
            # The same raster, vectorized by the generic converter instead: no
            # stream attributes, but corners are smoothed.
            gs.run_command(
                "r.to.vect", input="ridges_rast", output="ridges_tv",
                type="line", flags="s", overwrite=True,
            )
            vector = "ridges_tv"

        vector = demgrass.lines_only(gs, vector)

        if args.sharpness > 0:
            sharp = keep_sharp_lines(gs, vector, region, args)
            if sharp is None:
                return None
            vector = sharp

        res = (region["nsres"] + region["ewres"]) / 2
        vector = demgrass.generalize(gs, vector, demgrass.resolve_simplify(args.simplify, res))

        lines = gs.vector_info_topo(vector)["lines"]
        log(f"ridge lines: {lines}, {demgrass.vertex_count(gs, vector)} vertices")
        if not lines:
            return None

        exported = work_dir / "ridges_native.gpkg"
        demgrass.export_lines(gs, vector, exported)
        return str(exported), vector


def keep_sharp_lines(gs, vector: str, region, args) -> str | None:
    """Drop lines that follow a smooth divide instead of a crest.

    Convexity - the DEM minus a moving average of it - is a few metres on the
    flank of a dome like Elbrus and tens of metres on an arete, so the median
    along a line separates the two cleanly whatever the absolute height is.
    """
    res = (region["nsres"] + region["ewres"]) / 2
    # r.neighbors wants an odd window; `| 1` rounds the cell count up to one.
    size = max(3, int(round(args.sharp_window / res)) | 1)
    log(f"sharpness: convexity over a {size}-cell ({size * res:.0f} m) window")
    gs.run_command(
        "r.neighbors", input="dem", output="dem_smooth",
        method="average", size=size, overwrite=True,
    )
    gs.mapcalc("convexity = dem - dem_smooth", overwrite=True)
    gs.run_command(
        "v.rast.stats", map=vector, raster="convexity",
        column_prefix="sharp", method="median", type="line", flags="c",
    )

    values = []
    for row in gs.read_command("v.db.select", map=vector, columns="sharp_median",
                               separator=",").splitlines()[1:]:
        row = row.strip()
        if row:
            values.append(float(row))
    if values:
        values.sort()
        quartiles = [values[int(q * (len(values) - 1))] for q in (0, 0.25, 0.5, 0.75, 1)]
        log("line convexity (m): " + " / ".join(f"{v:.1f}" for v in quartiles)
            + "  [min / q1 / median / q3 / max]")

    output = f"{vector}_sharp"
    gs.run_command(
        "v.extract", input=vector, output=output,
        where=f"sharp_median IS NOT NULL AND sharp_median >= {args.sharpness}",
        overwrite=True,
    )
    kept = gs.vector_info_topo(output)["lines"]
    log(f"sharp enough (>= {args.sharpness} m): {kept} of {len(values)} lines")
    if not kept:
        log("everything looked smooth - lower --sharpness")
        return None
    return output


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    demgrass.add_common_arguments(parser, default_output="ridges.gpkg",
                                  feature="crest lines", osm=True)
    parser.add_argument("--threshold", type=float, default=18000,
                        help="flow accumulation in cells above which a crest is kept "
                             "(default: 18000)")
    parser.add_argument("--sharpness", type=float, default=10.0, metavar="M",
                        help="keep a line only if it stands this many metres above its "
                             "surroundings (median); 0 keeps smooth divides too (default: 10)")
    parser.add_argument("--sharp-window", type=float, default=500.0, metavar="M",
                        help="width of the moving average the sharpness is measured "
                             "against (default: 500)")
    parser.add_argument("--vectorizer", choices=("stream", "r.to.vect"), default="stream",
                        help="'stream' takes r.stream.extract's own vector, 'r.to.vect' "
                             "vectorizes its raster instead (default: stream)")
    args = parser.parse_args(argv)

    bbox = demgrass.normalize_bbox(parser, args)
    demgrass.quiet_grass(args.verbose)
    demgrass.resolve_simplify(args.simplify, 1.0)  # fail on a bad value before any work
    demgrass.validate_output(args.output, osm=True)
    work_dir = demgrass.work_directory(args)

    try:
        dem_tif = demgrass.dem_for_args(args, bbox, work_dir)
        gs = demgrass.import_grass_script()
        result = extract_ridges(gs, dem_tif, work_dir, args)
        if result is None:
            log("no ridges extracted; nothing written")
            return 1
        exported, layer = result
        demgrass.write_output(
            Path(exported), args.output, args.out_crs, layer, work_dir,
            generator="dem2ridges", tags_for=lambda feature: OSM_TAGS,
        )
        log(f"wrote {args.output} ({args.out_crs})")
    finally:
        demgrass.cleanup(work_dir, args.keep_work)
    return 0


if __name__ == "__main__":
    sys.exit(main())
