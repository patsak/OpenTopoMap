"""Anonymous browser identity (cookie) for job ownership."""

from __future__ import annotations

import uuid

CLIENT_COOKIE = "otm_client"
CLIENT_COOKIE_MAX_AGE = 400 * 24 * 3600


def parse_client_id(raw: str | None) -> str | None:
    try:
        return str(uuid.UUID((raw or "").strip()))
    except (AttributeError, TypeError, ValueError):
        return None


def resolve_client_id(raw: str | None) -> tuple[str, bool]:
    """Return (client_id, is_new)."""
    parsed = parse_client_id(raw)
    if parsed is None:
        return str(uuid.uuid4()), True
    return parsed, False
