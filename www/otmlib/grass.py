"""GRASS GIS from Python: find it, import grass.script, run a throwaway project.

Shared by the DEM tools in ``tools/`` (ridges, rivers) and the routing graph
build (``www/routing``), which all run the same r.watershed hydrology on a DEM
warped into a metric CRS. GRASS is not a Python package: its ``grass.script``
lives inside the installation and is put on ``sys.path`` from what the
launcher reports, so nothing here is importable until :func:`import_grass_script`
has run.
"""

from __future__ import annotations

import contextlib
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterator


def utm_epsg(lon: float, lat: float) -> int:
    """EPSG code of the WGS84 UTM zone a point falls into."""
    zone = int(math.floor((lon + 180.0) / 6.0)) % 60 + 1
    return (32600 if lat >= 0 else 32700) + zone


def grass_executable() -> str:
    """The grass launcher: ``GRASS_BIN``, then PATH, then a macOS app bundle."""
    env = os.environ.get("GRASS_BIN", "").strip()
    if env:
        return env
    for name in ("grass", "grass85", "grass84", "grass83"):
        found = shutil.which(name)
        if found:
            return found
    for app in sorted(Path("/Applications").glob("GRASS-*.app"), reverse=True):
        candidate = app / "Contents/Resources/bin/grass"
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError(
        "GRASS not found: install it (brew install --cask grass or grass-gis) "
        "or point GRASS_BIN at the grass launcher"
    )


def available() -> bool:
    try:
        grass_executable()
    except RuntimeError:
        return False
    return True


def grass_gisbase(grass_bin: str | None = None) -> str:
    """The GRASS installation directory (``grass --config path``)."""
    grass_bin = grass_bin or grass_executable()
    try:
        return subprocess.run([grass_bin, "--config", "path"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"cannot query {grass_bin} --config path: {exc}") from exc


def import_grass_script(grass_bin: str | None = None):
    """Put GRASS' own python package on sys.path and import grass.script."""
    grass_bin = grass_bin or grass_executable()
    try:
        python_path = subprocess.run(
            [grass_bin, "--config", "python_path"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"cannot query {grass_bin} --config python_path: {exc}") from exc
    if python_path not in sys.path:
        sys.path.append(python_path)
    import grass.script as gs  # noqa: PLC0415  (only importable once the path is set)

    return gs


@contextlib.contextmanager
def dem_project(gs, dem_tif: Path, work_dir: Path, name: str) -> Iterator[dict]:
    """A throwaway GRASS project holding the DEM as raster ``dem``.

    Yields the region so the caller can turn cell counts into square kilometres.
    """
    grassdata = work_dir / "grassdata"
    grassdata.mkdir(parents=True, exist_ok=True)
    project = grassdata / name
    if project.exists():
        shutil.rmtree(project)
    if hasattr(gs, "create_project"):
        gs.create_project(str(project), filename=str(dem_tif))
    else:
        # GRASS before 8.4 (Debian 12 has 8.2): its create_location runs
        # modules before there is a session to run them in, so the launcher
        # makes the project: -c from the GeoTIFF's CRS, -e exit at once
        subprocess.run([grass_executable(), "-c", str(dem_tif), "-e", str(project)],
                       check=True, capture_output=True, text=True)
    # GRASS before 8.4 does not find its own install (and so its modules)
    # unless told; later versions take the same hint
    import grass.script.setup as gsetup  # noqa: PLC0415 (8.2 does not import it with grass.script)

    with gsetup.init(str(project), grass_path=grass_gisbase()):
        gs.run_command("r.in.gdal", input=str(dem_tif), output="dem", overwrite=True)
        gs.run_command("g.region", raster="dem")
        yield gs.region()
