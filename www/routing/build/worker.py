"""What a parallel build process runs (routing.build --jobs).

Its own module: worker processes are spawned, and a function defined in the
``python -m routing.build`` main module is not importable from them.
"""

from __future__ import annotations

import logging
import os
import threading
import time

from routing import config
from routing.build import pipeline

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def init_logging() -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)


def init_worker() -> None:
    """A pool process: logging, and a watch on the parent. Killed, the parent
    leaves its workers running on (they are spawned, so a pkill on
    routing.build misses them too); they would go on building the same tiles
    as the next run, each wiping the other's work dir. So a worker whose
    parent is gone exits."""
    init_logging()
    parent = os.getppid()

    def watch() -> None:
        while True:
            time.sleep(2)
            if os.getppid() != parent:
                os._exit(1)

    threading.Thread(target=watch, name="parent-watch", daemon=True).start()


def build_one(cfg: config.Config, area: config.Area, pbfs, state_pbfs, opts: dict) -> tuple[str, bool, float]:
    started = time.monotonic()
    built = pipeline.build_area(cfg, area, pbfs=pbfs, state_pbfs=state_pbfs, **opts)
    return area.name, built, time.monotonic() - started
