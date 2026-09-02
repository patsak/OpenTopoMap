"""Invoke tilemaker.

Two callers, one command line: the nightly tileset build in
:mod:`tilesvc.tilemaker` and the bbox preview in :mod:`tilesvc.preview`. What
differs between them is policy — which config, which zooms, where the output
goes, what happens to the previous file — so only the invocation itself lives
here.
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
