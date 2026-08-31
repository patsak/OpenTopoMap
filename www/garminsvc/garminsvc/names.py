"""Display names for Garmin map products."""

from __future__ import annotations

GARMIN_NAME_MAX = 50


def garmin_map_names(name: str) -> tuple[str, str]:
    """Names written into IMG family-name / description (Garmin limit ~50 chars)."""
    cleaned = " ".join((name or "").split()).replace("=", " ").replace('"', "'")
    base = cleaned[:GARMIN_NAME_MAX] or "OpenTopoMap Hike"
    suffix = " Contours"
    contours = f"{base}{suffix}"
    if len(contours) > GARMIN_NAME_MAX:
        contours = f"{base[: GARMIN_NAME_MAX - len(suffix)]}{suffix}"
    return base, contours
