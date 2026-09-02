"""Keep cached Geofabrik PBFs current, tracking the applied sequence in Postgres.

This is the join between the two halves of the pipeline: :mod:`otmlib.geofabrik`
knows how to fetch and rewrite files but nothing about a database, and
:mod:`otmlib.pgmeta` is the reverse. Both tilesvc (its configured regions, as
tilemaker input) and garminsvc (the leaf regions a bbox needs) sync through
here, so a region shared by the two is downloaded, updated and tracked once.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from shapely.ops import unary_union

from otmlib import geofabrik, pgmeta
from otmlib.filelock import exclusive
from otmlib.geofabrik import LogFn, Region

log = logging.getLogger(__name__)

# Past this many pending diffs, re-downloading the whole extract beats applying
# them one by one: each osmium apply-changes pass rewrites the entire PBF, so N
# diffs cost N full rewrites. Roughly "the region has been untouched for half a
# year"; a fresh -latest.osm.pbf is one download and one rewrite.
MAX_OSC_CATCHUP = 200


@dataclass(frozen=True)
class SyncResult:
    region: Region
    pbf: Path
    # The applied replication sequence, or None when Geofabrik publishes no
    # replication stream for this extract and it can only be re-downloaded.
    sequence: int | None
    changed: bool

    @property
    def revision(self) -> str:
        """Stable identity of this region's current data, for tile_state."""
        return f"{self.region.region_id}@{self.sequence if self.sequence is not None else 'unknown'}"


def sync_region(region: Region, cache_dir: Path, log_fn: LogFn | None = None) -> SyncResult:
    """Bring *region*'s cached PBF up to Geofabrik's latest published sequence.

    The PBF is downloaded once and then rewritten in place by
    ``osmium apply-changes``; ``otm.replication_state`` is advanced only after
    a rewrite succeeds, so an interrupted run retries the same range rather
    than silently skipping it.

    Three processes share this cache — the nightly tile job, a Garmin build and
    a preview — and the rewrite is in place, so the whole sync of one region is
    held under a lock file. The others wait for the result instead of applying
    the same diffs to the same file at the same time.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    lock = cache_dir / f"{region.region_id.replace('/', '_')}.sync.lock"
    with exclusive(lock):
        return _sync_region(region, cache_dir, log_fn)


def _sync_region(region: Region, cache_dir: Path, log_fn: LogFn | None = None) -> SyncResult:
    pbf = geofabrik.download_full_pbf(region, cache_dir, log_fn)
    region_id = region.region_id

    known = pgmeta.get_replication_state(region_id)
    if known is None:
        known = geofabrik.pbf_sequence(pbf)
        if known is None:
            log.warning(
                "%s: no replication sequence in the PBF header or sidecar; "
                "the extract can only be refreshed by re-downloading it",
                region_id,
            )
            pgmeta.upsert_region(region_id, region.name, region.geometry.bounds)
            return SyncResult(region=region, pbf=pbf, sequence=None, changed=True)
        pgmeta.set_replication_state(region_id, known)

    try:
        latest, _ = geofabrik.fetch_latest_sequence(region.updates_url)
    except Exception as exc:  # noqa: BLE001
        if not geofabrik.is_not_found_error(exc):
            raise
        log.info("%s: no replication stream published; keeping the cached extract", region_id)
        pgmeta.upsert_region(region_id, region.name, region.geometry.bounds)
        return SyncResult(region=region, pbf=pbf, sequence=known, changed=False)

    pgmeta.upsert_region(region_id, region.name, region.geometry.bounds)

    if latest <= known:
        log.info("%s: up to date at sequence %d", region_id, known)
        return SyncResult(region=region, pbf=pbf, sequence=known, changed=False)

    pending = latest - known
    if pending > MAX_OSC_CATCHUP:
        log.info(
            "%s: %d diffs behind (> %d), re-downloading the full extract instead",
            region_id,
            pending,
            MAX_OSC_CATCHUP,
        )
        pbf = geofabrik.refetch_full_pbf(region, cache_dir, log_fn)
        sequence = geofabrik.pbf_sequence(pbf) or latest
        pgmeta.set_replication_state(region_id, sequence)
        return SyncResult(region=region, pbf=pbf, sequence=sequence, changed=True)

    updates = geofabrik.updates_dir(cache_dir, region)
    log.info("%s: applying %d diff(s), %d..%d", region_id, pending, known + 1, latest)
    try:
        osc_files = geofabrik.download_osc_range(
            region.updates_url, known + 1, latest, updates, log_fn
        )
    except Exception as exc:  # noqa: BLE001
        if not geofabrik.is_not_found_error(exc):
            raise
        # Geofabrik keeps only a bounded window of diffs; past it the only way
        # forward is a fresh extract.
        log.info("%s: diff %d is gone, re-downloading the full extract", region_id, known + 1)
        pbf = geofabrik.refetch_full_pbf(region, cache_dir, log_fn)
        sequence = geofabrik.pbf_sequence(pbf) or latest
        pgmeta.set_replication_state(region_id, sequence)
        return SyncResult(region=region, pbf=pbf, sequence=sequence, changed=True)

    geofabrik.apply_osc_files(pbf, osc_files)
    pgmeta.set_replication_state(region_id, latest)
    geofabrik.retain_last_osc(updates, latest)
    return SyncResult(region=region, pbf=pbf, sequence=latest, changed=True)


def sync_regions(
    regions: list[Region], cache_dir: Path, log_fn: LogFn | None = None
) -> list[SyncResult]:
    return [sync_region(region, cache_dir, log_fn) for region in regions]


def tileset_revision(results: list[SyncResult]) -> str:
    """One string identifying the exact inputs a tileset would be built from."""
    return " ".join(sorted(r.revision for r in results))


def configured_regions(
    cache_dir: Path, base_url: str = geofabrik.GEOFABRIK_BASE_URL
) -> list[Region]:
    """The regions the deployment covers, as Geofabrik regions with geometry.

    ``otm.regions`` is kept equal to tilesvc's config.yaml by prune_regions(),
    so this reads that list back and re-attaches the polygons from the cached
    index — which is how garminsvc, which never sees config.yaml, can tell
    whether a bbox is inside the covered area.
    """
    ids = {region_id for region_id, _ in pgmeta.list_regions()}
    if not ids:
        return []
    return [
        region
        for region in geofabrik.load_regions(cache_dir=cache_dir, base_url=base_url)
        if region.region_id in ids
    ]


def bbox_coverage_gap(
    west: float,
    south: float,
    east: float,
    north: float,
    cache_dir: Path,
    base_url: str = geofabrik.GEOFABRIK_BASE_URL,
) -> str:
    """"" when *bbox* lies inside the configured regions, else why it does not.

    Previews are only offered for the configured regions: outside them there is
    no kept-current PBF to cut, and building one would mean downloading a whole
    new extract on a button press.
    """
    from shapely.geometry import box

    regions = configured_regions(cache_dir, base_url)
    if not regions:
        return "регионы не настроены — запустите tilesvc-job"
    area = box(min(west, east), min(south, north), max(west, east), max(south, north))
    covered = unary_union([region.geometry for region in regions])
    if covered.contains(area):
        return ""
    names = ", ".join(sorted(region.name for region in regions))
    return f"область выходит за пределы регионов сервиса ({names})"
