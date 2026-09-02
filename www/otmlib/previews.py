"""Preview records: the state behind the picker's "Preview" button.

A preview is one bbox rendered with the same cartography the web map uses, as
a single ``.pmtiles`` file nginx serves. This module owns the row; the queue is
huey's (:mod:`otmlib.previewqueue`) and the building is
:mod:`tilesvc.preview`. Both services import this — garminsvc to create rows
and report them, the worker to advance them — so nothing here may import
either.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from otmlib import pg

log = logging.getLogger(__name__)

SQL_DIR = Path(__file__).resolve().parent / "sql"

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
ERROR = "error"

# Bbox coordinates are rounded to this many decimals before they are stored, so
# that "the same area" is a plain equality check when looking for a preview to
# reuse. 5 decimals is ~1 m: finer than anything a drawn rectangle means.
BBOX_DECIMALS = 5

# How long a built preview stays usable, counted from when it was requested.
# A preview is a cache of one look at the map, so it goes stale in two ways
# that a keep-newest-N cap does not catch: the region PBF it was cut from has
# had a night of diffs applied since, and the cartography it was rendered with
# may have moved on. Past the TTL the same rectangle is built again instead of
# reused (find_ready), garminsvc stops handing out its URL, and the row and its
# file go on the next sweep (expire).
TTL_SECONDS = 24 * 3600

_COLUMNS = (
    "preview_id, west, south, east, north, status, message, error, "
    "tiles_file, minzoom, maxzoom, size_bytes, owner_id, created_at, updated_at"
)


@dataclass(frozen=True)
class Preview:
    preview_id: str
    west: float
    south: float
    east: float
    north: float
    status: str
    message: str = ""
    error: str | None = None
    tiles_file: str | None = None
    minzoom: int = 0
    maxzoom: int = 0
    size_bytes: int = 0
    owner_id: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.west, self.south, self.east, self.north)

    def age_seconds(self) -> float:
        if self.created_at is None:
            return 0.0
        return (datetime.now(timezone.utc) - self.created_at).total_seconds()

    def expires_in_seconds(self, ttl_seconds: float = TTL_SECONDS) -> float:
        """Seconds of TTL left, 0 once it is up."""
        return max(0.0, ttl_seconds - self.age_seconds())

    def is_expired(self, ttl_seconds: float = TTL_SECONDS) -> bool:
        """Whether the TTL is up. A row with no timestamp - only possible
        before the INSERT has returned - is never expired."""
        if self.created_at is None:
            return False
        return self.age_seconds() >= ttl_seconds

    def to_dict(self) -> dict:
        """The JSON the picker polls. ``tiles_file`` becomes a URL upstream, in
        garminsvc.vectorbasemap, which is the only place that knows where nginx
        publishes the directory."""
        return {
            "preview_id": self.preview_id,
            "west": self.west,
            "south": self.south,
            "east": self.east,
            "north": self.north,
            "status": self.status,
            "message": self.message,
            "error": self.error,
            "minzoom": self.minzoom,
            "maxzoom": self.maxzoom,
            "size_bytes": self.size_bytes,
            "age_seconds": round(self.age_seconds()),
            "expires_in_seconds": round(self.expires_in_seconds()),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


def ensure_schema() -> None:
    pg.ensure_schema(SQL_DIR)


def round_bbox(
    west: float, south: float, east: float, north: float
) -> tuple[float, float, float, float]:
    return tuple(round(float(v), BBOX_DECIMALS) for v in (west, south, east, north))  # type: ignore[return-value]


def _row_to_preview(row) -> Preview:
    return Preview(
        preview_id=row[0],
        west=float(row[1]),
        south=float(row[2]),
        east=float(row[3]),
        north=float(row[4]),
        status=row[5],
        message=row[6] or "",
        error=row[7],
        tiles_file=row[8],
        minzoom=int(row[9]),
        maxzoom=int(row[10]),
        size_bytes=int(row[11]),
        owner_id=row[12] or "",
        created_at=row[13],
        updated_at=row[14],
    )


def _fetch(sql: str, params: tuple) -> Preview | None:
    with pg.connection() as conn:
        row = conn.execute(sql, params).fetchone()
        conn.commit()
    return _row_to_preview(row) if row else None


def create(
    west: float, south: float, east: float, north: float, *, owner_id: str = ""
) -> Preview:
    west, south, east, north = round_bbox(west, south, east, north)
    preview_id = uuid.uuid4().hex
    preview = _fetch(
        f"""
        INSERT INTO otm.map_previews
            (preview_id, west, south, east, north, status, message, owner_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING {_COLUMNS}
        """,
        (preview_id, west, south, east, north, QUEUED, "Queued", owner_id),
    )
    assert preview is not None  # INSERT ... RETURNING always yields a row
    return preview


def get(preview_id: str) -> Preview | None:
    return _fetch(
        f"SELECT {_COLUMNS} FROM otm.map_previews WHERE preview_id = %s", (preview_id,)
    )


def find_ready(
    west: float,
    south: float,
    east: float,
    north: float,
    *,
    previews_dir: Path,
    ttl_seconds: float = TTL_SECONDS,
) -> Preview | None:
    """A finished preview of exactly this area, still inside its TTL, whose
    file is still on disk.

    Drawing the same rectangle twice — or reloading the page — should show the
    previous render instead of spending another tilemaker run on it, but only
    for as long as that render still stands for the current data and
    cartography: past ``ttl_seconds`` the area is built again. Rows that are
    expired or whose file has been pruned away are skipped rather than deleted
    here: expire() and prune() own that, and a read path that writes is a
    surprise.
    """
    west, south, east, north = round_bbox(west, south, east, north)
    with pg.connection() as conn:
        rows = conn.execute(
            f"""
            SELECT {_COLUMNS} FROM otm.map_previews
            WHERE status = %s AND west = %s AND south = %s AND east = %s AND north = %s
              AND created_at > now() - (%s * interval '1 second')
            ORDER BY created_at DESC
            """,
            (DONE, west, south, east, north, float(ttl_seconds)),
        ).fetchall()
    for row in rows:
        preview = _row_to_preview(row)
        if preview.tiles_file and (previews_dir / preview.tiles_file).is_file():
            return preview
    return None


def find_active(
    west: float, south: float, east: float, north: float
) -> Preview | None:
    """A preview of this area that is already queued or running."""
    west, south, east, north = round_bbox(west, south, east, north)
    return _fetch(
        f"""
        SELECT {_COLUMNS} FROM otm.map_previews
        WHERE status IN (%s, %s) AND west = %s AND south = %s AND east = %s AND north = %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (QUEUED, RUNNING, west, south, east, north),
    )


