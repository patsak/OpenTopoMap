"""A throwaway Postgres database for the tests that need one.

Both services keep real state in Postgres now (tile metadata, Garmin job
records, the huey queue), and the SQL in those modules is worth running rather
than mocking. Set ``DATABASE_URL`` to an existing server and the fixture
creates a fresh database per session against it; leave it unset and the tests
that ask for one skip, so a plain ``pytest -q`` still works with nothing
installed.

    DATABASE_URL=postgresql://otm:otm@localhost:5432/otm pytest -q
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

WWW = Path(__file__).resolve().parent
# otmlib/sql holds what both services share (the preview records).
SCHEMA_DIRS = (WWW / "otmlib/sql", WWW / "tilesvc/sql", WWW / "garminsvc/sql")


def _with_dbname(url: str, dbname: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{dbname}"))


@pytest.fixture(scope="session")
def pgUrl() -> str:
    """DSN of a scratch database with both schemas applied.

    A whole database rather than a schema: huey's Postgres backend creates its
    own tables in ``public`` and drops nothing, so isolating by database is the
    only way a run cannot leave anything behind in a real deployment's server.
    """
    admin = os.environ.get("DATABASE_URL", "").strip()
    if not admin:
        pytest.skip("DATABASE_URL is not set; skipping tests that need Postgres")
    psycopg = pytest.importorskip("psycopg")

    dbname = f"otm_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{dbname}"')
    url = _with_dbname(admin, dbname)
    try:
        with psycopg.connect(url) as conn:
            for directory in SCHEMA_DIRS:
                for path in sorted(directory.glob("*.sql")):
                    conn.execute(path.read_text(encoding="utf-8"))
            conn.commit()
        yield url
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')


@pytest.fixture()
def pgDatabase(pgUrl, monkeypatch):
    """Point the code under test at the scratch database and empty it first.

    Truncation rather than a per-test database: creating one costs about a
    second, and every table here is small enough that the tests can share one.
    """
    monkeypatch.setenv("DATABASE_URL", pgUrl)
    import psycopg

    with psycopg.connect(pgUrl) as conn:
        conn.execute("TRUNCATE otm_garmin.jobs, otm_garmin.family_id_seq")
        conn.execute("TRUNCATE otm.replication_state, otm.regions, otm.tile_state")
        conn.execute("TRUNCATE otm.map_previews")
        conn.commit()
    return pgUrl
