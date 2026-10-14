"""Build routing graphs: ``python -m routing.build [--area NAME ... | --all]``.

A NAME is an area, a tile, or a region (an area with tile_km): a region is
planned into tiles first (routing.build.tiles) and every tile is built. Without
--force an area is skipped while its package is current - built from the same
extracts with the same parameters - so the nightly run costs nothing on a
night without diffs. Tiles build in parallel with --jobs. Scheduling lives
outside (supercronic in the routing-build container), so a manual run and a
cron run are the same code.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from routing import config  # noqa: E402
from routing.build import pipeline, worker  # noqa: E402

log = logging.getLogger("routing.build")
_logging = worker.init_logging
_build_one = worker.build_one


def targets(cfg: config.Config, names: list[str] | None, args) -> tuple[config.Config, list[tuple]]:
    """(area, pbfs, state_pbfs) to build; plans regions on the way."""
    from routing.build import tiles

    wanted = names or [a.name for a in cfg.areas if not a.region] + [r.name for r in cfg.regions]
    out: list[tuple] = []
    for name in wanted:
        region = cfg.region(name)
        if region is None:
            area = cfg.area(name)
            if area.region is not None or args.plan_only:  # a tile asked for by name
                out.append((area, args.pbf, None))
                continue
            # a plain area is cut like a region of one: the night's check
            # reads a few MB, not the district
            sources = args.pbf or pipeline.source_pbfs(cfg, area)
            out.append((area, [pipeline.region_extract(cfg, area, sources)], sources))
            continue
        if args.replan or not cfg.tiles_of(name):
            tiles.plan(cfg, region)
            cfg = config.load(args.config)  # the plan's tiles are areas now
        if args.plan_only:
            out += [(tile, None, None) for tile in cfg.tiles_of(name)]
            continue
        sources = args.pbf or pipeline.source_pbfs(cfg, region)
        cut = pipeline.region_extract(cfg, region, sources)
        out += [(tile, [cut], sources) for tile in cfg.tiles_of(name)]
    return cfg, out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, help="config.yaml (default: OTM_ROUTING_CONFIG)")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--area", action="append", help="area, tile or region name (repeatable)")
    which.add_argument("--all", action="store_true", help="every configured area and region")
    parser.add_argument("--pbf", type=Path, action="append",
                        help="cut from this PBF instead of the synced regions (repeatable)")
    parser.add_argument("--force", action="store_true", help="rebuild even if current")
    parser.add_argument("--replan", action="store_true", help="lay the regions' tiles out again")
    parser.add_argument("--plan-only", action="store_true", help="plan the regions, build nothing")
    parser.add_argument("--jobs", type=int, default=int(os.environ.get("OTM_ROUTING_JOBS", "1")),
                        help="areas built at once (default: OTM_ROUTING_JOBS or 1)")
    parser.add_argument("--no-debug", action="store_true", help="skip debug.gpkg")
    parser.add_argument("--work-dir", type=Path, help="intermediates (one area only)")
    parser.add_argument("--keep-work", action="store_true", help="do not delete the work dir")
    args = parser.parse_args(argv)

    _logging()
    cfg = config.load(args.config)
    cfg, todo = targets(cfg, None if args.all else args.area, args)
    log.info("Data dir: %s; %s area(s) to check", cfg.data_dir, len(todo))
    if args.plan_only:
        for area, _, _ in todo:
            log.info("  %s %s", area.name, [round(v, 4) for v in area.bbox])
        return 0

    opts = {"work_dir": args.work_dir if len(todo) == 1 else None, "keep_work": args.keep_work,
            "debug": not args.no_debug, "force": args.force}
    failed = built = done = 0
    started = time.monotonic()
    if args.jobs <= 1:
        for area, pbfs, state in todo:
            try:
                _, ok, _ = _build_one(cfg, area, pbfs, state, opts)
                built += ok
            except Exception:  # noqa: BLE001 - one broken area must not stop the others
                failed += 1
                log.exception("%s: build failed", area.name)
            done += 1
    else:
        with ProcessPoolExecutor(max_workers=args.jobs, initializer=worker.init_worker) as pool:
            futures = {pool.submit(_build_one, cfg, area, pbfs, state, opts): area.name
                       for area, pbfs, state in todo}
            for future in as_completed(futures):
                done += 1
                try:
                    name, ok, seconds = future.result()
                    built += ok
                    log.info("[%s/%s] %s %s in %.0fs", done, len(todo), name,
                             "built" if ok else "current", seconds)
                except Exception:  # noqa: BLE001
                    failed += 1
                    log.exception("[%s/%s] %s: build failed", done, len(todo), futures[future])
    log.info("%s built, %s current, %s failed in %.0f min", built, done - built - failed, failed,
             (time.monotonic() - started) / 60)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
