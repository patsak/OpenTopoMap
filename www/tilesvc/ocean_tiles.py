"""The ``otm-ocean`` tileset: coastline-derived water polygons, built by tilemaker.

Ocean is a separate tileset rather than a layer of ``otm.mbtiles`` because its
inputs are unrelated to the OSM extracts: it is global, changes only when
osmdata.openstreetmap.de republishes the shapefiles, and would otherwise be
re-rendered every time any region gets a diff. The style keeps it as its own
MapLibre source (``opentopomap-ocean``).

The shapefiles are OSM coastline already assembled into polygons and split into
manageable pieces — reassembling coastline rings ourselves is the slow, fragile
step this avoids. ``water-polygons-split-4326`` carries full detail for z8-14;
``simplified-water-polygons-split-4326`` is the pre-simplified set for z0-7,
where full detail would be both invisible and enormous.
"""

from __future__ import annotations

import logging
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from otmlib.filelock import exclusive

from tilesvc import tilemaker
from tilesvc.config import Config

log = logging.getLogger(__name__)

USER_AGENT = "OpenTopoMap-tilesvc/1.0"
BASE_URL = "https://osmdata.openstreetmap.de/download"

# The ocean config addresses these by the same relative paths, resolved from the
# data dir (tilemaker runs with cwd=cfg.data_dir), so the layout here and the
# "source" fields in tilemaker-config-otm-ocean.json must stay in step.
DETAILED = "water-polygons-split-4326"
SIMPLIFIED = "simplified-water-polygons-split-4326"
SHAPEFILE_NAMES = {
    DETAILED: "water_polygons.shp",
    SIMPLIFIED: "simplified_water_polygons.shp",
}


@dataclass(frozen=True)
class ShapefileSet:
    root: Path
    shapefiles: dict[str, Path]

    @property
    def revision(self) -> str:
        """Identity of this download, so an unchanged set is not re-tiled.

        Sizes rather than content hashes: the two archives are hundreds of MB
        and are replaced wholesale on republication, so a size change is a
        reliable "this is different data" signal at no read cost.
        """
        parts = [f"{name}:{path.stat().st_size}" for name, path in sorted(self.shapefiles.items())]
        return " ".join(parts)


def _download_zip(url: str, dest_dir: Path) -> None:
    """Fetch *url* and unpack it so its own top-level directory lands on *dest_dir*."""
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    archive = dest_dir.with_suffix(".zip")
    req = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
    )
    log.info("Download %s", url)
    with urllib.request.urlopen(req, timeout=1800) as response, archive.open("wb") as handle:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)
    try:
        with zipfile.ZipFile(archive) as zipped:
            # testzip() CRC-checks every member: a download cut short mid-way
            # would otherwise extract a partial shapefile that looks valid
            # enough for tilemaker to render a hole in the ocean.
            bad_member = zipped.testzip()
            if bad_member is not None:
                raise RuntimeError(f"{url}: corrupt member in downloaded archive: {bad_member}")
            # The archives already contain a top-level directory named after
            # the dataset, so they extract into the shapefile root itself.
            zipped.extractall(dest_dir.parent)
    except (zipfile.BadZipFile, RuntimeError):
        archive.unlink(missing_ok=True)
        raise
    archive.unlink(missing_ok=True)


def ensure_shapefiles(cfg: Config, *, force: bool = False) -> ShapefileSet:
    """Download the water-polygon shapefiles into ``<data_dir>/shapefiles``."""
    root = cfg.shapefiles
    root.mkdir(parents=True, exist_ok=True)
    resolved: dict[str, Path] = {}
    for dataset, shp_name in SHAPEFILE_NAMES.items():
        shp = root / dataset / shp_name
        if force or not shp.is_file():
            with exclusive(root / f"{dataset}.lock"):
                if force or not shp.is_file():
                    _download_zip(f"{BASE_URL}/{dataset}.zip", root / dataset)
        if not shp.is_file():
            raise RuntimeError(f"shapefile missing after download: {shp}")
        resolved[dataset] = shp
    log.info("Ocean shapefiles ready in %s", root)
    return ShapefileSet(root=root, shapefiles=resolved)


def build_ocean(cfg: Config, *, force: bool = False) -> Path | None:
    """Build ``otm-ocean.mbtiles`` when the shapefile set has changed.

    Returns the tileset path, or None when the existing file is already built
    from this shapefile set.
    """
    shapes = ensure_shapefiles(cfg, force=force)
    revision = shapes.revision
    if not force and not tilemaker.needs_rebuild(
        tilemaker.TILESET_OCEAN, revision, cfg.vector_tiles
    ):
        log.info("otm-ocean.mbtiles is current")
        return None

    return tilemaker.build_tileset(
        tilemaker.TILESET_OCEAN,
        revision,
        config=tilemaker.style_dir() / tilemaker.CONFIG_OCEAN,
        tiles_dir=cfg.vector_tiles,
        store_dir=cfg.tilemaker_store / tilemaker.TILESET_OCEAN,
        # Every layer of this config comes from a shapefile, so there is no OSM
        # input to take an extent from; tilemaker needs the world stated.
        bbox=(-180.0, -85.0511287798, 180.0, 85.0511287798),
        cwd=cfg.data_dir,
    )
