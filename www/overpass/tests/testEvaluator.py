"""Queries over the fixture town (tests/fixture.osm), answers worked out by hand."""

import unittest
from unittest import mock

import pytest

# Collected by the whole-www pytest run too, whose venvs need not have geodesk.
pytest.importorskip("geodesk")

from geodesk import Features  # noqa: E402

from overpass import evaluator
from overpass.parser import parse
from overpass.tests.golaide import GolTestCase


class EvaluatorCase(GolTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.world = Features(str(cls.gol))

    def run_script(self, text, max_elements=1000):
        script = parse(text)
        return evaluator.run(self.world, script.settings, script.statements, max_elements=max_elements)

    def found(self, text):
        """The elements of the last out, as "kind/id" strings in output order."""
        result = self.run_script(text if "out" in text else text + "out;")
        self.assertIsNone(result.remark)
        return [repr(e) for e in result.blocks[-1].elements]


class TestTags(EvaluatorCase):
    def testEquality(self):
        self.assertEqual(self.found("node[amenity=pub];"), ["node/1"])

    def testRegexAlternative(self):
        self.assertEqual(self.found('node[amenity~"^(pub|bar)$"];'), ["node/1", "node/2"])

    def testCaseInsensitiveRegex(self):
        self.assertEqual(self.found('node[name~"anchor",i];'), ["node/1"])

    def testNumberShapedValuesCompareAsStrings(self):
        # A GOL stores ele=1200 as a number; GOQL's [ele="1200"] and ~"^12"
        # would miss it. Overpass compares strings and finds it.
        self.assertEqual(self.found('node["ele"="1200"];'), ["node/4"])
        self.assertEqual(self.found('node[ele~"^12"];'), ["node/4"])
        self.assertEqual(self.found(r'node["addr:housenumber"~"^\d+$"];'), ["node/5"])

    def testNotEqualMatchesMissingKeys(self):
        self.assertEqual(self.found("node[amenity][amenity!=pub];"), ["node/2", "node/3"])
        self.assertEqual(self.found('node[name][amenity!~"^p"];'), ["node/2", "node/3", "node/4", "node/5"])

    def testAbsenceAndKeyRegex(self):
        self.assertEqual(self.found("node[!amenity][shop];"), ["node/5", "node/7"])
        self.assertEqual(self.found('node[~"^addr:"~"."];'), ["node/5"])

    def testKeyPresentIncludesNo(self):
        # GOQL's [shop] leaves out shop=no; Overpass's does not.
        self.assertEqual(self.found("node[shop];"), ["node/5", "node/7"])
        self.assertEqual(self.found("node[shop=no];"), ["node/7"])
        self.assertEqual(self.found('node[shop~"o"];'), ["node/7"])

    def testNegationsOnRareKeysKeepEverythingElse(self):
        # GeoDesk 2.3 drops features from [!k] next to another clause when k
        # is rare; here only node 7 has opening_hours:covid19.
        self.assertEqual(
            self.found('node[name][!opening_hours][!"opening_hours:covid19"];'),
            ["node/1", "node/2", "node/3", "node/4", "node/5"],
        )

    def testStarIsNotAGlob(self):
        # GOQL would read "pu*" as a glob and find the pub.
        self.assertEqual(self.found('node[amenity="pu*"];'), [])

    def testUnicodeValue(self):
        self.assertEqual(self.found('node[name="Café"];'), ["node/3"])

    def testCombinedTypes(self):
        self.assertEqual(
            self.found("nwr[name];"),
            ["node/1", "node/2", "node/3", "node/4", "node/5", "way/101", "way/200", "relation/1000", "relation/2000"],
        )
        self.assertEqual(self.found("wr[name];"), ["way/101", "way/200", "relation/1000", "relation/2000"])

    def testWayIncludesClosedWays(self):
        # A building is an area in GeoDesk ("a"), but a way to Overpass.
        self.assertEqual(self.found("way[building];"), ["way/101"])


class TestIds(EvaluatorCase):
    def testLookups(self):
        self.assertEqual(self.found("node(1);"), ["node/1"])
        self.assertEqual(self.found("node(id:2,1,999);"), ["node/1", "node/2"])
        self.assertEqual(self.found("way(101);"), ["way/101"])
        self.assertEqual(self.found("rel(1000);"), ["relation/1000"])

    def testIdAndTagTogether(self):
        self.assertEqual(self.found("node(id:1,2)[amenity=bar];"), ["node/2"])


class TestSpatial(EvaluatorCase):
    def testBbox(self):
        self.assertEqual(self.found("node(50.0,10.0,50.025,10.025)[amenity];"), ["node/1", "node/2"])

    def testGlobalBbox(self):
        self.assertEqual(self.found("[bbox:50.0,10.0,50.025,10.025];node[amenity];"), ["node/1", "node/2"])

    def testBboxGoesByTheLineNotItsBox(self):
        # The box sits inside Testtown's outline: way 100's bounding box covers
        # it, the way itself does not touch it. Main Street crosses it.
        self.assertEqual(self.found("way(50.015,10.015,50.025,10.025);"), ["way/200"])

    def testAroundPoint(self):
        self.assertEqual(self.found("node(around:500,50.01,10.01)[amenity];"), ["node/1"])

    def testAroundSet(self):
        self.assertEqual(self.found("node[amenity=pub]->.p;node(around.p:2000)[amenity];"), ["node/1", "node/2"])

    def testAroundManyCentresGoesByTheirExtentAndAnIndex(self):
        # Past AROUND_QUERIES_MAX centres the GOL is asked once, over their
        # extent, and the distance is tested against an index of them.
        with mock.patch.object(evaluator, "AROUND_QUERIES_MAX", 0):
            self.assertEqual(self.found("node[amenity=pub]->.p;node(around.p:2000)[amenity];"), ["node/1", "node/2"])
            self.assertEqual(self.found("node[name]->.n;way(around.n:60);"), ["way/200"])

    def testAroundSetOnAnInputSetUsesTheIndexToo(self):
        # node.h(around.w:100): the candidates come from a set, so every one
        # is tested - this is the query that used to be quadratic.
        self.assertEqual(self.found("way(200);>->.h;node(2)->.w;node.h(around.w:100);"), ["node/21"])

    def testAroundFindsWaysByTheirLine(self):
        self.assertEqual(self.found("way(around:100,50.02,10.02);"), ["way/200"])

    def testPoly(self):
        self.assertEqual(self.found('node(poly:"50.0 10.0 50.0 10.015 50.015 10.015 50.015 10.0");'), ["node/1"])

    def testSpatialFilterOnAnInputSet(self):
        self.assertEqual(self.found("node[name]->.n;node.n(50.0,10.0,50.025,10.025);"), ["node/1", "node/2"])


class TestAreas(EvaluatorCase):
    def testAreaIdsFollowOverpass(self):
        self.assertEqual(self.found("area[name=Testtown];"), ["area/3600001000"])
        self.assertEqual(self.found("area[building];"), ["area/2400000101"])

    def testInsideAreaFromSet(self):
        self.assertEqual(self.found("area[name=Testtown]->.a;node(area.a)[amenity];"), ["node/1", "node/2"])

    def testInsideAreaById(self):
        # Untagged nodes are not features in a GOL, so unlike Overpass this
        # returns only the tagged ones.
        self.assertEqual(self.found("node(area:3600001000);"), ["node/1", "node/2", "node/5"])
        self.assertEqual(self.found("way(area:3600001000);"), ["way/100", "way/101", "way/200"])

    def testMapToArea(self):
        self.assertEqual(self.found("rel(1000);map_to_area;"), ["area/3600001000"])
        self.assertEqual(self.found("rel(1000);map_to_area;node(area)[amenity];"), ["node/1", "node/2"])

    def testRelationThatIsNoAreaMapsToNothing(self):
        self.assertEqual(self.found("rel(2000);map_to_area;"), [])

    def testIsIn(self):
        self.assertEqual(self.found("is_in(50.011,10.031);"), ["area/2400000101", "area/3600001000"])
        self.assertEqual(self.found("node(1);is_in;"), ["area/3600001000"])

    def testPivot(self):
        self.assertEqual(self.found("area[name=Testtown];rel(pivot);"), ["relation/1000"])

    def testAreaQueriesNeedAreasInTheSet(self):
        # As in Overpass: a relation in the set is not an area.
        self.assertEqual(self.found("rel(1000)->.r;node(area.r)[amenity];"), [])


class TestRecursion(EvaluatorCase):
    def testDownFromWay(self):
        self.assertEqual(self.found("way(200);>;"), ["node/20", "node/21", "node/22"])

    def testUnionWithDown(self):
        self.assertEqual(self.found("way(200);(._;>;);"), ["node/20", "node/21", "node/22", "way/200"])

    def testDownFromRelationSkipsSubrelations(self):
        self.assertEqual(self.found("rel(2000);>;"), ["node/1", "node/20", "node/21", "node/22", "way/200"])

    def testDownRecursive(self):
        self.assertEqual(
            self.found("rel(2000);>>;"),
            [
                "node/1", "node/10", "node/11", "node/12", "node/13",
                "node/20", "node/21", "node/22",
                "way/100", "way/200", "relation/1000",
            ],
        )

    def testUp(self):
        self.assertEqual(self.found("node(1);<;"), ["relation/2000"])
        # Nodes → their way → the relation holding that way.
        self.assertEqual(self.found("way(200);>;<;"), ["way/200", "relation/2000"])

    def testUpFromRelationNeedsDoubleArrow(self):
        self.assertEqual(self.found("rel(1000);<;"), [])
        self.assertEqual(self.found("rel(1000);<<;"), ["relation/2000"])

    def testRecurseFilters(self):
        self.assertEqual(self.found("way(200);node(w);"), ["node/20", "node/21", "node/22"])
        self.assertEqual(self.found("rel(2000);way(r);"), ["way/200"])
        self.assertEqual(self.found('rel(2000);node(r:"stop");'), ["node/1"])
        self.assertEqual(self.found("rel(2000);rel(r);"), ["relation/1000"])
        self.assertEqual(self.found("way(200);>;way(bn);"), ["way/200"])
        self.assertEqual(self.found("way(200);rel(bw);"), ["relation/2000"])
        self.assertEqual(self.found("rel(1000);rel(br);"), ["relation/2000"])

    def testBackwardRoles(self):
        self.assertEqual(self.found('node(1);rel(bn:"stop");'), ["relation/2000"])
        self.assertEqual(self.found('node(1);rel(bn:"platform");'), [])


class TestIf(EvaluatorCase):
    def testTagComparison(self):
        self.assertEqual(self.found('node[amenity](if: t["name"] == "The Anchor");'), ["node/1"])

    def testNumbers(self):
        self.assertEqual(self.found('node[natural](if: number(t["ele"]) > 1000);'), ["node/4"])
        self.assertEqual(self.found('node[ele](if: t["ele"] + 1 == 1201);'), ["node/4"])

    def testFunctions(self):
        self.assertEqual(self.found("way[name](if: is_closed());"), ["way/101"])
        self.assertEqual(self.found("node[name](if: count_tags() >= 3);"), ["node/4", "node/5"])
        self.assertEqual(self.found('node[name](if: id() < 3 && type() == "node");'), ["node/1", "node/2"])
        self.assertEqual(self.found('node[name](if: !is_tag("amenity"));'), ["node/4", "node/5"])
        self.assertEqual(self.found("rel[name](if: count_members() == 3);"), ["relation/2000"])

    def testLength(self):
        # The Hall spans 0.002°: about 222 m north-south and 143 m east-west
        # at 50°N, so it is about 730 m round.
        self.assertEqual(self.found("way[building](if: length() > 700 && length() < 760);"), ["way/101"])


class TestCount(EvaluatorCase):
    def testPlacesWithNoWayNearby(self):
        # The shape of a real query: for each point, gather the ways around it
        # together with the point, and keep the point when there are none.
        # The bar has Main Street 55 m away; the pub and the cafe have nothing
        # within 100 m.
        result = self.run_script(
            "node[amenity]->.c;"
            "foreach .c -> .d ("
            "  (way(around.d:100); .d;);"
            "  node._(if:count(ways) == 0);"
            "  out;"
            ");"
        )
        self.assertIsNone(result.remark)
        self.assertEqual([repr(e) for b in result.blocks for e in b.elements], ["node/1", "node/3"])

    def testCountsTheDefaultSetByType(self):
        self.assertEqual(
            self.found("(way(200); >;);node._(if: count(nodes) == 3 && count(ways) == 1 && count(nwr) == 4);"),
            ["node/20", "node/21", "node/22"],
        )


class TestSetsAndBlocks(EvaluatorCase):
    def testDifference(self):
        self.assertEqual(self.found("(node[amenity]; - node[amenity=pub];);"), ["node/2", "node/3"])

    def testNamedSetsSurviveLaterStatements(self):
        self.assertEqual(self.found("node[amenity=pub]->.p;way(200);.p out;"), ["node/1"])

    def testUndefinedSetIsEmpty(self):
        self.assertEqual(self.found(".nothing out;"), [])

    def testForeachRunsTheBodyPerElement(self):
        result = self.run_script("node[amenity];foreach->.n{.n out ids;}")
        self.assertEqual([[repr(e) for e in b.elements] for b in result.blocks], [["node/1"], ["node/2"], ["node/3"]])

    def testOutLimitAndCount(self):
        self.assertEqual(self.found("node[name];out 2;"), ["node/1", "node/2"])
        result = self.run_script("nwr[name];out count;")
        self.assertEqual(result.blocks[0].counts, {"node": 5, "way": 2, "relation": 2, "area": 0})


class TestLimits(EvaluatorCase):
    def testTimeoutIsARuntimeRemark(self):
        result = self.run_script("[timeout:0];node[amenity];out;")
        self.assertTrue(result.remark.startswith("runtime error: Query timed out"))

    def testTooManyElementsIsARuntimeRemark(self):
        result = self.run_script("node[amenity=pub];out;node[amenity];out;", max_elements=2)
        self.assertIn("ran out of memory", result.remark)
        # What was printed before the error still goes out.
        self.assertEqual([repr(e) for e in result.blocks[0].elements], ["node/1"])

    def testTheLimitCountsMatchesNotCandidates(self):
        # A key regex cannot narrow the GOL query, so every node streams by;
        # only the one match is held, and a limit of 2 is not exceeded.
        result = self.run_script('node[~"^addr:"~"."];out;', max_elements=2)
        self.assertIsNone(result.remark)
        self.assertEqual([repr(e) for e in result.blocks[0].elements], ["node/5"])

    def testBboxWithoutSettingIsARuntimeRemark(self):
        self.assertIn("[bbox:", self.run_script("node(bbox);out;").remark)


if __name__ == "__main__":
    unittest.main()
