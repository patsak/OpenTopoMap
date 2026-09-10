#!/usr/bin/env python3
"""Draw a river network from a DEM with GRASS hydrology.

The same chain as dem2ridges.py, minus the inversion: rivers are the drainage
network of the relief as it is.

    1. DEM for the bbox: GEDTM 30 m cropped through /vsicurl (the same source
       www/otmlib/dem.py feeds the tile and Garmin builds), or --dem <file|dir>
    2. reproject to a metric CRS (auto UTM) so cells are square metres
    3. r.watershed       flow accumulation and drainage directions
    4. r.stream.extract  channels wherever the catchment reaches --catchment km2
    5. r.stream.order    Strahler / Horton / Shreve / Hack hierarchy
    6. generalize and write GPKG/GeoJSON/Shapefile in EPSG:4326

    ./dem2rivers.py --bbox 42.3 43.0 43.5 43.5 -o rivers.gpkg
    ./dem2rivers.py --dem ~/dem/caucasus --catchment 5 -o rivers.geojson

--catchment is the drainage area a channel needs before it is drawn, in square
kilometres, so it means the same thing at any resolution: 30 km2 keeps the
valley rivers, 1-5 km2 fills the map with mountain brooks.

Lines run downstream, and every segment carries its Strahler/Horton/Shreve/Hack
order, its length and gradient, and catchment_km2 - the catchment area at its
lower end - so the hierarchy is there for line widths and for telling a river
from a brook. No OSM tagging yet: this writes GIS files to look at, not a JOSM
layer to trace (dem2ridges.py has that if a river version is wanted later).

Either way, DEM-derived lines are not survey data: they ignore where the water
actually runs (canals, karst, dry beds) and they cut straight across lakes.

r.stream.order is a GRASS addon. Install it once:

    grass --tmp-project EPSG:4326 --exec g.extension extension=r.stream.order
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import demgrass
from demgrass import log


def extract_rivers(gs, dem_tif: Path, work_dir: Path, args) -> tuple[str, str] | None:
    """Run the GRASS chain in a throwaway project; return (exported gpkg, map)."""
    with demgrass.dem_project(gs, dem_tif, work_dir, "rivers") as region:
        # find_program runs the module, which needs the session PATH the project
        # just set up - GRASS puts its addons on it, not the system PATH.
        if not gs.find_program("r.stream.order", "--help"):
            raise SystemExit(
                "r.stream.order not found; install the addon with\n"
                "  grass --tmp-project EPSG:4326 --exec g.extension extension=r.stream.order"
            )

        cell_km2 = region["nsres"] * region["ewres"] / 1e6
        threshold = args.catchment / cell_km2

        demgrass.flow_accumulation(gs, "dem", "accum", args)
        peak = demgrass.accumulation_max(gs, "accum")
        log(f"accumulation: max {peak:.0f} cells ({peak * cell_km2:.1f} km2), "
            f"channel starts at {args.catchment} km2 = {threshold:.0f} cells")
        if peak < threshold:
            log("no catchment that large in this bbox - lower --catchment")
            return None

        # r.stream.order needs the direction map from the very module that drew
        # the streams, so r.stream.extract has to write its own.
        log(f"r.stream.extract (min segment {args.min_cells} cells)")
        gs.run_command(
            "r.stream.extract",
            elevation="dem",
            accumulation="accum",
            threshold=threshold,
            stream_length=args.min_cells,
            memory=args.memory,
            stream_raster="streams_rast",
            direction="streams_dir",
            overwrite=True,
        )

        # The ordered vector is the output: lines directed downstream, one
        # feature per segment, with the hierarchy and the segment geometry
        # (length, gradient, elevation drop) already in the attribute table.
        log("r.stream.order (strahler, horton, shreve, hack)")
        gs.run_command(
            "r.stream.order",
            stream_rast="streams_rast",
            direction="streams_dir",
            elevation="dem",
            accumulation="accum",
            stream_vect="rivers",
            memory=args.memory,
            overwrite=True,
        )

        vector = demgrass.lines_only(gs, "rivers")
        add_catchment_column(gs, vector, cell_km2)
        log_hierarchy(gs, vector)

        res = (region["nsres"] + region["ewres"]) / 2
        vector = demgrass.generalize(gs, vector, demgrass.resolve_simplify(args.simplify, res))

        lines = gs.vector_info_topo(vector)["lines"]
        log(f"river segments: {lines}, {demgrass.vertex_count(gs, vector)} vertices")
        if not lines:
            return None

        exported = work_dir / "rivers_native.gpkg"
        demgrass.export_lines(gs, vector, exported)
        return str(exported), vector


def add_catchment_column(gs, vector: str, cell_km2: float) -> None:
    """Write each segment's own catchment area, in km2, into the table.

    r.stream.order's flow_accum is undocumented and does not behave like an
    outlet value (a tributary can carry more of it than the trunk below the
    confluence), so the area comes from the accumulation raster --catchment was
    thresholded against. Accumulation grows monotonically down a channel, hence
    the maximum along a segment is the catchment at its downstream end.
    """
    gs.run_command(
        "v.rast.stats", map=vector, raster="accum",
        column_prefix="acc", method="maximum", type="line", flags="c",
    )
    gs.run_command("v.db.addcolumn", map=vector, columns="catchment_km2 double precision")
    gs.run_command("v.db.update", map=vector, column="catchment_km2",
                   query_column=f"acc_maximum * {cell_km2}")
    gs.run_command("v.db.dropcolumn", map=vector, columns="acc_maximum")


def log_hierarchy(gs, vector: str) -> None:
    """The spread of the network, which is what --catchment is tuned against."""
    counts: dict[int, int] = {}
    total_km = 0.0
    catchments: list[float] = []
    for row in gs.read_command("v.db.select", map=vector,
                               columns="strahler,length,catchment_km2",
                               separator=",").splitlines()[1:]:
        parts = row.strip().split(",")
        if len(parts) != 3 or not parts[0]:
            continue
        order = int(float(parts[0]))
        counts[order] = counts.get(order, 0) + 1
        total_km += float(parts[1] or 0) / 1000
        if parts[2]:
            catchments.append(float(parts[2]))
    if counts:
        log("strahler order: "
            + ", ".join(f"{order}x{counts[order]}" for order in sorted(counts))
            + f" ({total_km:.0f} km of channel)")
    if catchments:
        catchments.sort()
        quartiles = [catchments[int(q * (len(catchments) - 1))] for q in (0, 0.5, 1)]
        log("segment catchment (km2): " + " / ".join(f"{v:.0f}" for v in quartiles)
            + "  [min / median / max]")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    demgrass.add_common_arguments(parser, default_output="rivers.gpkg", feature="channels")
    parser.add_argument("--catchment", type=float, default=30.0, metavar="KM2",
                        help="drainage area a channel needs before it is drawn, in km2 "
                             "(default: 30)")
    args = parser.parse_args(argv)

    bbox = demgrass.normalize_bbox(parser, args)
    demgrass.quiet_grass(args.verbose)
    demgrass.resolve_simplify(args.simplify, 1.0)  # fail on a bad value before any work
    demgrass.validate_output(args.output, osm=False)
    if args.catchment <= 0:
        parser.error("--catchment must be positive")
    work_dir = demgrass.work_directory(args)

    try:
        dem_tif = demgrass.dem_for_args(args, bbox, work_dir)
        gs = demgrass.import_grass_script()
        result = extract_rivers(gs, dem_tif, work_dir, args)
        if result is None:
            log("no rivers extracted; nothing written")
            return 1
        exported, layer = result
        demgrass.write_output(Path(exported), args.output, args.out_crs, layer, work_dir)
        log(f"wrote {args.output} ({args.out_crs})")
    finally:
        demgrass.cleanup(work_dir, args.keep_work)
    return 0


if __name__ == "__main__":
    sys.exit(main())
