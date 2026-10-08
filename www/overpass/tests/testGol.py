"""otmlib.gol: several regions becoming the one GOL the service answers from."""

import errno
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

# Collected by the whole-www pytest run too, whose venvs need not have geodesk.
pytest.importorskip("geodesk")

from geodesk import Features  # noqa: E402

from otmlib import gol
from overpass import evaluator
from overpass.library import Library
from overpass.parser import parse
from overpass.tests.golaide import GolTestCase, fixture_pbf, neighbour_pbf


def to_pbf(xml: str, pbf: Path) -> Path:
    osm = pbf.with_suffix("").with_suffix(".osm")
    osm.write_text(xml)
    subprocess.run(["osmium", "cat", str(osm), "-o", str(pbf), "--overwrite"], check=True, capture_output=True)
    return pbf


class GolCase(GolTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # Copies, as datasvc's cache would hold them.
        self.town = self.tmp / "town-latest.osm.pbf"
        self.east = self.tmp / "east-latest.osm.pbf"
        shutil.copyfile(fixture_pbf(), self.town)
        shutil.copyfile(neighbour_pbf(), self.east)

    def found(self, target, text):
        script = parse(text)
        result = evaluator.run(Features(str(target)), script.settings, script.statements, max_elements=1000)
        self.assertIsNone(result.remark)
        return [repr(e) for e in result.blocks[-1].elements]


class TestMerge(GolCase):
    def setUp(self):
        super().setUp()
        self.merged = self.tmp / "gol" / "both.gol"
        gol.build([self.town, self.east], self.merged)

    def testBothRegionsAreThere(self):
        self.assertEqual(self.found(self.merged, "node[amenity=pub];out;"), ["node/1", "node/6"])

    def testBorderWayIsOneElement(self):
        self.assertEqual(self.found(self.merged, "way[highway];out;"), ["way/200", "way/300"])
        self.assertEqual(
            self.found(self.merged, "way[highway];>;out;"), ["node/20", "node/21", "node/22", "node/23"]
        )

    def testQueriesCrossTheBorder(self):
        # Ways meeting at node 22, which both extracts carry.
        self.assertEqual(self.found(self.merged, "way(200);>;way(bn);out;"), ["way/200", "way/300"])

    def testTimestampIsTheOldestPartsNewestEdit(self):
        # The town's newest edit is 2026-09-04, the east's 2026-08-26: the
        # merged data is only known to be current as of the earlier one.
        self.assertEqual(gol.newest_object(self.town), "2026-09-04T20:21:21Z")
        self.assertEqual(Library(self.merged).timestamp(), "2026-08-26T20:22:15Z")

    def testNoHalfBuiltFilesAreLeftBehind(self):
        self.assertEqual(sorted(p.name for p in self.merged.parent.iterdir()), ["both.gol", "both.gol.state.txt"])


class TestWorkDir(GolCase):
    def testBuildsInTheWorkDirAndMovesTheResultIntoPlace(self):
        work = self.tmp / "work"
        target = self.tmp / "gol" / "both.gol"
        with mock.patch.dict(os.environ, {"OTM_GOL_WORK_DIR": str(work)}):
            gol.build([self.town, self.east], target)
        self.assertEqual(self.found(target, "node[amenity=pub];out;"), ["node/1", "node/6"])
        self.assertEqual(list(work.iterdir()), [], "the scratch dir is cleaned up")
        self.assertEqual(sorted(p.name for p in target.parent.iterdir()), ["both.gol", "both.gol.state.txt"])

    def testAcrossFilesystemsTheLastStepIsStillARename(self):
        built = self.tmp / "built.gol"
        built.write_bytes(b"new")
        target = self.tmp / "gol" / "x.gol"
        target.parent.mkdir()
        target.write_bytes(b"old")
        real_replace = os.replace
        calls = []

        def replace(src, dst):
            calls.append((Path(src).name, Path(dst).name))
            if Path(src) == built:
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real_replace(src, dst)

        with mock.patch("otmlib.gol.os.replace", side_effect=replace):
            gol._move_into_place(built, target)
        self.assertEqual(target.read_bytes(), b"new")
        self.assertEqual(calls, [("built.gol", "x.gol"), (".x.gol.incoming", "x.gol")])
        self.assertFalse(target.with_name(".x.gol.incoming").exists())


class TestVersions(GolCase):
    def testNewestVersionWinsWhenExtractsDisagree(self):
        # The east extract has had a diff the town's has not: Main Street's
        # middle node moved and is at version 2. One copy, the new one.
        moved = to_pbf(
            '<osm version="0.6">'
            '<node id="21" version="2" timestamp="2026-10-01T00:00:00Z" lat="50.0300000" lon="10.0200000"/>'
            "</osm>",
            self.tmp / "moved-latest.osm.pbf",
        )
        target = self.tmp / "gol" / "moved.gol"
        gol.build([self.town, moved], target)
        nodes = list(Features(str(target)).way(200).nodes)
        self.assertEqual([n.id for n in nodes], [20, 21, 22])
        self.assertAlmostEqual(nodes[1].lat, 50.03, places=6)


class TestState(GolCase):
    def testStateFilesBesideThePbfAreIgnored(self):
        # datasvc leaves Geofabrik's first state.txt there and never updates it.
        (self.tmp / "east-latest.osm.pbf.state.txt").write_text("timestamp=2001-01-01T00\\:00\\:00Z\n")
        target = self.tmp / "gol" / "east.gol"
        gol.build([self.east], target)
        self.assertEqual(Library(target).timestamp(), "2026-08-26T20:22:15Z")

    def testOneUndatedPartMakesItUnknown(self):
        undated = to_pbf(
            '<osm version="0.6"><node id="7" version="1" lat="50.1" lon="10.1"><tag k="a" v="b"/></node></osm>',
            self.tmp / "undated-latest.osm.pbf",
        )
        target = self.tmp / "gol" / "unknown.gol"
        gol.build([self.east, undated], target)
        # The state file is still there - it lists the inputs - but undated,
        # so the service falls back to the GOL's own mtime.
        self.assertNotIn("timestamp=", gol.state_path(target).read_text())

    def testMissingExtractIsRefusedBeforeAnythingRuns(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            gol.build([self.town, self.tmp / "nope.osm.pbf"], self.tmp / "gol" / "x.gol")
        self.assertIn("nope.osm.pbf", str(ctx.exception))
        self.assertFalse((self.tmp / "gol").exists())


class TestIsCurrent(GolCase):
    def setUp(self):
        super().setUp()
        self.pbfs = [self.town, self.east]
        self.target = self.tmp / "gol" / "both.gol"

    def testNoGolIsNotCurrent(self):
        self.assertFalse(gol.is_current(self.pbfs, self.target))

    def testFreshBuildIsCurrent(self):
        gol.build(self.pbfs, self.target)
        self.assertTrue(gol.is_current(self.pbfs, self.target))
        self.assertEqual(gol.built_from(self.target), gol.fingerprint(self.pbfs))

    def testRewrittenExtractIsNot(self):
        gol.build(self.pbfs, self.target)
        # What datasvc does with a diff: a new file renamed over the old one.
        fresh = self.tmp / "east.new"
        shutil.copyfile(neighbour_pbf(), fresh)
        os.utime(fresh, ns=(self.east.stat().st_mtime_ns + 10**9,) * 2)
        os.replace(fresh, self.east)
        self.assertFalse(gol.is_current(self.pbfs, self.target))

    def testDifferentRegionListIsNot(self):
        gol.build(self.pbfs, self.target)
        self.assertFalse(gol.is_current([self.town], self.target))


if __name__ == "__main__":
    unittest.main()
