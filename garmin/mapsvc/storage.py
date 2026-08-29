"""SQLite store shared by Huey (queue) and job records."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from mapsvc.constants import DATA_DIR, FAMILY_ID_CONTOURS, FAMILY_ID_MAP, FAMILY_ID_MAX, JOBS_DB, JOBS_DIR

_local = threading.local()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  west REAL NOT NULL,
  south REAL NOT NULL,
  east REAL NOT NULL,
  north REAL NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  message TEXT NOT NULL DEFAULT '',
  log TEXT NOT NULL DEFAULT '[]',
  geofabrik_urls TEXT NOT NULL DEFAULT '[]',
  parts INTEGER NOT NULL DEFAULT 0,
  zip_path TEXT,
  error TEXT,
  family_id_map INTEGER NOT NULL DEFAULT 0,
  family_id_contours INTEGER NOT NULL DEFAULT 0,
  source_pbf TEXT,
  owner_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS family_id_seq (
  name TEXT PRIMARY KEY,
  value INTEGER NOT NULL
);
"""


def db_path() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    return JOBS_DB


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    path = db_path()
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # Autocommit: a leftover read transaction on a leaked thread would block Huey
    # BEGIN EXCLUSIVE if the queue ever shared this file again.
    conn.isolation_level = None
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    _local.conn = conn
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "family_id_map" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN family_id_map INTEGER NOT NULL DEFAULT 0")
    if "family_id_contours" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN family_id_contours INTEGER NOT NULL DEFAULT 0")
    if "source_pbf" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN source_pbf TEXT")
    if "owner_id" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''")
    conn.execute(
        "INSERT OR IGNORE INTO family_id_seq (name, value) VALUES ('map', ?), ('contours', ?)",
        (FAMILY_ID_MAP, FAMILY_ID_CONTOURS),
    )
    conn.commit()


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads_list(raw: str | None) -> list:
    if not raw:
        return []
    data = json.loads(raw)
    return data if isinstance(data, list) else []


def upsert_job(job: object) -> None:
    conn = connect()
    conn.execute(
        """
        INSERT INTO jobs (
          job_id, name, west, south, east, north, status, created_at, updated_at,
          message, log, geofabrik_urls, parts, zip_path, error,
          family_id_map, family_id_contours, source_pbf, owner_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id) DO UPDATE SET
          name=excluded.name,
          west=excluded.west,
          south=excluded.south,
          east=excluded.east,
          north=excluded.north,
          status=excluded.status,
          created_at=excluded.created_at,
          updated_at=excluded.updated_at,
          message=excluded.message,
          log=excluded.log,
          geofabrik_urls=excluded.geofabrik_urls,
          parts=excluded.parts,
          zip_path=excluded.zip_path,
          error=excluded.error,
          family_id_map=excluded.family_id_map,
          family_id_contours=excluded.family_id_contours,
          source_pbf=excluded.source_pbf
        """,
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
            _dumps(job.log),
            _dumps(job.geofabrik_urls),
            job.parts,
            job.zip_path,
            job.error,
            job.family_id_map,
            job.family_id_contours,
            job.source_pbf,
            getattr(job, "owner_id", "") or "",
        ),
    )
    conn.commit()


def row_to_job(row: sqlite3.Row):
    from mapsvc.job import Job, JobStatus

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
        source_pbf=row["source_pbf"] if "source_pbf" in row.keys() else None,
        owner_id=(row["owner_id"] or "") if "owner_id" in row.keys() else "",
    )


def get_job(job_id: str):
    row = connect().execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return row_to_job(row) if row else None


def list_jobs(limit: int = 0):
    sql = "SELECT * FROM jobs ORDER BY created_at DESC"
    params: tuple = ()
    if limit > 0:
        sql += " LIMIT ?"
        params = (limit,)
    return [row_to_job(row) for row in connect().execute(sql, params)]


def delete_job(job_id: str) -> None:
    conn = connect()
    conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
    conn.commit()


def allocate_family_ids() -> tuple[int, int]:
    """Next unique 4-digit family-id pair (map + contours), persisted in SQLite."""
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    used = set()
    for row in conn.execute("SELECT family_id_map, family_id_contours FROM jobs"):
        if row["family_id_map"]:
            used.add(int(row["family_id_map"]))
        if row["family_id_contours"]:
            used.add(int(row["family_id_contours"]))

    def _take(kind: str, start: int) -> int:
        row = conn.execute("SELECT value FROM family_id_seq WHERE name = ?", (kind,)).fetchone()
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
        conn.execute("UPDATE family_id_seq SET value = ? WHERE name = ?", (candidate + 1, kind))
        return candidate

    map_id = _take("map", FAMILY_ID_MAP)
    contours_id = _take("contours", FAMILY_ID_CONTOURS)
    conn.commit()
    return map_id, contours_id


def count_by_status(status: str) -> int:
    row = connect().execute("SELECT COUNT(*) AS n FROM jobs WHERE status = ?", (status,)).fetchone()
    return int(row["n"] if row else 0)
