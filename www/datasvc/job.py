"""The scheduled pass: keep the configured Geofabrik extracts current.

Geofabrik publishes daily diffs, so on a typical night this applies a handful
of ``.osc.gz`` files to each cached PBF and records how far it got. Nothing is
rendered here: tiles are built per drawn bbox, on demand, by
:mod:`datasvc.preview`, out of exactly these files.

Then, as a step of its own, the GOL www/overpass answers from is brought up to
the same files (:mod:`otmlib.gol`) - updated with the same diffs, or rebuilt -
here rather than by something watching the extracts, because only this pass
knows when it has finished writing them.

Last, the hiking router's graphs are built out of the same files
(:func:`build_routing`), for the same reason: only this pass knows the
extracts are final.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from otmlib import gol, pg, pgmeta, regionsync
from otmlib.filelock import exclusive
from otmlib.geofabrik import download_osc_range, region_by_id

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


# Past this many diffs behind in one region, a GOL is rebuilt rather than
# updated: Geofabrik keeps only a window of them, and gol holds a whole update
# in memory (~750 MB per day of the planet's changes, by its author).
GOL_MAX_UPDATE_DIFFS = 60


def build_gol(cfg: Config, results: list[regionsync.SyncResult]) -> bool:
    """Bring ``cfg.gol_path`` up to the synced extracts. Returns whether it
    changed.

    Most nights by ``gol update`` with the diffs between the sequences the GOL
    holds and those the sync reached; a build from the extracts when there is
    no GOL to update, the regions changed, a region is too far behind or went
    back (a fresh download), ``cfg.gol_rebuild_days`` have passed since the
    last build, or the update failed.

    Asked of the recorded sequences rather than of ``SyncResult.changed``: a
    night whose step failed, or a GOL deleted by hand, is made good by the
    next run even when no diff moved.
    """
    target = cfg.gol_path
    if target is None:
        return False
    pbfs = [r.pbf for r in results]
    snapshot = [_extract_snapshot(cfg, r) for r in results]
    sequences = {r.pbf.name: seq for r, (seq, _) in zip(results, snapshot)}
    sources = [fp for _, fp in snapshot]

    if None in sequences.values():
        # An extract without a replication stream: only rebuilds follow it.
        if gol.is_current(pbfs, target):
            log.info("GOL %s is current", target.name)
            return False
        gol.build(pbfs, target)
        return True

    state = gol.read_state(target) if target.is_file() else None
    reason = _rebuild_reason(state, sequences)
    if reason is None:
        behind = {name: seq for name, seq in sequences.items() if seq != state.sequences[name]}
        if not behind:
            log.info("GOL %s is current", target.name)
            return False
        reason = _rebuild_due(cfg, state)
        if reason is None:
            try:
                _update_gol(target, results, state, sequences, sources)
                return True
            except Exception:  # noqa: BLE001
                log.exception("GOL: the update failed")
                reason = "the update failed"
    log.info("GOL: building from the extracts: %s", reason)
    gol.build(pbfs, target, sequences=sequences, expected_sources=sources)
    return True


def _extract_snapshot(cfg: Config, result: regionsync.SyncResult) -> tuple[int | None, str]:
    """The sequence an extract is at and its fingerprint, read as one: under
    the lock a sync holds while it rewrites the PBF and moves the sequence.
    mapsvc syncs the same files during the day."""
    with exclusive(regionsync.lock_path(result.region, cfg.geofabrik_cache)):
        sequence = pgmeta.get_replication_state(result.region.region_id)
        return sequence, gol.fingerprint([result.pbf])[0]


def _rebuild_reason(state: gol.State | None, sequences: dict[str, int]) -> str | None:
    """Why the GOL cannot be updated to *sequences*, or None if it can."""
    if state is None:
        return "there is no GOL yet"
    if not state.sequences:
        return "the GOL records no sequences to update from"
    if set(state.sequences) != set(sequences):
        return "the regions changed"
    for name, seq in sequences.items():
        held = state.sequences[name]
        if seq < held:
            return f"{name} went back from sequence {held} to {seq}"
        if seq - held > GOL_MAX_UPDATE_DIFFS:
            return f"{name} is {seq - held} diffs behind"
    return None


def _rebuild_due(cfg: Config, state: gol.State) -> str | None:
    if state.built is None:
        return "the GOL does not say when it was built"
    built = datetime.strptime(state.built, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - built >= timedelta(days=cfg.gol_rebuild_days):
        return f"it was last built {state.built}, more than {cfg.gol_rebuild_days} day(s) ago"
    return None


def _update_gol(
    target: Path,
    results: list[regionsync.SyncResult],
    state: gol.State,
    sequences: dict[str, int],
    sources: list[str],
) -> None:
    """Fetch the diffs the GOL is missing and apply them.

    Downloaded again rather than taken from the cache: the sync keeps only the
    last diff of a region there, and a sync by mapsvc may already have dropped
    the ones the GOL needs. They are small - a federal district's day is a few
    hundred kilobytes.
    """
    diffs_dir = Path(tempfile.mkdtemp(dir=gol.work_dir() or target.parent, prefix=".gol-diffs-"))
    try:
        changes = {}
        for r in results:
            name = r.pbf.name
            held, seq = state.sequences[name], sequences[name]
            if seq == held:
                continue
            files = download_osc_range(r.region.updates_url, held + 1, seq, diffs_dir / name)
            changes[name] = (seq, files)
        gol.update(target, changes, sources=sources)
    finally:
        shutil.rmtree(diffs_dir, ignore_errors=True)


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
