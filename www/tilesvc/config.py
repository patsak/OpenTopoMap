"""Region list and paths for the tile job."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from otmlib import paths
from otmlib.geofabrik import GEOFABRIK_BASE_URL, mirror_base_url

ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parent.parent
CONFIG_ENV = "OTM_TILESVC_CONFIG"
DEFAULT_CONFIG = ROOT / "config.yaml"


@dataclass(frozen=True)
class Region:
    """One Geofabrik extract to keep current in the tileset."""

    geofabrik_id: str


@dataclass(frozen=True)
class Config:
    data_dir: Path
    regions: list[Region]
    # Local directory laid out with Geofabrik's own paths (index-v1.json,
    # <region>-latest.osm.pbf, <region>-updates/state.txt, …) to read extracts
    # from instead of download.geofabrik.de. None uses the real site.
    geofabrik_mirror: Path | None = None

    @property
    def geofabrik_cache(self) -> Path:
        return paths.geofabrik_cache(self.data_dir)

    @property
    def geofabrik_base_url(self) -> str:
        if self.geofabrik_mirror is None:
            return GEOFABRIK_BASE_URL
        return mirror_base_url(self.geofabrik_mirror)

    @property
    def shapefiles(self) -> Path:
        return paths.shapefiles(self.data_dir)

    @property
    def vector_tiles(self) -> Path:
        return paths.vector_tiles(self.data_dir)

    @property
    def tiles_input(self) -> Path:
        return paths.tiles_input(self.data_dir)

    @property
    def tilemaker_store(self) -> Path:
        return paths.tilemaker_store(self.data_dir)


def config_path() -> Path:
    configured = os.environ.get(CONFIG_ENV, "").strip()
    return Path(configured) if configured else DEFAULT_CONFIG


def load(path: Path | None = None) -> Config:
    source = path or config_path()
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}

    regions = [Region(geofabrik_id=str(entry)) for entry in raw.get("regions") or []]
    if not regions:
        raise ValueError(f"{source}: no regions configured; the job would produce nothing")

    fallback = REPO_ROOT / "www/garminsvc/data"
    data_dir = Path(raw["data_dir"]) if raw.get("data_dir") else paths.resolve_data_dir(fallback)
    geofabrik_mirror = Path(raw["geofabrik_mirror"]) if raw.get("geofabrik_mirror") else None

    return Config(data_dir=data_dir, regions=regions, geofabrik_mirror=geofabrik_mirror)
