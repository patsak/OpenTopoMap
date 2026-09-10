"""The scheduled pass: keep the configured Geofabrik extracts current.

Geofabrik publishes daily diffs, so on a typical night this applies a handful
of ``.osc.gz`` files to each cached PBF and records how far it got. Nothing is
rendered here: tiles are built per drawn bbox, on demand, by
:mod:`tilesvc.preview`, out of exactly these files.
"""

from __future__ import annotations

import logging
from pathlib import Path

from otmlib import pg, pgmeta, regionsync
from otmlib.geofabrik import region_by_id

from tilesvc.config import Config

log = logging.getLogger(__name__)


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
    # Coverage follows config.yaml: a region removed from it stops being
    # offered for previews even though its PBF is still on disk.
    pgmeta.prune_regions([r.region.region_id for r in results])
    return results


def run_once(cfg: Config) -> None:
    results = sync_regions(cfg)
    changed = [r.region.region_id for r in results if r.changed]
    log.info(
        "Synced %d region(s); %s",
        len(results),
        f"updated: {', '.join(changed)}" if changed else "nothing moved",
    )
