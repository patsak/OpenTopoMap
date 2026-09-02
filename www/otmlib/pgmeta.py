"""Tile-pipeline metadata in Postgres: replication position, regions, builds.

The tables are defined in ``www/tilesvc/sql/001_schema.sql``; that file's header
explains why they are the only thing this pipeline keeps in a database. Every
function here takes an optional open connection so a caller can do a whole
region's read-apply-write inside one transaction, and opens its own otherwise.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from otmlib import pg

DEFAULT_CENTER_ZOOM = 6


@contextmanager
def _conn(conn=None) -> Iterator:
    """Reuse a caller's connection, or open (and commit) one of our own."""
    if conn is not None:
        yield conn
        return
    with pg.connection() as own:
        yield own
        own.commit()


def get_replication_state(region_id: str, *, conn=None) -> int | None:
    """The last ``.osc.gz`` sequence applied to *region_id*'s PBF, if any."""
    with _conn(conn) as c:
        row = c.execute(
            "SELECT sequence_number FROM otm.replication_state WHERE region_id = %s",
            (region_id,),
        ).fetchone()
    return int(row[0]) if row else None


def set_replication_state(region_id: str, sequence: int, *, conn=None) -> None:
    with _conn(conn) as c:
        c.execute(
            """
            INSERT INTO otm.replication_state (region_id, sequence_number, updated_at)
            VALUES (%s, %s, now())
            ON CONFLICT (region_id) DO UPDATE
            SET sequence_number = EXCLUDED.sequence_number, updated_at = now()
            """,
            (region_id, sequence),
        )


def clear_replication_state(region_id: str, *, conn=None) -> None:
    """Forget a region's position, so the next sync re-bootstraps from the PBF."""
    with _conn(conn) as c:
        c.execute("DELETE FROM otm.replication_state WHERE region_id = %s", (region_id,))


def upsert_region(region_id: str, name: str, bounds: tuple[float, float, float, float], *, conn=None) -> None:
    west, south, east, north = bounds
    with _conn(conn) as c:
        c.execute(
            """
            INSERT INTO otm.regions (region_id, name, west, south, east, north, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (region_id) DO UPDATE
            SET name = EXCLUDED.name,
                west = EXCLUDED.west, south = EXCLUDED.south,
                east = EXCLUDED.east, north = EXCLUDED.north,
                updated_at = now()
            """,
            (region_id, name, west, south, east, north),
        )


def prune_regions(keep_region_ids: list[str], *, conn=None) -> None:
    """Drop rows for regions no longer in config.yaml, so coverage follows it."""
    with _conn(conn) as c:
        if keep_region_ids:
            c.execute("DELETE FROM otm.regions WHERE region_id <> ALL(%s)", (keep_region_ids,))
        else:
            c.execute("DELETE FROM otm.regions")


def list_regions(*, conn=None) -> list[tuple[str, str]]:
    """``(region_id, name)`` of every region currently in the tileset config.

    The row set is kept equal to config.yaml by prune_regions(), so this is how
    a service without that file — garminsvc — learns which regions the
    deployment covers.
    """
    with _conn(conn) as c:
        rows = c.execute("SELECT region_id, name FROM otm.regions ORDER BY region_id").fetchall()
    return [(row[0], row[1]) for row in rows]


def coverage_bbox(*, conn=None) -> tuple[float, float, float, float] | None:
    """Bounding box of every region in the tileset, or None when there are none."""
    with _conn(conn) as c:
        row = c.execute(
            "SELECT min(west), min(south), max(east), max(north) FROM otm.regions"
        ).fetchone()
    if not row or row[0] is None:
        return None
    return (float(row[0]), float(row[1]), float(row[2]), float(row[3]))


def coverage_center() -> dict:
    """``{"center": [lat, lon], "zoom": n}`` for the map's initial view.

    Shaped for MapLibre (lat first, as ``vectorbasemap.config()`` passes it
    straight through) and empty when nothing has been imported yet. A database
    that is down raises, as everywhere else here; the caller that can carry on
    without an opening position is the one that catches (see
    ``garminsvc.vectorbasemap._map_center``).
    """
    bounds = coverage_bbox()
    if bounds is None:
        return {}
    west, south, east, north = bounds
    return {
        "center": [(south + north) / 2, (west + east) / 2],
        "zoom": DEFAULT_CENTER_ZOOM,
    }


def get_tile_state(tileset: str, *, conn=None) -> str | None:
    """The ``source_revision`` the current *tileset* file was built from."""
    with _conn(conn) as c:
        row = c.execute(
            "SELECT source_revision FROM otm.tile_state WHERE tileset = %s", (tileset,)
        ).fetchone()
    return str(row[0]) if row else None


def set_tile_state(tileset: str, source_revision: str, *, conn=None) -> None:
    with _conn(conn) as c:
        c.execute(
            """
            INSERT INTO otm.tile_state (tileset, source_revision, built_at)
            VALUES (%s, %s, now())
            ON CONFLICT (tileset) DO UPDATE
            SET source_revision = EXCLUDED.source_revision, built_at = now()
            """,
            (tileset, source_revision),
        )
