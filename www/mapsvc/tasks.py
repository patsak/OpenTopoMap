"""Huey tasks. The queue lives in Postgres, alongside the job records."""

from __future__ import annotations

from huey import PostgresHuey

from otmlib import pg


huey = PostgresHuey(name="mapsvc", dsn=pg.database_url(), results=False)


@huey.task()
def build_map(job_id: str) -> None:
    from mapsvc.jobs import job_manager

    job_manager.run_job(job_id)
