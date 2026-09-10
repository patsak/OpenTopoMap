"""Making a preview record look old, for the TTL tests.

A TTL is only visible in time, and nothing is going to wait a day for it.
``created_at`` is the one column otmlib.previews never writes - Postgres
defaults it - so the tests move it backwards themselves.
"""

from __future__ import annotations

from otmlib import pg, previews


def backdate(preview_id: str, seconds: float) -> None:
    """Pretend the preview was requested *seconds* ago."""
    with pg.connection() as conn:
        conn.execute(
            """
            UPDATE otm.map_previews
            SET created_at = now() - (%s * interval '1 second')
            WHERE preview_id = %s
            """,
            (float(seconds), preview_id),
        )
        conn.commit()


def makeExpired(preview_id: str, ttl_seconds: float = previews.TTL_SECONDS) -> None:
    """Push it just past the TTL, so every expiry check sees it as up."""
    backdate(preview_id, ttl_seconds + 60)
