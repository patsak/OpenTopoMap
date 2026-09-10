"""Invoke tilemaker, and find the cartography it runs on.

The one caller today is the bbox preview (:mod:`tilesvc.preview`), but neither
the command line nor the lookup of the style tree belongs to it: both are about
tilemaker itself, and both are wanted by anything that renders these tiles.
Policy — which config, which zooms, where the output lands — stays with the
caller.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from otmlib.proc import run, worker_count

log = logging.getLogger(__name__)

DEFAULT_BIN = "tilemaker"
BIN_ENV = "OTM_TILEMAKER_BIN"
THREADS_ENV = "OTM_TILEMAKER_THREADS"
PROCESS_LUA = "process-otm.lua"
CONFIG_REGION = "tilemaker-config-otm-region.json"

ROOT = Path(__file__).resolve().parent
# In the images the tilemaker tree is copied next to the packages as
# /app/vector/tilemaker; in a checkout it sits beside www/.
STYLE_DIRS = (
    ROOT.parent / "vector/tilemaker",
    ROOT.parent.parent / "vector/tilemaker",
)


def style_dir() -> Path:
    """Where process-otm.lua and the tilemaker configs live."""
    for directory in STYLE_DIRS:
        if (directory / PROCESS_LUA).is_file():
            return directory
    raise RuntimeError(f"{PROCESS_LUA} not found in vector/tilemaker")


def tilemaker_bin() -> str:
    configured = os.environ.get(BIN_ENV, "").strip()
    if configured:
        return configured
    found = shutil.which(DEFAULT_BIN)
    if not found:
        raise RuntimeError(
            "tilemaker not found; the tilesvc image copies it from "
            f"ghcr.io/systemed/tilemaker, or set {BIN_ENV}"
        )
    return found


def threads() -> int:
    """Same core-leaving policy as the contour builder, via OTM_TILEMAKER_THREADS."""
    return worker_count(os.cpu_count() or 2, THREADS_ENV)


def build(
    *,
    output: Path,
    config: Path,
    process: Path,
    store_dir: Path,
    input_pbf: Path | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    cwd: Path | None = None,
) -> Path:
    """Run tilemaker into *output*, which it overwrites.

    Either *input_pbf* or *bbox* must be given — a config whose layers all come
    from shapefiles has no OSM input, and tilemaker then needs the extent
    stated explicitly. Note that ``--bbox`` does not clip: it only supplies an
    extent for an input without one in its header, so an input cut to the area
    of interest is what actually bounds the output.
    """
    if input_pbf is None and bbox is None:
        raise ValueError("tilemaker.build: pass input_pbf, bbox, or both")

    output.parent.mkdir(parents=True, exist_ok=True)
    store_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        tilemaker_bin(),
        "--output",
        str(output),
        "--config",
        str(config),
        "--process",
        str(process),
        # Without --store tilemaker keeps every node and way in RAM, which a
        # federal district does not fit into. --shard-stores splits that store
        # so its own memory-mapped windows stay bounded too.
        "--store",
        str(store_dir),
        "--shard-stores",
        "--threads",
        str(threads()),
    ]
    if input_pbf is not None:
        cmd += ["--input", str(input_pbf)]
    if bbox is not None:
        cmd += ["--bbox", ",".join(str(v) for v in bbox)]

    run(cmd, cwd=cwd)
    return output
