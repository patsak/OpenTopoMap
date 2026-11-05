"""otmlib.gol: several regions becoming the one GOL the service answers from."""

import errno
import gzip
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


def to_osc(xml: str, osc: Path) -> Path:
    with gzip.open(osc, "wt", encoding="utf-8") as f:
        f.write(f'<?xml version="1.0" encoding="UTF-8"?><osmChange version="0.6">{xml}</osmChange>')
    return osc


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


# Node 22 is where the two extracts meet: both carry it, and both diffs move it.
BORDER_MOVE = (
    '<modify><node id="22" version="2" timestamp="2026-10-01T10:00:00Z" lat="50.0250000" lon="10.0300000"/>'
    "</modify>"
)


class TestUpdate(GolCase):
    def setUp(self):
        super().setUp()
        self.target = self.tmp / "gol" / "both.gol"
        names = {self.town.name: 1, self.east.name: 1}
        gol.build([self.town, self.east], self.target, sequences=names)
        self.town_osc = to_osc(
            '<modify><node id="1" version="2" timestamp="2026-10-01T12:00:00Z" lat="50.0100000" lon="10.0100000">'
            '<tag k="amenity" v="pub"/><tag k="name" v="The Crown"/></node></modify>' + BORDER_MOVE,
            self.tmp / "town-2.osc.gz",
        )
        self.east_osc = to_osc(
            '<create><node id="9" version="1" timestamp="2026-10-02T12:00:00Z" lat="50.0200000" lon="10.0900000">'
            '<tag k="amenity" v="pub"/><tag k="name" v="New Inn"/></node></create>'
            '<delete><node id="6" version="2" timestamp="2026-10-02T13:00:00Z" lat="50.0200000" lon="10.0800000"/>'
            "</delete>" + BORDER_MOVE,
            self.tmp / "east-2.osc.gz",
        )
        self.changes = {self.town.name: (2, [self.town_osc]), self.east.name: (2, [self.east_osc])}

    def testTheDiffsAreInTheGol(self):
        gol.update(self.target, self.changes)
        self.assertEqual(self.found(self.target, "node[amenity=pub];out;"), ["node/1", "node/9"])
        features = Features(str(self.target))
        self.assertEqual(features.node(1)["name"], "The Crown")
        nodes = list(features.way(200).nodes)
        self.assertEqual([n.id for n in nodes], [20, 21, 22])
        self.assertAlmostEqual(nodes[2].lat, 50.025, places=6)

    def testItIsTheGolABuildFromThePatchedExtractsGives(self):
        gol.update(self.target, self.changes)
        patched = []
        for pbf, osc in ((self.town, self.town_osc), (self.east, self.east_osc)):
            out = self.tmp / f"patched-{pbf.name}"
            subprocess.run(
                ["osmium", "apply-changes", str(pbf), str(osc), "-o", str(out)], check=True, capture_output=True
            )
            patched.append(out)
        rebuilt = self.tmp / "gol" / "rebuilt.gol"
        gol.build(patched, rebuilt)
        for query in ("node[amenity];out;", "way[highway];>;out;", "way(200);>;way(bn);out;", "node(50,10,51,11);out;"):
            with self.subTest(query):
                self.assertEqual(self.found(self.target, query), self.found(rebuilt, query))

    def testTheStateMovesOn(self):
        built = gol.read_state(self.target).built
        gol.update(self.target, self.changes, sources=["new"])
        state = gol.read_state(self.target)
        self.assertEqual(state.sequences, {self.town.name: 2, self.east.name: 2})
        # The oldest of the regions' newest edits: the town's diff, 2026-10-01.
        self.assertEqual(Library(self.target).timestamp(), "2026-10-01T12:00:00Z")
        self.assertEqual((state.built, state.sources), (built, ["new"]))

    def testAnExtractWithoutDiffsKeepsItsSequence(self):
        gol.update(self.target, {self.town.name: (2, [self.town_osc])})
        self.assertEqual(gol.read_state(self.target).sequences, {self.town.name: 2, self.east.name: 1})

    def testTheServiceHoldingTheGolOpenDoesNotStopIt(self):
        # gol locks a GOL it updates against readers; the copy it updates has
        # none. (The reader keeps the old GOL - overpass.library.)
        reader = Features(str(self.target))
        self.assertEqual(len(list(reader("n[amenity=pub]"))), 2)
        gol.update(self.target, self.changes)
        self.assertEqual(gol.read_state(self.target).sequences[self.town.name], 2)
        check = subprocess.run(
            [gol.gol_binary(), "query", str(self.target), "n[amenity=pub][name='The Crown']", "-f", "count"],
            check=True, capture_output=True, text=True,
        )
        self.assertEqual(check.stdout.strip(), "1")

    def testNoScratchFilesAreLeftBehind(self):
        work = self.tmp / "work"
        with mock.patch.dict(os.environ, {"OTM_GOL_WORK_DIR": str(work)}):
            gol.update(self.target, self.changes)
        self.assertEqual(list(work.iterdir()), [])
        self.assertEqual(sorted(p.name for p in self.target.parent.iterdir()), ["both.gol", "both.gol.state.txt"])
