"""Shared PostgreSQL connection helpers for datasvc and garminsvc."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg

DEFAULT_DATABASE_URL = "postgresql://otm:otm@localhost:5432/otm"

# Tables both services read or write: previews, regions, replication state.
SHARED_SQL_DIR = Path(__file__).resolve().parent / "sql"


def database_url() -> str:
    return os.environ.get("DATABASE_URL", "").strip() or DEFAULT_DATABASE_URL


def connect(**kwargs) -> psycopg.Connection:
    return psycopg.connect(database_url(), **kwargs)


@contextmanager
def connection(**kwargs) -> Iterator[psycopg.Connection]:
    with connect(**kwargs) as conn:
        yield conn


def run_sql_files(conn: psycopg.Connection, directory: Path) -> None:
    """Apply numbered ``*.sql`` files in *directory* (001_…, 002_…)."""
    files = sorted(directory.glob("*.sql"))
    if not files:
        # A wrong path would otherwise "succeed" and leave the tables missing.
        raise FileNotFoundError(f"no *.sql files in {directory}")
    for path in files:
        conn.execute(path.read_text(encoding="utf-8"))
    conn.commit()


def ensure_schema(sql_dir: Path) -> None:
    with connection() as conn:
        run_sql_files(conn, sql_dir)


def ensure_shared_schema() -> None:
    """Create the otmlib-owned tables. Every service calls this on start."""
    ensure_schema(SHARED_SQL_DIR)
