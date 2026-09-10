"""Shared constants for the Garmin map service."""

from __future__ import annotations

import os
from pathlib import Path

from otmlib import paths

# The service directory, which is also the package directory: www/garminsvc in a
# checkout, /app/garminsvc in the image.
ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent.parent

# Base Garmin family ids (4 digits: mapid = {id}0001). Each build takes the next free pair.
FAMILY_ID_MAP = 6324
FAMILY_ID_CONTOURS = 5355
FAMILY_ID_MAX = 9999
# One value per level of style/opentopomap-hike/options: 24, 23, 22, 21, 20, 19, 18, 16
DEM_DISTS = "9942,9942,9942,19884,19884,39768,39768,53024"

SEA_URL = "https://www.thkukuk.de/osm/data/sea-latest.zip"
BOUNDS_URL = "https://www.thkukuk.de/osm/data/bounds-latest.zip"

# Styles and mkgmap *_options stay in garmin/; the service only consumes them.
# In Docker the image (or a bind mount) puts that tree at OTM_GARMIN_DIR.
GARMIN_DIR = Path(os.environ.get("OTM_GARMIN_DIR", str(REPO_ROOT / "garmin")))
STYLE_DIR = GARMIN_DIR / "style"
OPTIONS_MAIN = GARMIN_DIR / "opentopomap_hike_options"
OPTIONS_CONTOURS = GARMIN_DIR / "contours_hike_options"

# Shared with tilesvc: same tree, same layout (see otmlib.paths).
DATA_DIR = paths.resolve_data_dir(ROOT / "data")
# mkgmap and splitter (see garminsvc.artifacts). Downloaded content, so it lives
# under data/ and not in the source tree: git and Docker already ignore that
# whole directory, and a checkout stays clean. The image sets OTM_TOOLS_DIR to a
# path of its own so the jars are baked in rather than written to the volume.
TOOLS_DIR = Path(os.environ.get("OTM_TOOLS_DIR") or DATA_DIR / "tools")
SEA_DIR = DATA_DIR / "sea"
BOUNDS_DIR = DATA_DIR / "bounds"
HGT_CACHE = paths.hgt_cache(DATA_DIR)
# Geofabrik extracts, shared with tilesvc: the same file is both a bbox source
# here and tilemaker's input there, kept current from one tracked sequence.
GEOFABRIK_CACHE = paths.geofabrik_cache(DATA_DIR)
JOBS_DIR = DATA_DIR / "jobs"
# Built bbox previews (<id>.pmtiles). Written by the preview worker in the
# tilesvc image, published as static files by nginx, and read here only to
# check that a finished preview is still on disk.
PREVIEWS_DIR = paths.previews(DATA_DIR)
# MapLibre style of the vector map shown as a basemap in the bbox picker. In a git
# checkout the style lives at repo/vector/; in Docker it is bind-mounted next to
# the package, into /app/vector - hence ROOT.parent - and a deployment without the
# repo can drop the files into data/vector instead.
VECTOR_STYLE_DIRS = (
    ROOT.parent / "vector/maplibregljs",
    REPO_ROOT / "vector/maplibregljs",
    DATA_DIR / "vector",
)
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
