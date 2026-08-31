"""Prepare an uploaded OSM/PBF extract for a Garmin map build."""

from __future__ import annotations

import shutil
from pathlib import Path

import npyosmium as osmium

from garminsvc.constants import MAX_UPLOAD_BYTES
from garminsvc.proc import run

ALLOWED_SUFFIXES = (
    ".osm.pbf",
    ".pbf",
    ".osm",
    ".osm.gz",
    ".osm.bz2",
    ".osm.xml",
)


class UploadError(ValueError):
    pass


def _require_osmium() -> str:
    path = shutil.which("osmium")
    if not path:
        raise RuntimeError("osmium tool not found; install osmium-tool (brew install osmium-tool)")
    return path


def normalize_upload_name(filename: str) -> str:
    name = Path(filename or "").name
    lower = name.lower()
    if not any(lower.endswith(suffix) for suffix in ALLOWED_SUFFIXES):
        raise UploadError("Нужен файл .osm, .osm.pbf или .pbf (допускаются .gz / .bz2)")
    return name


def bbox_from_pbf(pbf: Path) -> tuple[float, float, float, float]:
    """west, south, east, north from PBF header or a node scan."""
    reader = osmium.io.Reader(str(pbf))
    try:
        box = reader.header().box()
        if box.valid():
            bl, tr = box.bottom_left, box.top_right
            west, east = bl.lon, tr.lon
            south, north = bl.lat, tr.lat
            if east != west and north != south:
                if east < west:
                    west, east = east, west
                if north < south:
                    south, north = north, south
                return west, south, east, north
    finally:
        reader.close()

    class Ext(osmium.SimpleHandler):
        def __init__(self) -> None:
            super().__init__()
            self.min_lon = 180.0
            self.min_lat = 90.0
            self.max_lon = -180.0
            self.max_lat = -90.0
            self.n = 0

        def node(self, node) -> None:
            if not node.location.valid():
                return
            lon, lat = node.location.lon, node.location.lat
            self.min_lon = min(self.min_lon, lon)
            self.max_lon = max(self.max_lon, lon)
            self.min_lat = min(self.min_lat, lat)
            self.max_lat = max(self.max_lat, lat)
            self.n += 1

    handler = Ext()
    handler.apply_file(str(pbf), locations=False)
    if handler.n == 0:
        raise RuntimeError(f"В файле нет координат: {pbf.name}")
    return handler.min_lon, handler.min_lat, handler.max_lon, handler.max_lat


def to_pbf(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    lower = src.name.lower()
    if lower.endswith(".pbf"):
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        return dest
    osmium_bin = _require_osmium()
    run([osmium_bin, "cat", "-o", str(dest), "--overwrite", str(src)])
    if not dest.is_file() or dest.stat().st_size == 0:
        raise RuntimeError(f"osmium cat не создал PBF из {src.name}")
    return dest


def save_upload_stream(stream, dest: Path, *, declared_size: int | None, max_bytes: int = MAX_UPLOAD_BYTES) -> int:
    if declared_size is not None and declared_size > max_bytes:
        raise UploadError(f"Файл больше {max_bytes // (1024 * 1024)} МБ")
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with dest.open("wb") as out:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                dest.unlink(missing_ok=True)
                raise UploadError(f"Файл больше {max_bytes // (1024 * 1024)} МБ")
            out.write(chunk)
    if written == 0:
        dest.unlink(missing_ok=True)
        raise UploadError("Пустой файл")
    return written
