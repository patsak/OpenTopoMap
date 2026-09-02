"""Layout of the data directory both services share.

garminsvc, tilesvc and the preview worker all read the same tree (one Docker
volume). The names live here so a rename cannot leave one service writing where
another is not looking.
"""

from __future__ import annotations

import os
from pathlib import Path

DATA_DIR_ENV = "OTM_DATA_DIR"

GEOFABRIK_CACHE_NAME = "geofabrik-cache"
DEM_CACHE_NAME = "dem-cache"

PREVIEWS_NAME = "previews"


def resolve_data_dir(fallback: Path) -> Path:
    """Shared data root: ``OTM_DATA_DIR`` when set, else the caller's default."""
    configured = os.environ.get(DATA_DIR_ENV, "").strip()
    return Path(configured) if configured else fallback


def geofabrik_cache(data_dir: Path) -> Path:
    return data_dir / GEOFABRIK_CACHE_NAME


def hgt_cache(data_dir: Path) -> Path:
    return data_dir / DEM_CACHE_NAME / "hgt"


def previews(data_dir: Path) -> Path:
    """Built ``<preview_id>.pmtiles`` for the bbox previews. nginx serves this
    directory as static files; the browser reads them with range requests."""
    return data_dir / PREVIEWS_NAME

