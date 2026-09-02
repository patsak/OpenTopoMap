"""Layout of the data directory both services share.

garminsvc, tilesvc and Martin all read the same tree (one Docker volume). The names
live here so a rename cannot leave one service writing where another is not looking.
"""

from __future__ import annotations

import os
from pathlib import Path

DATA_DIR_ENV = "OTM_DATA_DIR"

GEOFABRIK_CACHE_NAME = "geofabrik-cache"
DEM_CACHE_NAME = "dem-cache"

SHAPEFILES_NAME = "shapefiles"
VECTOR_TILES_NAME = "vector-tiles"
TILES_INPUT_NAME = "tiles-input"
TILEMAKER_STORE_NAME = "tilemaker-store"
PREVIEWS_NAME = "previews"


def resolve_data_dir(fallback: Path) -> Path:
    """Shared data root: ``OTM_DATA_DIR`` when set, else the caller's default."""
    configured = os.environ.get(DATA_DIR_ENV, "").strip()
    return Path(configured) if configured else fallback


def geofabrik_cache(data_dir: Path) -> Path:
    return data_dir / GEOFABRIK_CACHE_NAME


def hgt_cache(data_dir: Path) -> Path:
    return data_dir / DEM_CACHE_NAME / "hgt"


def shapefiles(data_dir: Path) -> Path:
    """Cache for external geo data the tile service downloads, e.g. sea polygons."""
    return data_dir / SHAPEFILES_NAME


def vector_tiles(data_dir: Path) -> Path:
    """Built ``.mbtiles`` files. Martin serves this directory, naming each
    source after the file stem: ``otm.mbtiles`` → ``/otm/{z}/{x}/{y}``."""
    return data_dir / VECTOR_TILES_NAME


def tiles_input(data_dir: Path) -> Path:
    """Merged PBF handed to tilemaker. Derived, disposable."""
    return data_dir / TILES_INPUT_NAME


def previews(data_dir: Path) -> Path:
    """Built ``<preview_id>.pmtiles`` for the bbox previews. nginx serves this
    directory as static files; the browser reads them with range requests."""
    return data_dir / PREVIEWS_NAME


def tilemaker_store(data_dir: Path) -> Path:
    """tilemaker's on-disk node/way store (``--store``). Disposable, and large:
    without it a federal-district run holds the whole extract in RAM."""
    return data_dir / TILEMAKER_STORE_NAME
