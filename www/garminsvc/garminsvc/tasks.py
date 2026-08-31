"""Huey tasks. Queue is a separate SQLite file from job records."""

from __future__ import annotations

from huey import SqliteHuey

from garminsvc.constants import DATA_DIR, HUEY_DB

DATA_DIR.mkdir(parents=True, exist_ok=True)
huey = SqliteHuey(name="garminsvc", filename=str(HUEY_DB), results=False)


@huey.task()
def build_map(job_id: str) -> None:
    from garminsvc.jobs import job_manager

    job_manager.run_job(job_id)
