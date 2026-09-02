"""Build ``.mbtiles`` with tilemaker.

Tiles are rendered ahead of time here and served as files, so a tile request is
a lookup rather than a computation. Everything expensive — reading the PBF,
assembling multipolygons, generalizing, simplifying — happens once per nightly
run instead of once per uncached tile.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from otmlib import pgmeta
from otmlib import tilemaker as runner

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
# In the image the tilemaker tree is copied to /app/vector/tilemaker; in a
# checkout it sits next to www/. Same two-candidate lookup the styles use.
STYLE_DIRS = (
    ROOT.parent / "vector/tilemaker",
    ROOT.parent.parent / "vector/tilemaker",
)
PROCESS_LUA = runner.PROCESS_LUA
CONFIG_REGION = "tilemaker-config-otm-region.json"
CONFIG_OCEAN = "tilemaker-config-otm-ocean.json"

TILESET_OTM = "otm"
TILESET_OCEAN = "otm-ocean"


def style_dir() -> Path:
    for directory in STYLE_DIRS:
        if (directory / PROCESS_LUA).is_file():
            return directory
    raise RuntimeError(f"{PROCESS_LUA} not found in vector/tilemaker")


def tileset_path(tiles_dir: Path, tileset: str) -> Path:
    return tiles_dir / f"{tileset}.mbtiles"


def needs_rebuild(tileset: str, revision: str, tiles_dir: Path) -> bool:
    """True unless the built file exists and already matches *revision*."""
    output = tileset_path(tiles_dir, tileset)
    if not output.is_file() or output.stat().st_size == 0:
        return True
    return pgmeta.get_tile_state(tileset) != revision


def build_tileset(
    tileset: str,
    revision: str,
    *,
    config: Path,
    tiles_dir: Path,
    store_dir: Path,
    input_pbf: Path | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    cwd: Path | None = None,
) -> Path:
    """Run tilemaker into ``<tiles_dir>/<tileset>.mbtiles``.

    Writes to a temporary sibling and renames on success: Martin holds the
    served file open, and a half-written mbtiles would otherwise be visible to
    it (and would survive a failed run as the new truth).

    Either *input_pbf* or *bbox* must be given — a config whose layers all come
    from shapefiles has no OSM input, and tilemaker then needs the extent
    stated explicitly.
    """
    if input_pbf is None and bbox is None:
        raise ValueError("build_tileset: pass input_pbf, bbox, or both")

    tiles_dir.mkdir(parents=True, exist_ok=True)
    store_dir.mkdir(parents=True, exist_ok=True)
    output = tileset_path(tiles_dir, tileset)
    # The staging name has to keep the .mbtiles extension: tilemaker picks its
    # output format from it, and anything else (".mbtiles.tmp" included) makes
    # it write a directory of loose tiles instead of one file.
    tmp = tiles_dir / f"{tileset}.building.mbtiles"
    tmp.unlink(missing_ok=True)

    styles = style_dir()
    log.info("tilemaker → %s (%s)", output.name, revision)
    try:
        runner.build(
            output=tmp,
            config=config,
            process=styles / PROCESS_LUA,
            store_dir=store_dir,
            input_pbf=input_pbf,
            bbox=bbox,
            cwd=cwd or styles.parent.parent,
        )
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    if not tmp.is_file() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"tilemaker produced no output: {tmp}")

    os.replace(tmp, output)
    pgmeta.set_tile_state(tileset, revision)
    log.info("%s: %.1f MB", output.name, output.stat().st_size / 1e6)
    return output


def build_otm(
    revision: str,
    input_pbf: Path,
    *,
    tiles_dir: Path,
    store_dir: Path,
) -> Path:
    """The OSM tileset. Uses the region config — the shapefile layers of the
    full config (ocean, admin points) are built separately, see build_ocean."""
    return build_tileset(
        TILESET_OTM,
        revision,
        config=style_dir() / CONFIG_REGION,
        tiles_dir=tiles_dir,
        store_dir=store_dir,
        input_pbf=input_pbf,
    )
