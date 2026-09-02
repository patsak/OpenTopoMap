"""The preview queue: huey, in Postgres, separate from the Garmin build queue.

Its own queue rather than a second task on garminsvc's: that consumer runs one
worker, and a preview enqueued behind a full .img build would wait out the
whole build before it started. Here the producer is garminsvc (the HTTP
handler) and the consumer is the ``tilesvc-preview`` service, which has
tilemaker in its image — so the task body must not import anything from
either service at module level.
"""

from __future__ import annotations

import logging

from huey import PostgresHuey

from otmlib import pg

log = logging.getLogger(__name__)

QUEUE_NAME = "otm-preview"

# results=False for the same reason as garminsvc's queue: the outcome is read
# from otm.map_previews, and huey holding a copy would only be a second truth.
huey = PostgresHuey(name=QUEUE_NAME, dsn=pg.database_url(), results=False)


@huey.task()
def build_preview(preview_id: str) -> None:
    """Consumer side. Imported lazily: garminsvc enqueues this task but has no
    tilesvc package in its image, and would fail to import the module."""
    from tilesvc.preview import build

    build(preview_id)


def enqueue(preview_id: str) -> None:
    log.info("Queued preview %s", preview_id)
    build_preview(preview_id)
