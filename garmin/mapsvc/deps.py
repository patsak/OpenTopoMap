"""External dependency checks and offline install helpers."""

from __future__ import annotations

import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen

from mapsvc.constants import (
    BOUNDS_DIR,
    BOUNDS_URL,
    DATA_DIR,
    MKGMAP_JAR,
    MKGMAP_URL,
    MKGMAP_VERSION,
    OPTIONS_CONTOURS,
    OPTIONS_MAIN,
    RIDGES_SCRIPT,
    SEA_DIR,
    SEA_URL,
    SPLITTER_URL,
    SPLITTER_VERSION,
    STYLE_DIR,
    TOOLS_DIR,
)

LogFn = Callable[[str], None]


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


def _find_splitter_jar() -> Path | None:
    found = next(TOOLS_DIR.rglob("splitter.jar"), None)
    return found


def _download(url: str, dest: Path, log: LogFn) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    if tmp.exists():
        tmp.unlink()
    log(f"Downloading {url}")
    req = Request(url, headers={"User-Agent": "OpenTopoMap-garmin-server/1.0"})
    with urlopen(req, timeout=600) as resp, tmp.open("wb") as out:
        shutil.copyfileobj(resp, out, length=1024 * 1024)
    tmp.replace(dest)


def _unzip(zip_path: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest_dir)


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
) -> Path:
    if dest_dir.is_dir() and _first_glob(dest_dir, marker_glob):
        log(f"{label}: already present at {dest_dir}")
        return dest_dir

    local_zips = sorted(DATA_DIR.glob(f"{nested_name}*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    if local_zips:
        zpath = local_zips[0]
        log(f"{label}: extracting local {zpath.name}")
    else:
        zpath = DATA_DIR / zip_name
        _download(url, zpath, log)

    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    _unzip(zpath, dest_dir)
    _flatten_nested(dest_dir, nested_name)
    if not _first_glob(dest_dir, marker_glob):
        raise RuntimeError(f"{label}: no {marker_glob} after extract in {dest_dir}")
    log(f"{label}: ready at {dest_dir}")
    return dest_dir


def download_deps(log: LogFn | None = None) -> Deps:
    """Download mkgmap/splitter/sea/bounds into garmin/. Fail if Java/osmium missing."""
    log = log or print
    java = shutil.which("java")
    if not java:
        raise RuntimeError("java not found; install Java 17+ first")
    osmium = shutil.which("osmium")
    if not osmium:
        raise RuntimeError("osmium not found; install osmium-tool first (brew install osmium-tool)")

    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not MKGMAP_JAR.is_file():
        found = next(TOOLS_DIR.rglob("mkgmap.jar"), None)
        if found:
            if MKGMAP_JAR.exists() or MKGMAP_JAR.is_symlink():
                MKGMAP_JAR.unlink()
            MKGMAP_JAR.symlink_to(found)
            log(f"mkgmap: linked {MKGMAP_JAR} → {found}")
        else:
            zpath = TOOLS_DIR / f"{MKGMAP_VERSION}.zip"
            _download(MKGMAP_URL, zpath, log)
            _unzip(zpath, TOOLS_DIR)
            jar = TOOLS_DIR / MKGMAP_VERSION / "mkgmap.jar"
            if not jar.is_file():
                raise RuntimeError(f"mkgmap.jar missing after unpack: {jar}")
            if MKGMAP_JAR.exists() or MKGMAP_JAR.is_symlink():
                MKGMAP_JAR.unlink()
            MKGMAP_JAR.symlink_to(jar)
            log(f"mkgmap: installed {MKGMAP_JAR}")
    else:
        log(f"mkgmap: {MKGMAP_JAR}")

    splitter = _find_splitter_jar()
    if splitter is None:
        zpath = TOOLS_DIR / f"{SPLITTER_VERSION}.zip"
        _download(SPLITTER_URL, zpath, log)
        _unzip(zpath, TOOLS_DIR)
        splitter = TOOLS_DIR / SPLITTER_VERSION / "splitter.jar"
        if not splitter.is_file():
            raise RuntimeError(f"splitter.jar missing after unpack: {splitter}")
        log(f"splitter: installed {splitter}")
    else:
        log(f"splitter: {splitter}")

    sea = _extract_archive("SEA", SEA_DIR, SEA_URL, "sea-latest.zip", "sea", "sea_*", log)
    bounds = _extract_archive(
        "BOUNDS", BOUNDS_DIR, BOUNDS_URL, "bounds-latest.zip", "bounds", "bounds_*", log
    )

    for required in (
        STYLE_DIR / "opentopomap-hike",
        STYLE_DIR / "contours-hike",
        STYLE_DIR / "typ" / "opentopomap-hike.txt",
        STYLE_DIR / "typ" / "contours-hike.txt",
        OPTIONS_MAIN,
        OPTIONS_CONTOURS,
        RIDGES_SCRIPT,
    ):
        if not required.exists():
            raise RuntimeError(f"Missing project file: {required}")

    return Deps(
        java=java,
        osmium=osmium,
        mkgmap_jar=MKGMAP_JAR.resolve(),
        splitter_jar=splitter.resolve(),
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

    if not MKGMAP_JAR.is_file():
        missing.append(f"mkgmap.jar at {MKGMAP_JAR} (run: python download_deps.py)")
    splitter = _find_splitter_jar()
    if splitter is None:
        missing.append(f"splitter.jar under {TOOLS_DIR} (run: python download_deps.py)")

    if not (SEA_DIR.is_dir() and _first_glob(SEA_DIR, "sea_*")):
        missing.append(f"sea tiles in {SEA_DIR} (run: python download_deps.py)")
    if not (BOUNDS_DIR.is_dir() and _first_glob(BOUNDS_DIR, "bounds_*")):
        missing.append(f"bounds files in {BOUNDS_DIR} (run: python download_deps.py)")

    for required in (
        STYLE_DIR / "opentopomap-hike",
        STYLE_DIR / "contours-hike",
        STYLE_DIR / "typ" / "opentopomap-hike.txt",
        STYLE_DIR / "typ" / "contours-hike.txt",
        OPTIONS_MAIN,
        OPTIONS_CONTOURS,
        RIDGES_SCRIPT,
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

    assert java and osmium and splitter
    return Deps(
        java=java,
        osmium=osmium,
        mkgmap_jar=MKGMAP_JAR.resolve(),
        splitter_jar=splitter.resolve(),
        sea_dir=SEA_DIR,
        bounds_dir=BOUNDS_DIR,
        python=sys_executable(),
    )


def sys_executable() -> str:
    import sys

    return sys.executable
