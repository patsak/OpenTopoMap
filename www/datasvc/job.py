"""The scheduled pass: keep the configured Geofabrik extracts current.

Geofabrik publishes daily diffs, so on a typical night this applies a handful
of ``.osc.gz`` files to each cached PBF and records how far it got. Nothing is
rendered here: tiles are built per drawn bbox, on demand, by
:mod:`datasvc.preview`, out of exactly these files.

Then, as a step of its own, the GOL www/overpass answers from is rebuilt out of
the same files (:mod:`otmlib.gol`) - here rather than by something watching the
extracts, because only this pass knows when it has finished writing them.

Last, the hiking router's graphs are built out of the same files
(:func:`build_routing`), for the same reason: only this pass knows the
extracts are final.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from otmlib import gol, pg, pgmeta, regionsync
from otmlib.geofabrik import region_by_id

from datasvc.config import Config

log = logging.getLogger(__name__)

# where ``python -m routing.…`` finds its package: /app in the image, www/ here
APP_ROOT = Path(__file__).resolve().parent.parent


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


# The routing build is two commands of www/routing, each run as a process of
# its own: what a build takes in memory (a tile's DEM and graph, the Caucasus
# compiled) is given back when it ends, and its log goes to the job's.
ROUTING_BUILD = ["-m", "routing.build", "--all"]
ROUTING_COMPILE = ["-m", "routing.compile", "--all"]


def build_routing(cfg: Config) -> bool:
    """Build the hiking router's graphs out of the extracts just synced
    (www/routing/config.yaml): the tiles whose data, DEM or parameters changed
    - most nights a checksum per tile and nothing more - then the graphs whose
    tiles moved. Returns whether both steps succeeded.

    A failure is logged and left at that: the sync and the GOL are done by
    now, and the routing service keeps the graphs it has. A tile that failed
    is built the next night - it is not current.
    """
    if not cfg.routing:
        return False
    jobs = os.environ.get("OTM_ROUTING_JOBS", "").strip()
    ok = True
    for args in (ROUTING_BUILD + (["--jobs", jobs] if jobs else []), ROUTING_COMPILE):
        log.info("routing: %s", " ".join(args[1:]))
        done = subprocess.run([sys.executable, *args], cwd=APP_ROOT)
        if done.returncode != 0:
            log.error("routing: %s exited %s", args[1], done.returncode)
            ok = False
    return ok


def run_once(cfg: Config) -> None:
    results = sync_regions(cfg)
    changed = [r.region.region_id for r in results if r.changed]
    log.info(
        "Synced %d region(s); %s",
        len(results),
        f"updated: {', '.join(changed)}" if changed else "nothing moved",
    )
    build_gol(cfg, results)
    build_routing(cfg)
