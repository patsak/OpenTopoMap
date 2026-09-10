"""Cartography constants shared by the Garmin build and the vector tiles."""

from __future__ import annotations

# Adaptive contour spacing: plain 10 m, mountain 20 m (Genshtab-style).
# The Garmin overlay uses this on the device. The web map draws a fixed 20 m
# interval from Mapterhorn (maplibre-contour has no terrain classifier).
MOUNTAIN_RELIEF_M = 300
MOUNTAIN_MAX_ELEV_M = 1200

# Above this slope the major contours crowd into an unreadable band, so they are
# thinned on the Garmin overlay.
STEEP_DEG = 50.0

# Genshtab contour hierarchy: index every 100 m, intermediate every 50 m.
MAJOR_INTERVAL_M = 100
MEDIUM_INTERVAL_M = 50

# Zoom range the web map draws isolines over. Zoomed out further, a 20 m interval
# collapses into a brown wash, so the crest lines (natural=ridge, from the base
# tileset) carry the relief on their own down there — the same division the Garmin
# overlay makes between the ridge and contour symbols.
CONTOUR_MINZOOM = 12
CONTOUR_MAXZOOM = 14

# Tags that make an object part of the glacier subset: the ice itself, the crevasses
# on it and the moraines that ride it. The Garmin contour tagging is cut with this
# list; the tile job writes the matching PBF next to each Geofabrik extract.
GLACIER_FILTER = (
    "nwr/natural=glacier",
    "nwr/natural=crevasse",
    "nwr/natural=moraine",
    "nwr/geological=moraine",
    "nwr/glacier:part",
)
