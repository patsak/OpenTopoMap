"""The fixture town as a GOL, built once per test run.

Needs osmium (the XML fixture → PBF) and GeoDesk's gol tool (PBF → GOL,
through otmlib.gol). Either missing, the tests that need a GOL skip:
``OTM_GOL_BIN=/path/to/gol pytest overpass/tests`` runs them.
"""

from __future__ import annotations

import atexit
import functools
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from otmlib.gol import build, gol_binary

FIXTURE = Path(__file__).resolve().parent / "fixture.osm"
NEIGHBOUR = Path(__file__).resolve().parent / "neighbour.osm"


def tools_missing() -> str | None:
    if shutil.which("osmium") is None:
        return "osmium is not installed"
    if shutil.which(gol_binary()) is None:
        return "GeoDesk's gol tool is not on PATH; set OTM_GOL_BIN"
    return None


@functools.cache
def work_dir() -> Path:
    work = Path(tempfile.mkdtemp(prefix="overpass-test-"))
    atexit.register(shutil.rmtree, work, ignore_errors=True)
    return work


def to_pbf(source: Path) -> Path:
    pbf = work_dir() / (source.stem + ".osm.pbf")
    if not pbf.exists():
        subprocess.run(["osmium", "cat", str(source), "-o", str(pbf)], check=True, capture_output=True)
    return pbf


def fixture_pbf() -> Path:
    return to_pbf(FIXTURE)


def neighbour_pbf() -> Path:
    return to_pbf(NEIGHBOUR)


@functools.cache
def fixture_gol() -> Path:
    gol = fixture_pbf().parent / "fixture.gol"
    build([fixture_pbf()], gol)
    return gol


def gol_without_waynode_ids() -> Path:
    """The same town built the wrong way, for the check that refuses it."""
    target = fixture_pbf().parent / "no-waynode-ids"
    if not target.with_suffix(".gol").exists():
        subprocess.run([gol_binary(), "build", str(target), str(fixture_pbf())], check=True, capture_output=True)
    return target.with_suffix(".gol")


class GolTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        missing = tools_missing()
        if missing:
            raise unittest.SkipTest(missing)
        cls.gol = fixture_gol()
