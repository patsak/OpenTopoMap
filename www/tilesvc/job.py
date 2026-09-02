"""The scheduled pass: sync Geofabrik extracts, then build the tilesets.

Both steps are skip-if-current. Geofabrik publishes daily diffs, so on a
typical night the sync applies a handful of ``.osc.gz`` files to each cached
PBF and the ocean tileset is left alone entirely; only the regions that
actually moved make ``otm.mbtiles`` stale.
"""

from __future__ import annotations

import logging
from pathlib import Path

from otmlib import pg, pgmeta, regionsync
from otmlib.geofabrik import merge_pbfs, region_by_id

from tilesvc import tilemaker
from tilesvc.config import Config
from tilesvc.ocean_tiles import build_ocean

log = logging.getLogger(__name__)

MERGED_INPUT_NAME = "otm.osm.pbf"


def sql_dir() -> Path:
    return Path(__file__).resolve().parent / "sql"


def sync_regions(cfg: Config) -> list[regionsync.SyncResult]:
    """Resolve the configured regions and bring their cached PBFs up to date.

    Each region's full extract is downloaded once and then kept current in
    place by ``osmium apply-changes``, from the sequence tracked in
    ``otm.replication_state`` (see :mod:`otmlib.regionsync`).
    """
    pg.ensure_schema(sql_dir())
    cfg.geofabrik_cache.mkdir(parents=True, exist_ok=True)
    regions = [
        region_by_id(
            entry.geofabrik_id, cache_dir=cfg.geofabrik_cache, base_url=cfg.geofabrik_base_url
        )
        for entry in cfg.regions
    ]
    results = regionsync.sync_regions(regions, cfg.geofabrik_cache)
    # Coverage follows config.yaml: a region removed from it stops contributing
    # to the map's initial view even though its PBF is still on disk.
    pgmeta.prune_regions([r.region.region_id for r in results])
    return results


def build_tiles(cfg: Config, results: list[regionsync.SyncResult], *, force: bool = False) -> bool:
    """Build ``otm.mbtiles`` from the synced regions. True when it was rebuilt."""
    if not results:
        return False
    revision = regionsync.tileset_revision(results)
    if not force and not tilemaker.needs_rebuild(
        tilemaker.TILESET_OTM, revision, cfg.vector_tiles
    ):
        log.info("otm.mbtiles is current")
        return False

    cfg.tiles_input.mkdir(parents=True, exist_ok=True)
    merged = merge_pbfs([r.pbf for r in results], cfg.tiles_input / MERGED_INPUT_NAME)
    tilemaker.build_otm(
        revision,
        merged,
        tiles_dir=cfg.vector_tiles,
        store_dir=cfg.tilemaker_store / tilemaker.TILESET_OTM,
    )
    # Martin opened the previous file at its own startup and keeps serving it;
    # the replacement is only picked up on restart.
    log.info("Restart the tile server to serve the new otm.mbtiles")
    return True


def run_once(cfg: Config, *, recreate: bool = False) -> None:
    results = sync_regions(cfg)
    rebuilt = build_tiles(cfg, results, force=recreate)
    build_ocean(cfg, force=recreate)
    log.info(
        "Synced %d region(s); otm.mbtiles %s",
        len(results),
        "rebuilt" if rebuilt else "unchanged",
    )
