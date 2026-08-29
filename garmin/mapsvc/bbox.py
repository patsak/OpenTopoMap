"""BBox parsing and size checks in kilometres (great-circle)."""

from __future__ import annotations

from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt

EARTH_RADIUS_KM = 6371.0088
MAX_BBOX_SIDE_KM = 500.0


@dataclass
class BBox:
    west: float
    south: float
    east: float
    north: float


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    lon1, lat1, lon2, lat2 = map(radians, (lon1, lat1, lon2, lat2))
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    chord = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(min(1.0, sqrt(chord)))


def bbox_max_side_km(west: float, south: float, east: float, north: float) -> float:
    if east < west:
        west, east = east, west
    if north < south:
        south, north = north, south
    return max(
        haversine_km(west, south, east, south),
        haversine_km(west, north, east, north),
        haversine_km(west, south, west, north),
        haversine_km(east, south, east, north),
    )


def validate_bbox_size(west: float, south: float, east: float, north: float) -> None:
    side = bbox_max_side_km(west, south, east, north)
    if side > MAX_BBOX_SIDE_KM:
        raise ValueError(
            f"Каждая сторона bbox должна быть не больше {MAX_BBOX_SIDE_KM:.0f} км "
            f"(сейчас {side:.0f} км)"
        )


def parse_bbox(payload: dict) -> tuple[float, float, float, float]:
    try:
        west = float(payload["west"])
        south = float(payload["south"])
        east = float(payload["east"])
        north = float(payload["north"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Expected JSON: west, south, east, north (float, EPSG:4326)") from exc
    if not (-180 <= west <= 180 and -180 <= east <= 180):
        raise ValueError("Longitude must be between -180 and 180")
    if not (-90 <= south <= 90 and -90 <= north <= 90):
        raise ValueError("Latitude must be between -90 and 90")
    if east == west or north == south:
        raise ValueError("BBox must have non-zero width and height")
    if east < west:
        west, east = east, west
    if north < south:
        south, north = north, south
    validate_bbox_size(west, south, east, north)
    return west, south, east, north