def start(preview_id: str, message: str = "Building the preview…") -> Preview | None:
    return _fetch(
        f"""
        UPDATE otm.map_previews
        SET status = %s, message = %s, error = NULL, updated_at = now()
        WHERE preview_id = %s
        RETURNING {_COLUMNS}
        """,
        (RUNNING, message, preview_id),
    )


def progress(preview_id: str, message: str) -> None:
    with pg.connection() as conn:
        conn.execute(
            "UPDATE otm.map_previews SET message = %s, updated_at = now() WHERE preview_id = %s",
            (message[:500], preview_id),
        )
        conn.commit()


def finish(
    preview_id: str, *, tiles_file: str, minzoom: int, maxzoom: int, size_bytes: int
) -> Preview | None:
    return _fetch(
        f"""
        UPDATE otm.map_previews
        SET status = %s, message = %s, error = NULL, tiles_file = %s,
            minzoom = %s, maxzoom = %s, size_bytes = %s, updated_at = now()
        WHERE preview_id = %s
        RETURNING {_COLUMNS}
        """,
        (DONE, "Ready", tiles_file, minzoom, maxzoom, size_bytes, preview_id),
    )


def fail(preview_id: str, error: str) -> Preview | None:
    return _fetch(
        f"""
        UPDATE otm.map_previews
        SET status = %s, message = %s, error = %s, updated_at = now()
        WHERE preview_id = %s
        RETURNING {_COLUMNS}
        """,
        (ERROR, "Failed", error[:2000], preview_id),
    )


