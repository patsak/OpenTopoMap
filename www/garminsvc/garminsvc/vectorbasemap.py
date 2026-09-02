"""What the picker needs to draw a map in the OpenTopoMap style.

Not a basemap any more: the public raster maps in the picker's dropdown are the
browser's business, and the one OTM-styled layer the service can offer is a
preview of the drawn bbox (see :mod:`otmlib.previews`). What is left here is
everything that preview needs on top of its own tiles — the MapLibre style
assets, the DEM the hillshade and contours are drawn from, and where nginx
publishes the built ``.pmtiles``.
"""

from __future__ import annotations

import os
from pathlib import Path

from garminsvc.constants import VECTOR_STYLE_DIRS

URL_PREFIX = "/vector"
ASSETS_URL = f"{URL_PREFIX}/assets"
LAYERS_ASSET = "otm_layers.json"
STYLE_ASSET = "otm_style.js"
SPRITE_ASSET = "otm_sprite"

MAP_NAME = "Превью области"
MAP_ATTRIBUTION = "Map style: © OpenTopoMap, Map data © OpenStreetMap contributors"
DEM_ATTRIBUTION = "DEM: © Mapterhorn"
DEFAULT_DEM_MAXZOOM = 12
DEFAULT_DEM_TILESIZE = 512
DEFAULT_DEM_URL = "https://tiles.mapterhorn.com/{z}/{x}/{y}.webp"
# Where the previews directory is published. The browser reads the .pmtiles
# with range requests, so this has to be a URL it can reach directly - not a
# path inside the container.
DEFAULT_PREVIEW_PUBLIC_URL = "http://127.0.0.1:8081"


def style_dir() -> Path | None:
    for candidate in VECTOR_STYLE_DIRS:
        if (candidate / LAYERS_ASSET).is_file() and (candidate / STYLE_ASSET).is_file():
            return candidate
    return None


def _public_url(env_var: str, default: str) -> str:
    return (os.environ.get(env_var, "").strip() or default).rstrip("/")


def preview_tiles_url(tiles_file: str) -> str:
    """Absolute URL of one built preview file."""
    base = _public_url("OTM_PREVIEW_PUBLIC_URL", DEFAULT_PREVIEW_PUBLIC_URL)
    return f"{base}/{tiles_file}"


def _map_center() -> dict:
    """Initial view, from the regions currently configured.

    Imported lazily and behind a catch-all: an absent or still-booting Postgres
    should cost the picker its opening position, not the whole map.
    """
    try:
        from otmlib.pgmeta import coverage_center

        return coverage_center()
    except Exception:  # noqa: BLE001
        return {}


def config() -> dict:
    """Everything the page needs before it can show a preview."""
    assets = style_dir()
    if assets is None:
        return {"available": False, "reason": "no MapLibre style found"}

    dem = {
        "tiles": os.environ.get("OTM_DEM_URL", "").strip() or DEFAULT_DEM_URL,
        "maxzoom": DEFAULT_DEM_MAXZOOM,
        "tileSize": DEFAULT_DEM_TILESIZE,
        "encoding": "terrarium",
        "attribution": DEM_ATTRIBUTION,
    }

    result = {
        "available": True,
        "name": MAP_NAME,
        "attribution": MAP_ATTRIBUTION,
        "dem": dem,
        "layers": f"{ASSETS_URL}/{LAYERS_ASSET}",
        "style": f"{ASSETS_URL}/{STYLE_ASSET}",
        "sprite": f"{ASSETS_URL}/{SPRITE_ASSET}",
    }
    result.update(_map_center())
    return result
