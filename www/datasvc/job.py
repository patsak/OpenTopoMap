"""The scheduled pass: keep the configured Geofabrik extracts current.

Geofabrik publishes daily diffs, so on a typical night this applies a handful
of ``.osc.gz`` files to each cached PBF and records how far it got. Nothing is
rendered here: tiles are built per drawn bbox, on demand, by
:mod:`datasvc.preview`, out of exactly these files.

Then, as a step of its own, the GOL www/overpass answers from is rebuilt out of
the same files (:mod:`otmlib.gol`) - here rather than by something watching the
extracts, because only this pass knows when it has finished writing them.
"""

from __future__ import annotations

import logging

from otmlib import gol, pg, pgmeta, regionsync
from otmlib.geofabrik import region_by_id

from datasvc.config import Config

log = logging.getLogger(__name__)


def sync_regions(cfg: Config) -> list[regionsync.SyncResult]:
    """Resolve the configured regions and bring their cached PBFs up to date.

    Each region's full extract is downloaded once and then kept current in
    place by ``osmium apply-changes``, from the sequence tracked in
    ``otm.replication_state`` (see :mod:`otmlib.regionsync`).
    """
    pg.ensure_shared_schema()
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


def build_gol(cfg: Config, results: list[regionsync.SyncResult]) -> bool:
    """Merge the synced extracts into ``cfg.gol_path``, unless it was built
    from these very files already. Returns whether it built.

    Asked of the files rather than of ``SyncResult.changed``: a night whose
    build failed, or a GOL deleted by hand, is made good by the next run even
    when no diff moved.
    """
    target = cfg.gol_path
    if target is None:
        return False
    pbfs = [r.pbf for r in results]
    if gol.is_current(pbfs, target):
        log.info("GOL %s is current", target.name)
        return False
    gol.build(pbfs, target)
    return True


def run_once(cfg: Config) -> None:
    results = sync_regions(cfg)
    changed = [r.region.region_id for r in results if r.changed]
    log.info(
        "Synced %d region(s); %s",
        len(results),
        f"updated: {', '.join(changed)}" if changed else "nothing moved",
    )
    build_gol(cfg, results)
