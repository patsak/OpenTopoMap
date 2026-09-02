"""Run the tile job once: ``python -m tilesvc``.

Scheduling lives outside (supercronic in the tilesvc-job container), so a manual
run and a cron run are the same code path.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tilesvc import config, job

log = logging.getLogger("tilesvc.job")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="config.yaml (default: OTM_TILESVC_CONFIG)")
    parser.add_argument(
        "--sync-only",
        action="store_true",
        help="only bring the region PBFs and their tracked sequences up to date",
    )
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="rebuild both tilesets from scratch, ignoring the recorded build revisions",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = config.load(args.config)
    log.info("Regions: %s", ", ".join(r.geofabrik_id for r in cfg.regions))
    log.info("Data dir: %s", cfg.data_dir)

    started = time.monotonic()
    if args.sync_only:
        job.sync_regions(cfg)
    else:
        # The whole pass in one call, so a step added to the job cannot be missed
        # here — this is the entry point cron runs.
        job.run_once(cfg, recreate=args.recreate)
    log.info("Done in %.0fs", time.monotonic() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
