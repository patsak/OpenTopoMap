"""Shared constants for the Garmin map service."""

from __future__ import annotations

import os
from pathlib import Path

from otmlib import paths

ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ROOT.parent.parent

# Базовые Garmin family-id (4 цифры: mapid = {id}0001). Каждая сборка берёт следующие свободные.
FAMILY_ID_MAP = 6324
FAMILY_ID_CONTOURS = 5355
FAMILY_ID_MAX = 9999
# One value per level of style/opentopomap-hike/options: 24, 23, 22, 21, 20, 19, 18, 16
DEM_DISTS = "9942,9942,9942,19884,19884,39768,39768,53024"

MKGMAP_VERSION = "mkgmap-r4924"
SPLITTER_VERSION = "splitter-r654"
SEA_URL = "https://www.thkukuk.de/osm/data/sea-latest.zip"
BOUNDS_URL = "https://www.thkukuk.de/osm/data/bounds-latest.zip"
MKGMAP_URL = f"https://www.mkgmap.org.uk/download/{MKGMAP_VERSION}.zip"
SPLITTER_URL = f"https://www.mkgmap.org.uk/download/{SPLITTER_VERSION}.zip"

TOOLS_DIR = ROOT / "tools"
MKGMAP_JAR = TOOLS_DIR / "mkgmap.jar"
# Styles and mkgmap *_options stay in garmin/; the service only consumes them.
# In Docker the image (or a bind mount) puts that tree at OTM_GARMIN_DIR.
GARMIN_DIR = Path(os.environ.get("OTM_GARMIN_DIR", str(REPO_ROOT / "garmin")))
STYLE_DIR = GARMIN_DIR / "style"
OPTIONS_MAIN = GARMIN_DIR / "opentopomap_hike_options"
OPTIONS_CONTOURS = GARMIN_DIR / "contours_hike_options"
RIDGES_SCRIPT = TOOLS_DIR / "contours_to_ridges.py"

# Shared with tilesvc and Martin: same tree, same layout (see otmlib.paths).
DATA_DIR = paths.resolve_data_dir(ROOT / "data")
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
# checkout the style lives at repo/vector/; in Docker it is bind-mounted into /app,
# and a deployment without the repo can drop the files into data/vector instead.
VECTOR_STYLE_DIRS = (ROOT / "vector/maplibregljs", REPO_ROOT / "vector/maplibregljs", DATA_DIR / "vector")
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
