"""External dependency checks and offline install helpers.

Two kinds of dependency come down from the network. The Java tooling is pinned
and verified — that manifest is [`artifacts.py`](artifacts.py). The sea and
bounds archives are the ones handled here: unversioned `*-latest.zip`, several
gigabytes unpacked, and shared with the data volume rather than the image.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from garminsvc import artifacts
from garminsvc.constants import (
    BOUNDS_DIR,
    BOUNDS_URL,
    DATA_DIR,
    OPTIONS_CONTOURS,
    OPTIONS_MAIN,
    SEA_DIR,
    SEA_URL,
    STYLE_DIR,
    TOOLS_DIR,
)
from garminsvc.fetch import ConsoleProgress, LogFn, ProgressFn, download, extract


def data_present(dir_path: Path, marker_glob: str) -> bool:
    return dir_path.is_dir() and bool(_first_glob(dir_path, marker_glob))


def sea_bounds_ready() -> bool:
    return data_present(SEA_DIR, "sea_*") and data_present(BOUNDS_DIR, "bounds_*")


@dataclass(frozen=True)
class Deps:
    java: str
    osmium: str
    mkgmap_jar: Path
    splitter_jar: Path
    sea_dir: Path
    bounds_dir: Path
    python: str


def _first_glob(path: Path, pattern: str) -> list[Path]:
    return sorted(path.glob(pattern))


def _flatten_nested(dir_path: Path, nested_name: str) -> None:
    nested = dir_path / nested_name
    if not nested.is_dir():
        return
    for item in nested.iterdir():
        target = dir_path / item.name
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        shutil.move(str(item), str(target))
    nested.rmdir()


def _extract_archive(
    label: str,
    dest_dir: Path,
    url: str,
    zip_name: str,
    nested_name: str,
    marker_glob: str,
    log: LogFn,
    progress: ProgressFn | None = None,
) -> Path:
    if data_present(dest_dir, marker_glob):
        log(f"{label}: already present at {dest_dir}")
        return dest_dir

    local_zips = sorted(DATA_DIR.glob(f"{nested_name}*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    downloaded = False
    if local_zips:
        zpath = local_zips[0]
        log(f"{label}: extracting local {zpath.name}")
    else:
        zpath = DATA_DIR / zip_name
        download(url, zpath, log, progress=progress, label=f"Downloading {zip_name}")
        downloaded = True

    if progress is not None:
        progress(f"Unpacking {label.lower()}…", 0, None)
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    extract(zpath, dest_dir)
    _flatten_nested(dest_dir, nested_name)
    if not _first_glob(dest_dir, marker_glob):
        raise RuntimeError(f"{label}: no {marker_glob} after extract in {dest_dir}")
    if downloaded:
        zpath.unlink(missing_ok=True)
    if progress is not None:
        progress(f"{label} ready", 1, 1)
    log(f"{label}: ready at {dest_dir}")
    return dest_dir


def download_sea_bounds(log: LogFn | None = None, progress: ProgressFn | None = None) -> tuple[Path, Path]:
    log = log or print
    if progress is None:
        progress = ConsoleProgress(log)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    sea = _extract_archive("SEA", SEA_DIR, SEA_URL, "sea-latest.zip", "sea", "sea_*", log, progress)
    bounds = _extract_archive(
        "BOUNDS", BOUNDS_DIR, BOUNDS_URL, "bounds-latest.zip", "bounds", "bounds_*", log, progress
    )
    return sea, bounds


def download_deps(log: LogFn | None = None, progress: ProgressFn | None = None) -> Deps:
    """Install the pinned jars and the sea/bounds data. Fail if Java/osmium missing."""
    log = log or print
    if progress is None:
        progress = ConsoleProgress(log)
    java = shutil.which("java")
    if not java:
        raise RuntimeError("java not found; install Java 17+ first")
    osmium = shutil.which("osmium")
    if not osmium:
        raise RuntimeError("osmium not found; install osmium-tool first (brew install osmium-tool)")

    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    jars = artifacts.install_all(TOOLS_DIR, log=log, progress=progress)

    sea, bounds = download_sea_bounds(log=log, progress=progress)

    for required in (
        STYLE_DIR / "opentopomap-hike",
        STYLE_DIR / "contours-hike",
        STYLE_DIR / "typ" / "opentopomap-hike.txt",
        STYLE_DIR / "typ" / "contours-hike.txt",
        OPTIONS_MAIN,
        OPTIONS_CONTOURS,
    ):
        if not required.exists():
            raise RuntimeError(f"Missing project file: {required}")

    return Deps(
        java=java,
        osmium=osmium,
        mkgmap_jar=jars["mkgmap"],
        splitter_jar=jars["splitter"],
        sea_dir=sea,
        bounds_dir=bounds,
        python=shutil.which("python3") or "python3",
    )


def require_deps() -> Deps:
    """Validate that all runtime dependencies already exist. Never downloads."""
    missing: list[str] = []

    java = shutil.which("java")
    if not java:
        missing.append("java (Java 17+)")
    osmium = shutil.which("osmium")
    if not osmium:
        missing.append("osmium (osmium-tool)")

    for absent in artifacts.missing(TOOLS_DIR):
        missing.append(f"{absent} (run: python -m garminsvc.fetchdeps)")

    if not data_present(SEA_DIR, "sea_*"):
        missing.append(f"sea tiles in {SEA_DIR} (run: python download_deps.py)")
    if not data_present(BOUNDS_DIR, "bounds_*"):
        missing.append(f"bounds files in {BOUNDS_DIR} (run: python download_deps.py)")

    for required in (
        STYLE_DIR / "opentopomap-hike",
        STYLE_DIR / "contours-hike",
        STYLE_DIR / "typ" / "opentopomap-hike.txt",
        STYLE_DIR / "typ" / "contours-hike.txt",
        OPTIONS_MAIN,
        OPTIONS_CONTOURS,
    ):
        if not required.exists():
            missing.append(f"project file {required}")

    try:
        import huey  # noqa: F401
        import npyosmium  # noqa: F401
        import numpy  # noqa: F401
        import pyhgtmap  # noqa: F401
        import scipy  # noqa: F401
        import shapely  # noqa: F401
        from osgeo import gdal  # noqa: F401
    except ImportError as exc:
        missing.append(f"Python package ({exc}); pip install -r requirements-server.txt")

    if missing:
        lines = "\n  - ".join(missing)
        raise RuntimeError(
            "Missing dependencies:\n  - "
            + lines
            + "\n\nInstall Python packages with:\n"
            "  pip install -r requirements-server.txt\n"
            "Download jars / sea / bounds with:\n"
            "  python download_deps.py"
        )

    assert java and osmium
    return Deps(
        java=java,
        osmium=osmium,
        mkgmap_jar=artifacts.jar_path(artifacts.MKGMAP, TOOLS_DIR),
        splitter_jar=artifacts.jar_path(artifacts.SPLITTER, TOOLS_DIR),
        sea_dir=SEA_DIR,
        bounds_dir=BOUNDS_DIR,
        python=sys_executable(),
    )


def sys_executable() -> str:
    import sys

    return sys.executable
