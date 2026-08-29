"""Shared constants for the Garmin map service."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Базовые Garmin family-id (4 цифры: mapid = {id}0001). Каждая сборка берёт следующие свободные.
FAMILY_ID_MAP = 6324
FAMILY_ID_CONTOURS = 5355
FAMILY_ID_MAX = 9999
# One value per level of style/opentopomap-hike/options: 24, 23, 22, 21, 20, 19, 18, 16
DEM_DISTS = "9942,9942,9942,19884,19884,39768,39768,53024"

# Adaptive contour spacing: plain 10 m, mountain 20 m (Genshtab-style).
MOUNTAIN_RELIEF_M = 300
MOUNTAIN_MAX_ELEV_M = 1200

MKGMAP_VERSION = "mkgmap-r4924"
SPLITTER_VERSION = "splitter-r654"
SEA_URL = "https://www.thkukuk.de/osm/data/sea-latest.zip"
BOUNDS_URL = "https://www.thkukuk.de/osm/data/bounds-latest.zip"
MKGMAP_URL = f"https://www.mkgmap.org.uk/download/{MKGMAP_VERSION}.zip"
SPLITTER_URL = f"https://www.mkgmap.org.uk/download/{SPLITTER_VERSION}.zip"

TOOLS_DIR = ROOT / "tools"
MKGMAP_JAR = TOOLS_DIR / "mkgmap.jar"
STYLE_DIR = ROOT / "style"
OPTIONS_MAIN = ROOT / "opentopomap_hike_options"
OPTIONS_CONTOURS = ROOT / "contours_hike_options"
RIDGES_SCRIPT = TOOLS_DIR / "contours_to_ridges.py"

DATA_DIR = ROOT / "data"
SEA_DIR = DATA_DIR / "sea"
BOUNDS_DIR = DATA_DIR / "bounds"
GEOFABRIK_CACHE = DATA_DIR / "geofabrik-cache"
DEM_CACHE = DATA_DIR / "dem-cache"
JOBS_DIR = DATA_DIR / "jobs"
JOBS_DB = DATA_DIR / "mapsvc.db"
# Huey BEGIN EXCLUSIVE cannot share this file with job records / HTTP polls.
HUEY_DB = DATA_DIR / "huey.db"
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
