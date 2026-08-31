#!/usr/bin/env python3
"""Turn the Garmin hike contour PBF into a vector tileset for the web style.

The client-side isolines (maplibre-contour over the DEM) cannot know where a
contour crosses a glacier or how steep the slope is, so three parts of the
Genshtab cartography are impossible with them: blue contours on ice, thinned
major contours on slopes over 50 degrees, and the crevasse hatching that is cut
out of the contour lines. All three already exist in the Garmin pipeline
(www/garminsvc/garminsvc/contour_post.py and crevasse.py). This script converts that
output into tiles the MapLibre style can consume instead.

    # 1. produce the tagged contour PBF with the existing Garmin pipeline
    # 2. then:
    python3 vector/tools/build_contours.py contours-tagged/*.osm.pbf \
        --crevasses crevasse-stripes.osm --output otm-contours.mbtiles

Attribute mapping (tag -> tile attribute):
    ele                        -> ele        (number)
    contour_ext=elevation_*    -> level      (2 major / 1 medium / 0 minor)
    glacier=yes                -> on_glacier (bool, drives the blue contours)
    steep=yes                  -> steep      (bool, thins the major contours)
    crevasse=stripe|stripe2    -> type       ("crevasse_stripe", layer "crevasses")

Requires osmium-tool and tippecanoe on PATH.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

LEVEL_BY_EXT = {"elevation_major": 2, "elevation_medium": 1, "elevation_minor": 0}


def require(tool: str):
    if shutil.which(tool) is None:
        raise SystemExit(f"{tool} not found on PATH")


def export_geojsonseq(pbf: Path) -> subprocess.Popen:
    return subprocess.Popen(
        ["osmium", "export", "-f", "geojsonseq", "--add-unique-id=type_id", "-o", "-", str(pbf)],
        stdout=subprocess.PIPE,
        text=True,
    )


def merge_sorted(pbfs: list[Path], out_path: Path) -> Path:
    """pyhgtmap writes one PBF per DEM band and contour_post leaves them unsorted,
    while osmium export insists on nodes-before-ways, so merge and sort first."""
    subprocess.run(
        ["osmium", "sort", "--overwrite", "-o", str(out_path), *[str(p) for p in pbfs]],
        check=True,
    )
    return out_path


def convert_contours(pbf: Path, out_path: Path) -> int:
    """Write one GeoJSON feature per line, keeping only contour ways."""
    written = 0
    proc = export_geojsonseq(pbf)
    with out_path.open("w", encoding="utf-8") as sink:
        for line in proc.stdout:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            feature = json.loads(line)
            tags = feature.get("properties") or {}
            if tags.get("contour") != "elevation":
                continue
            try:
                ele = float(tags["ele"])
            except (KeyError, TypeError, ValueError):
                continue
            properties = {
                "ele": int(round(ele)),
                "level": LEVEL_BY_EXT.get(tags.get("contour_ext"), 0),
            }
            if tags.get("glacier") == "yes":
                properties["on_glacier"] = True
            if tags.get("steep") == "yes":
                properties["steep"] = True
            feature["properties"] = properties
            sink.write(json.dumps(feature) + "\n")
            written += 1
    if proc.wait() != 0:
        raise SystemExit(f"osmium export failed for {pbf}")
    return written


def convert_crevasses(source: Path, out_path: Path) -> int:
    written = 0
    proc = export_geojsonseq(source)
    with out_path.open("w", encoding="utf-8") as sink:
        for line in proc.stdout:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            feature = json.loads(line)
            tags = feature.get("properties") or {}
            stripe = tags.get("crevasse")
            if stripe not in ("stripe", "stripe2") and tags.get("natural") != "crevasse":
                continue
            feature["properties"] = {
                "type": "crevasse_stripe" if stripe else "crevasse",
                "width": 2 if stripe == "stripe2" else 1,
            }
            sink.write(json.dumps(feature) + "\n")
            written += 1
    if proc.wait() != 0:
        raise SystemExit(f"osmium export failed for {source}")
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "contours", type=Path, nargs="+", help="contour PBFs tagged by garminsvc/contour_post.py"
    )
    parser.add_argument("--crevasses", type=Path, help="crevasse hatching from garminsvc/crevasse.py (.osm or .pbf)")
    parser.add_argument("--output", type=Path, default=Path("otm-contours.mbtiles"))
    parser.add_argument("--minzoom", type=int, default=10)
    parser.add_argument("--maxzoom", type=int, default=14)
    args = parser.parse_args(argv)

    require("osmium")
    require("tippecanoe")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        contour_json = tmp_dir / "contours.geojsonseq"
        merged = merge_sorted(args.contours, tmp_dir / "contours.osm.pbf")
        count = convert_contours(merged, contour_json)
        print(f"contours: {count} lines")
        inputs = ["-L", f"contours:{contour_json}"]

        if args.crevasses:
            crevasse_json = tmp_dir / "crevasses.geojsonseq"
            crevasses = merge_sorted([args.crevasses], tmp_dir / "crevasses.osm.pbf")
            count = convert_crevasses(crevasses, crevasse_json)
            print(f"crevasses: {count} lines")
            inputs += ["-L", f"crevasses:{crevasse_json}"]

        command = [
            "tippecanoe",
            "-o", str(args.output),
            "--force",
            "-Z", str(args.minzoom),
            "-z", str(args.maxzoom),
            "--drop-densest-as-needed",
            "--simplification=2",
            "--no-tile-size-limit",
            *inputs,
        ]
        print(" ".join(command))
        subprocess.run(command, check=True)

    print(f"wrote {args.output}")
    print("serve it and point the 'contour-source' of index.html at the tile URL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
