"""otmlib.gol without gol: the checks around a build, with the tool faked.

The builds themselves - merging, dating, the GOL a query sees - are tested in
overpass/tests/testGol.py, which needs the real gol and geodesk.
"""

import os
import shutil
import stat
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

from otmlib import gol

Usage = namedtuple("Usage", "total used free")


class GolCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pbf = self.tmp / "region-latest.osm.pbf"
        self.pbf.write_bytes(b"\0" * 1000)
        self.target = self.tmp / "gol" / "regions.gol"

    def fake_gol(self, body: str) -> None:
        script = self.tmp / "gol-tool"
        script.write_text(f"#!/bin/sh\n{body}\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        patch = mock.patch.dict(os.environ, {"OTM_GOL_BIN": str(script)})
        patch.start()
        self.addCleanup(patch.stop)


class TestSpace(GolCase):
    def testRefusesBeforeStartingWhenTheWorkDirIsTooSmall(self):
        self.fake_gol("exit 99")  # must never run
        need = int(1000 * gol.WORK_SPACE_FACTOR)
        with mock.patch("otmlib.gol.shutil.disk_usage", return_value=Usage(0, 0, need - 1)):
            with self.assertRaises(gol.GolBuildError) as ctx:
                gol.build([self.pbf], self.target)
        self.assertIn("free in", str(ctx.exception))
        self.assertFalse(self.target.exists())

    def testEnoughSpaceGoesAhead(self):
        # A stand-in gol that writes its output where gol would.
        self.fake_gol('touch "$2.gol"')
        with mock.patch("otmlib.gol.newest_object", return_value=None):
            gol.build([self.pbf], self.target)
        self.assertTrue(self.target.is_file())

    def testFactorCoversTheMeasuredPeak(self):
        self.assertGreater(gol.WORK_SPACE_FACTOR, 1)


class TestSigbus(GolCase):
    def testSigbusIsExplainedAsAFullDisk(self):
        self.fake_gol("kill -BUS $$")
        with self.assertRaises(gol.GolBuildError) as ctx:
            gol.build([self.pbf], self.target)
        self.assertIn("SIGBUS", str(ctx.exception))
        self.assertIn("ran out of space", str(ctx.exception))

    def testOtherFailuresAreLeftAsThey(self):
        self.fake_gol("exit 3")
        with self.assertRaises(gol.subprocess.CalledProcessError):
            gol.build([self.pbf], self.target)


if __name__ == "__main__":
    unittest.main()
