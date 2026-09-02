"""Build one bbox preview: the drawn rectangle as a ``.pmtiles`` file.

Runs in the tilesvc image, because that is where tilemaker and osmium live, and
takes its work from the preview queue (:mod:`otmlib.previewqueue`) rather than
from a schedule. The input is the same kept-current Geofabrik extract the
nightly tileset and the Garmin builds cut from, so a preview never downloads
anything the deployment was not already tracking.

    python -m tilesvc.preview      # the consumer, one worker
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from pathlib import Path

from otmlib import paths, previews, previewqueue, regionsync
from otmlib import tilemaker as runner
from otmlib.geofabrik import extract_bbox
from otmlib.proc import check_cancelled

from tilesvc import config as tilesvc_config
from tilesvc import tilemaker
from tilesvc.config import Config

log = logging.getLogger(__name__)

# The preview renders the same layers as the served tileset, so its config is
# derived from that one rather than checked in beside it: a layer added to the
# map would otherwise have to be remembered in two files.
CONFIG_SOURCE = tilemaker.CONFIG_REGION
# Only the zooms differ. Below 10 a drawn bbox carries no context worth
# building - the public basemap under it does that - and 14 is where the
# tileset ends.
MINZOOM = 10
MAXZOOM = 14
# How many previews stay on disk. Each is a cache of one look at one area, and
# a 50x50 km bbox is tens of megabytes.
KEEP_PREVIEWS = 8


def previews_dir(cfg: Config) -> Path:
    return paths.previews(cfg.data_dir)


def write_config(styles: Path, work: Path) -> Path:
    """The served tileset's config with the preview's zoom range."""
    config = json.loads((styles / CONFIG_SOURCE).read_text(encoding="utf-8"))
    config["settings"]["minzoom"] = MINZOOM
    config["settings"]["maxzoom"] = MAXZOOM
    config["settings"]["basezoom"] = MAXZOOM
    config["settings"]["name"] = "OpenTopoMap (превью области)"
    path = work / "tilemaker-config-preview.json"
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return path


def _sync_inputs(cfg: Config, preview: previews.Preview, log_fn) -> list[Path]:
    """The configured regions this bbox falls into, brought up to date.

    Only configured regions: a preview is offered for the covered area alone
    (garminsvc checks that before queueing), so anything else here would mean
    downloading a fresh multi-gigabyte extract on a button press.
    """
    from shapely.geometry import box

    west, south, east, north = preview.bbox
    area = box(west, south, east, north)
    regions = [
        region
        for region in regionsync.configured_regions(cfg.geofabrik_cache, cfg.geofabrik_base_url)
        if region.geometry.intersects(area)
    ]
    if not regions:
        raise RuntimeError("bbox is outside the configured regions")
    log_fn(f"Регионы: {', '.join(region.name for region in regions)}")
    results = regionsync.sync_regions(regions, cfg.geofabrik_cache, log_fn)
    return [result.pbf for result in results]


def build(preview_id: str, cfg: Config | None = None) -> None:
    """Take one queued preview all the way to a published .pmtiles."""
    cfg = cfg or tilesvc_config.load()
    preview = previews.get(preview_id)
    if preview is None:
        log.warning("Preview %s is gone, nothing to build", preview_id)
        return
    if preview.status == previews.DONE:
        log.info("Preview %s is already built", preview_id)
        return

    def log_fn(message: str) -> None:
        log.info("[%s] %s", preview_id[:8], message)
        previews.progress(preview_id, message)

    previews.start(preview_id)
    out_dir = previews_dir(cfg)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / f"{preview_id}.work"
    output = out_dir / f"{preview_id}.pmtiles"
    # Built inside the work directory, under a name that still ends in
    # .pmtiles: tilemaker picks its output format from the extension, and any
    # other suffix makes it write a directory of loose tiles instead.
    staging = work / "preview.pmtiles"
    try:
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)

        pbfs = _sync_inputs(cfg, preview, log_fn)
        check_cancelled()

        west, south, east, north = preview.bbox
        log_fn("Вырезаю область…")
        area_pbf = extract_bbox(pbfs, west, south, east, north, work / "preview.osm.pbf")

        log_fn(f"tilemaker: зумы {MINZOOM}–{MAXZOOM}…")
        styles = tilemaker.style_dir()
        runner.build(
            output=staging,
            config=write_config(styles, work),
            process=styles / runner.PROCESS_LUA,
            store_dir=work / "store",
            input_pbf=area_pbf,
            # Not a clip - the extract already did that - but the extent
            # tilemaker writes into the .pmtiles header. Without it the header
            # says [0,0,0,0], and MapLibre, reading coverage from there, asks
            # for no tiles at all and the preview renders empty.
            bbox=(west, south, east, north),
            cwd=styles.parent.parent,
        )
        if not staging.is_file() or staging.stat().st_size == 0:
            raise RuntimeError("tilemaker produced no preview")
        # Published by rename, from the work directory next to it: nginx serves
        # this directory, and a half-written file under the final name would
        # reach the browser as a broken tileset.
        staging.replace(output)
        size = output.stat().st_size
        previews.finish(
            preview_id,
            tiles_file=output.name,
            minzoom=MINZOOM,
            maxzoom=MAXZOOM,
            size_bytes=size,
        )
        log.info("Preview %s ready: %.1f MB", preview_id, size / 1e6)
    except Exception as exc:  # noqa: BLE001
        log.exception("Preview %s failed", preview_id)
        previews.fail(preview_id, str(exc))
    finally:
        shutil.rmtree(work, ignore_errors=True)
        previews.prune(KEEP_PREVIEWS, out_dir)


def recover() -> None:
    """Re-queue what a killed worker left behind, then anything still queued.

    huey keeps the task message only until a worker takes it, so a preview
    whose worker died is in the table as RUNNING with nothing left to run it.
    """
    moved = previews.requeue_running()
    if moved:
        log.info("Requeued %d interrupted preview(s)", moved)
    for preview_id in previews.queued_ids():
        previewqueue.enqueue(preview_id)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    previews.ensure_schema()
    recover()
    log.info("Preview worker ready, waiting for jobs")
    # One worker: tilemaker already uses every core it is given, so a second
    # preview building in parallel would only make both slower.
    consumer = previewqueue.huey.create_consumer(
        workers=1, periodic=False, worker_type="thread"
    )
    consumer.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
