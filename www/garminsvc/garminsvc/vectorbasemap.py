"""The MapLibre vector map served as a basemap for the bbox picker.

garminsvc builds Garmin images in the opentopomap-hike style, and vector/ renders the
same cartography for the browser. Offering it in the picker lets a user see on screen
what the build will put on the device, over exactly the area being selected.

Tiles are not proxied through Flask: they come from Martin (or any other tile server)
named by OTM_VECTOR_TILES_URL / OTM_MARTIN_PUBLIC_URL. Style assets stay here because
they are small and versioned with the picker.
"""

from __future__ import annotations

import os
from pathlib import Path

from garminsvc.constants import VECTOR_STYLE_DIRS, VECTOR_TILES_DIR

URL_PREFIX = "/vector"
ASSETS_URL = f"{URL_PREFIX}/assets"
BASE_SET = "otm"
CONTOUR_SET = "otm-contours"
LAYERS_ASSET = "otm_layers.json"
STYLE_ASSET = "otm_style.js"
SPRITE_ASSET = "otm_sprite"

MAP_ATTRIBUTION = "Map style: © OpenTopoMap, Map data © OpenStreetMap contributors"
CONTOUR_ATTRIBUTION = "Contours: © OpenTopoMap"
DEFAULT_MAXZOOM = 14
# Browser-facing base URL of Martin (or another MVT server). Inside Docker Compose the
# tiles service publishes this port on the host; the picker talks to it directly.
DEFAULT_MARTIN_PUBLIC_URL = "http://127.0.0.1:3000"


def style_dir() -> Path | None:
    """First location holding the MapLibre style, or None if the assets are missing."""
    for candidate in VECTOR_STYLE_DIRS:
        if (candidate / LAYERS_ASSET).is_file() and (candidate / STYLE_ASSET).is_file():
            return candidate
    return None


def _tileset_file(name: str) -> Path | None:
    path = VECTOR_TILES_DIR / f"{name}.mbtiles"
    return path if path.is_file() else None


def _martin_public_url() -> str:
    return os.environ.get("OTM_MARTIN_PUBLIC_URL", DEFAULT_MARTIN_PUBLIC_URL).rstrip("/")


def _martin_tiles_url(name: str) -> str:
    # Martin exposes each .mbtiles file as /{stem}/{z}/{x}/{y}.
    return f"{_martin_public_url()}/{name}/{{z}}/{{x}}/{{y}}"


def config() -> dict:
    """What the picker needs to build the style, or {"available": False}."""
    assets = style_dir()
    if assets is None:
        return {"available": False, "reason": "no MapLibre style found"}

    remote = os.environ.get("OTM_VECTOR_TILES_URL", "").strip()
    local = _tileset_file(BASE_SET)
    if remote:
        source = {"tiles": remote, "maxzoom": DEFAULT_MAXZOOM}
        name = "OpenTopoMap (вектор)"
    elif local is not None:
        # Local mbtiles are served by Martin from data/vector-tiles/, not by Flask.
        source = {"tiles": _martin_tiles_url(BASE_SET), "maxzoom": DEFAULT_MAXZOOM}
        name = "OpenTopoMap (вектор, локальные тайлы)"
    else:
        return {"available": False, "reason": "no vector tiles"}

    remote_contours = os.environ.get("OTM_VECTOR_CONTOURS_URL", "").strip()
    local_contours = _tileset_file(CONTOUR_SET)
    contours = None
    if remote_contours:
        contours = {"tiles": remote_contours, "maxzoom": DEFAULT_MAXZOOM}
    elif local_contours is not None:
        contours = {"tiles": _martin_tiles_url(CONTOUR_SET), "maxzoom": DEFAULT_MAXZOOM}
    if contours is not None:
        contours["attribution"] = CONTOUR_ATTRIBUTION

    return {
        "available": True,
        "name": name,
        "attribution": MAP_ATTRIBUTION,
        "contours": contours,
        "layers": f"{ASSETS_URL}/{LAYERS_ASSET}",
        "style": f"{ASSETS_URL}/{STYLE_ASSET}",
        "sprite": f"{ASSETS_URL}/{SPRITE_ASSET}",
        **source,
    }
