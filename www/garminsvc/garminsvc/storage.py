"""Job records in Postgres (schema ``otm_garmin``, see ../sql/001_schema.sql).

The queue lives in the same database, in huey's own tables (see
:mod:`garminsvc.tasks`), so the service has one piece of state to back up and
one connection string to configure.
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from garminsvc.constants import (
    DATA_DIR,
    FAMILY_ID_CONTOURS,
    FAMILY_ID_MAP,
    FAMILY_ID_MAX,
    JOBS_DIR,
    PREVIEWS_DIR,
)
from otmlib import pg

SQL_DIR = Path(__file__).resolve().parent.parent / "sql"

_local = threading.local()

_COLUMNS = (
    "job_id, name, west, south, east, north, status, created_at, updated_at, "
    "message, log, geofabrik_urls, parts, zip_path, error, "
    "family_id_map, family_id_contours, source_pbf, owner_id"
)


def ensure_schema() -> None:
    """Create the schema and seed the family-id cursors. Idempotent."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    # nginx mounts this directory; created here so a fresh checkout does not
    # get one made by Docker as root.
    PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    pg.ensure_schema(SQL_DIR)
    with pg.connection() as conn:
        conn.execute(
            """
            INSERT INTO otm_garmin.family_id_seq (name, value)
            VALUES ('map', %s), ('contours', %s)
            ON CONFLICT (name) DO NOTHING
            """,
            (FAMILY_ID_MAP, FAMILY_ID_CONTOURS),
        )
        conn.commit()


def connect() -> psycopg.Connection:
    """This thread's connection, opened on first use and kept.

    One per thread rather than one per call: a running build saves the job once
    per log line, and the eight gunicorn threads poll it from the HTTP side, so
    a connect-and-close around every statement would be most of the cost of
    each. ``dict_row`` so ``row["name"]`` reads the way the API payload does.
    """
    conn = getattr(_local, "conn", None)
    if conn is not None and not conn.closed:
        return conn
    conn = pg.connect(row_factory=dict_row)
    _local.conn = conn
    return conn


@contextmanager
def _session():
    """A transaction on this thread's connection, rolled back if it fails.

    Without the rollback a failed statement leaves the connection in
    "current transaction is aborted", and — because the connection is reused —
    every later query on this thread would fail too.
    """
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except psycopg.Error:
            # Connection is gone; drop it so the next call reconnects.
            _local.conn = None
        raise


def _loads_list(value) -> list:
    """jsonb comes back decoded; tolerate a text column or a NULL as well."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        data = json.loads(value) if value else []
        return data if isinstance(data, list) else []
    return []


def upsert_job(job: object) -> None:
    with _session() as conn:
        conn.execute(
            f"""
            INSERT INTO otm_garmin.jobs ({_COLUMNS})
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (job_id) DO UPDATE SET
              name = EXCLUDED.name,
              west = EXCLUDED.west,
              south = EXCLUDED.south,
              east = EXCLUDED.east,
              north = EXCLUDED.north,
              status = EXCLUDED.status,
              created_at = EXCLUDED.created_at,
              updated_at = EXCLUDED.updated_at,
              message = EXCLUDED.message,
              log = EXCLUDED.log,
              geofabrik_urls = EXCLUDED.geofabrik_urls,
              parts = EXCLUDED.parts,
              zip_path = EXCLUDED.zip_path,
              error = EXCLUDED.error,
              family_id_map = EXCLUDED.family_id_map,
              family_id_contours = EXCLUDED.family_id_contours,
              source_pbf = EXCLUDED.source_pbf
            """,
            # owner_id is deliberately absent from the SET list: ownership is
            # write-once, set when the job is created, and must not be
            # reassigned by a later save from the worker.
            (
                job.job_id,
                job.name,
                job.west,
                job.south,
                job.east,
                job.north,
                job.status.value,
                job.created_at,
                job.updated_at,
                job.message,
                Jsonb(job.log),
                Jsonb(job.geofabrik_urls),
                job.parts,
                str(job.zip_path) if job.zip_path else None,
                job.error,
                job.family_id_map,
                job.family_id_contours,
                str(job.source_pbf) if job.source_pbf else None,
                getattr(job, "owner_id", "") or "",
            ),
        )


def row_to_job(row):
    from garminsvc.job import Job, JobStatus

    return Job(
        job_id=row["job_id"],
        name=row["name"] or "",
        west=row["west"],
        south=row["south"],
        east=row["east"],
        north=row["north"],
        status=JobStatus(row["status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        message=row["message"] or "",
        log=_loads_list(row["log"]),
        geofabrik_urls=_loads_list(row["geofabrik_urls"]),
        parts=int(row["parts"] or 0),
        zip_path=row["zip_path"],
        error=row["error"],
        family_id_map=int(row["family_id_map"] or 0),
        family_id_contours=int(row["family_id_contours"] or 0),
        source_pbf=row.get("source_pbf"),
        owner_id=row.get("owner_id") or "",
    )


def get_job(job_id: str):
    with _session() as conn:
        row = conn.execute(
            "SELECT * FROM otm_garmin.jobs WHERE job_id = %s", (job_id,)
        ).fetchone()
    return row_to_job(row) if row else None


def list_jobs(limit: int = 0):
    """Jobs newest first — the order ``retention.jobs_to_keep`` expects."""
    sql = "SELECT * FROM otm_garmin.jobs ORDER BY created_at DESC"
    params: tuple = ()
    if limit > 0:
        sql += " LIMIT %s"
        params = (limit,)
    with _session() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [row_to_job(row) for row in rows]


def delete_job(job_id: str) -> None:
    with _session() as conn:
        conn.execute("DELETE FROM otm_garmin.jobs WHERE job_id = %s", (job_id,))


def allocate_family_ids() -> tuple[int, int]:
    """Next unique 4-digit family-id pair (map + contours).

    ``SELECT … FOR UPDATE`` on the cursor rows serializes concurrent
    allocations: two jobs created at once would otherwise read the same cursor
    and hand the same id to both maps, which a device rejects.
    """
    with _session() as conn:
        used: set[int] = set()
        for row in conn.execute(
            "SELECT family_id_map, family_id_contours FROM otm_garmin.jobs"
        ).fetchall():
            if row["family_id_map"]:
                used.add(int(row["family_id_map"]))
            if row["family_id_contours"]:
                used.add(int(row["family_id_contours"]))

        def take(kind: str, start: int) -> int:
            row = conn.execute(
                "SELECT value FROM otm_garmin.family_id_seq WHERE name = %s FOR UPDATE",
                (kind,),
            ).fetchone()
            candidate = int(row["value"]) if row else start
            if candidate < start:
                candidate = start
            while candidate in used or candidate < 1 or candidate > FAMILY_ID_MAX:
                candidate += 1
                if candidate > FAMILY_ID_MAX:
                    candidate = 1000
                if candidate == start:
                    raise RuntimeError("No free Garmin family-id left")
            used.add(candidate)
            conn.execute(
                "UPDATE otm_garmin.family_id_seq SET value = %s WHERE name = %s",
                (candidate + 1, kind),
            )
            return candidate

        map_id = take("map", FAMILY_ID_MAP)
        contours_id = take("contours", FAMILY_ID_CONTOURS)
    return map_id, contours_id


def count_by_status(status: str) -> int:
    with _session() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM otm_garmin.jobs WHERE status = %s", (status,)
        ).fetchone()
    return int(row["n"] if row else 0)
