"""Shared PostgreSQL connection helpers for tilesvc and garminsvc."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg

DEFAULT_DATABASE_URL = "postgresql://otm:otm@localhost:5432/otm"


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
    for path in files:
        conn.execute(path.read_text(encoding="utf-8"))
    conn.commit()


def ensure_schema(sql_dir: Path) -> None:
    with connection() as conn:
        run_sql_files(conn, sql_dir)