def requeue_running() -> int:
    """Put previews left RUNNING by a killed worker back in the queue.

    Returns how many rows moved, so the caller can re-enqueue exactly those.
    """
    with pg.connection() as conn:
        rows = conn.execute(
            """
            UPDATE otm.map_previews
            SET status = %s, message = %s, updated_at = now()
            WHERE status = %s
            RETURNING preview_id
            """,
            (QUEUED, "Queued again after a restart", RUNNING),
        ).fetchall()
        conn.commit()
    return len(rows)


def queued_ids() -> list[str]:
    with pg.connection() as conn:
        rows = conn.execute(
            "SELECT preview_id FROM otm.map_previews WHERE status = %s ORDER BY created_at",
            (QUEUED,),
        ).fetchall()
    return [row[0] for row in rows]


def _remove_rows(rows, previews_dir: Path) -> list[str]:
    """Delete the files of already-deleted rows. The row goes first: a file
    left behind with no row is picked up as an orphan by prune(), while a row
    pointing at a missing file would be offered and then 404 in the browser."""
    removed = []
    for preview_id, tiles_file in rows:
        removed.append(preview_id)
        if tiles_file:
            (previews_dir / tiles_file).unlink(missing_ok=True)
    return removed


def expire(previews_dir: Path, *, ttl_seconds: float = TTL_SECONDS) -> list[str]:
    """Drop previews whose TTL is up, with their files.

    The read paths already refuse an expired preview, so this is the sweep that
    gets it off the disk. It runs on the preview worker - at startup and after
    every build - which means an expired file can outlive its TTL on a worker
    that is never asked for anything; it is never served in that window.

    RUNNING rows are left alone whatever their age: a worker is inside build()
    for that id, and pulling the row out from under it would turn the finish
    into a lost update.
    """
    with pg.connection() as conn:
        rows = conn.execute(
            """
            DELETE FROM otm.map_previews
            WHERE status <> %s AND created_at <= now() - (%s * interval '1 second')
            RETURNING preview_id, tiles_file
            """,
            (RUNNING, float(ttl_seconds)),
        ).fetchall()
        conn.commit()
    removed = _remove_rows(rows, previews_dir)
    if removed:
        log.info("Expired %d preview(s) older than %.0f s", len(removed), ttl_seconds)
    return removed


def prune(keep: int, previews_dir: Path) -> list[str]:
    """Keep the *keep* newest previews, drop the rest with their files.

    A preview is a cache of one look at the map, and a 50×50 km area is tens of
    megabytes, so the directory would grow without bound otherwise. Age is
    expire()'s half of that: this is the count cap, which is what holds when a
    burst of previews is drawn well inside the TTL. Files with no row (a worker
    killed between writing and committing) go too.
    """
    with pg.connection() as conn:
        rows = conn.execute(
            """
            DELETE FROM otm.map_previews
            WHERE preview_id NOT IN (
                SELECT preview_id FROM otm.map_previews ORDER BY created_at DESC LIMIT %s
            )
            RETURNING preview_id, tiles_file
            """,
            (max(keep, 0),),
        ).fetchall()
        conn.commit()
        kept = {
            row[0]
            for row in conn.execute("SELECT tiles_file FROM otm.map_previews").fetchall()
            if row[0]
        }

    removed = _remove_rows(rows, previews_dir)
    if previews_dir.is_dir():
        for path in previews_dir.glob("*.pmtiles"):
            if path.name not in kept:
                log.info("Removing orphan preview file %s", path.name)
                path.unlink(missing_ok=True)
    return removed
