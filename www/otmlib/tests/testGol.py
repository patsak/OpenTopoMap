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


class TestSequences(GolCase):
    def setUp(self):
        super().setUp()
        self.fake_gol('touch "$2.gol"')
        patch = mock.patch("otmlib.gol.newest_object", return_value="2026-10-01T00:00:00Z")
        patch.start()
        self.addCleanup(patch.stop)

    def testBuildRecordsWhereEachExtractIs(self):
        sources = gol.fingerprint([self.pbf])
        gol.build([self.pbf], self.target, sequences={self.pbf.name: 42}, expected_sources=sources)
        state = gol.read_state(self.target)
        self.assertEqual(state.sequences, {"region-latest.osm.pbf": 42})
        self.assertEqual(state.newest, {"region-latest.osm.pbf": "2026-10-01T00:00:00Z"})
        self.assertEqual(state.timestamp, "2026-10-01T00:00:00Z")
        self.assertIsNotNone(state.built)
        self.assertEqual(state.sources, sources)

    def testAnExtractRewrittenSinceItsSequenceWasReadRecordsNone(self):
        sources = gol.fingerprint([self.pbf])
        self.pbf.write_bytes(b"\0" * 2000)
        gol.build([self.pbf], self.target, sequences={self.pbf.name: 42}, expected_sources=sources)
        self.assertEqual(gol.read_state(self.target).sequences, {})

    def testStateRoundTrips(self):
        state = gol.State(
            timestamp="2026-10-01T00:00:00Z",
            sources=["a b c"],
            sequences={"a-latest.osm.pbf": 1, "b c.osm.pbf": 2},
            newest={"a-latest.osm.pbf": "2026-10-01T00:00:00Z"},
            built="2026-10-02T00:00:00Z",
        )
        self.target.parent.mkdir()
        gol._write_state(self.target, state)
        self.assertEqual(gol.read_state(self.target), state)

    def testAnOldStateFileReadsAsWithoutSequences(self):
        self.target.parent.mkdir()
        gol.state_path(self.target).write_text("timestamp=2026-10-01T00:00:00Z\nsource=a 1 2\n")
        state = gol.read_state(self.target)
        self.assertEqual((state.sequences, state.built, state.sources), ({}, None, ["a 1 2"]))


class TestUpdate(GolCase):
    def testRefusesAGolWithoutState(self):
        self.fake_gol("exit 99")
        self.target.parent.mkdir()
        self.target.write_bytes(b"gol")
        with self.assertRaises(gol.GolBuildError):
            gol.update(self.target, {})

    def testRefusesAnExtractTheGolDoesNotHold(self):
        self.fake_gol("exit 99")
        self.target.parent.mkdir()
        self.target.write_bytes(b"gol")
        gol._write_state(self.target, gol.State(sequences={"a.osm.pbf": 1}))
        with self.assertRaises(gol.GolBuildError) as ctx:
            gol.update(self.target, {"b.osm.pbf": (2, [])})
        self.assertIn("b.osm.pbf", str(ctx.exception))

    def testAFailedUpdateLeavesTheGolAsItWas(self):
        # gol scribbles on the copy it was given, then fails.
        self.fake_gol('echo broken > "$2"; exit 3')
        osmium = self.tmp / "osmium-tool"
        osmium.write_text('#!/bin/sh\nwhile [ "$1" != "-o" ]; do shift; done\necho osc > "$2"\n')
        osmium.chmod(0o755)
        patch = mock.patch.dict(os.environ, {"OTM_OSMIUM_BIN": str(osmium)})
        patch.start()
        self.addCleanup(patch.stop)
        osc = self.tmp / "1.osc.gz"
        osc.write_bytes(b"")
        self.target.parent.mkdir()
        self.target.write_bytes(b"gol")
        gol._write_state(self.target, gol.State(sequences={"a.osm.pbf": 1}))
        before = gol.state_path(self.target).read_text()
        with self.assertRaises(gol.subprocess.CalledProcessError):
            gol.update(self.target, {"a.osm.pbf": (2, [osc])})
        self.assertEqual(self.target.read_bytes(), b"gol")
        self.assertEqual(gol.state_path(self.target).read_text(), before)
        self.assertEqual(sorted(p.name for p in self.target.parent.iterdir()), ["regions.gol", "regions.gol.state.txt"])


class TestSparseCopy(GolCase):
    def testContentIsKeptAndZerosBecomeHoles(self):
        # Holes of 128 MB: APFS leaves a seek of a few MB as written zeros,
        # ext4 makes a hole of any whole block.
        hole = 128 * gol._SPARSE_CHUNK
        src = self.tmp / "sparse.bin"
        with src.open("wb") as f:
            f.write(b"head")
            f.seek(hole)
            f.write(b"middle")
            f.seek(2 * hole)
            f.truncate()  # ends in a hole
        dst = self.tmp / "copy.bin"
        gol._copy_sparse(src, dst)
        self.assertEqual(dst.stat().st_size, 2 * hole)
        with src.open("rb") as a, dst.open("rb") as b:
            while chunk := a.read(gol._SPARSE_CHUNK):
                self.assertEqual(b.read(gol._SPARSE_CHUNK), chunk)
        self.assertLess(gol._disk_size(dst), hole // 2)

if __name__ == "__main__":
    unittest.main()
