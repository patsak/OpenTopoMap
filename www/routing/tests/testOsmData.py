"""Reading the area's OSM extract."""

import tempfile
import unittest
from pathlib import Path

from routing.build import osmdata

FIXTURE = """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6">
  <node id="1" lat="67.70" lon="33.70"/>
  <node id="2" lat="67.71" lon="33.70"/>
  <node id="3" lat="67.72" lon="33.70"><tag k="mountain_pass" v="yes"/><tag k="name" v="Рамзая"/></node>
  <node id="4" lat="67.70" lon="33.71"/>
  <node id="5" lat="67.71" lon="33.71"><tag k="ford" v="yes"/></node>
  <node id="6" lat="67.70" lon="33.72"/>
  <node id="7" lat="67.70" lon="33.73"/>
  <node id="8" lat="67.71" lon="33.73"/>
  <node id="9" lat="67.71" lon="33.72"/>
  <node id="10" lat="67.715" lon="33.71"><tag k="waterway" v="waterfall"/></node>
  <way id="100"><nd ref="1"/><nd ref="2"/><nd ref="3"/>
    <tag k="highway" v="path"/><tag k="sac_scale" v="mountain_hiking"/></way>
  <way id="101"><nd ref="4"/><nd ref="5"/><nd ref="10"/>
    <tag k="waterway" v="river"/><tag k="width" v="12 m"/></way>
  <way id="102"><nd ref="6"/><nd ref="7"/><nd ref="8"/><nd ref="9"/><nd ref="6"/>
    <tag k="natural" v="scrub"/></way>
  <way id="103"><nd ref="1"/><nd ref="4"/><tag k="highway" v="track"/><tag k="foot" v="no"/></way>
  <way id="104"><nd ref="2"/><nd ref="5"/><tag k="barrier" v="fence"/></way>
  <way id="105"><nd ref="6"/><nd ref="7"/><tag k="natural" v="valley"/><tag k="valley" v="canyon"/></way>
</osm>
"""


class TestRead(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = Path(tempfile.mkdtemp()) / "fixture.osm"
        cls.path.write_text(FIXTURE, encoding="utf-8")
        cls.data = osmdata.read(cls.path, (("barrier", "fence"),))

    def testRoutableWaysOnly(self):
        self.assertEqual([w.id for w in self.data.routable], [100])
        way = self.data.routable[0]
        self.assertEqual(way.nodes.tolist(), [1, 2, 3])
        self.assertEqual(way.tags["sac_scale"], "mountain_hiking")

    def testPointsOfInterest(self):
        self.assertEqual([(p.id, p.tags.get("name")) for p in self.data.passes], [(3, "Рамзая")])
        self.assertEqual([p.id for p in self.data.fords], [5])
        self.assertEqual([p.id for p in self.data.waterfalls], [10])

    def testLinesAndAreas(self):
        [(line, tags)] = self.data.waterways
        self.assertEqual(tags["width"], "12 m")
        self.assertEqual(len(line.coords), 3)
        self.assertEqual([tag for _, tag in self.data.barrier_lines], ["barrier=fence"])
        self.assertEqual(len(self.data.canyon_lines), 1)
        self.assertEqual(len(self.data.terrain_areas["scrub"]), 1)


class TestTags(unittest.TestCase):
    def testFootAccess(self):
        yes = osmdata.is_foot_routable
        self.assertTrue(yes({"highway": "path"}))
        self.assertTrue(yes({"highway": "service", "access": "private", "foot": "yes"}))
        self.assertFalse(yes({"highway": "service", "access": "private"}))
        self.assertFalse(yes({"highway": "path", "foot": "no"}))
        self.assertFalse(yes({"highway": "motorway"}))
        self.assertTrue(yes({"highway": "trunk", "foot": "yes"}))
        self.assertFalse(yes({"highway": "pedestrian", "area": "yes"}))
        self.assertFalse(yes({"highway": "proposed"}))

    def testWidth(self):
        self.assertEqual(osmdata.parse_width("12"), 12.0)
        self.assertEqual(osmdata.parse_width("5,5 m"), 5.5)
        self.assertIsNone(osmdata.parse_width("wide"))
        self.assertIsNone(osmdata.parse_width(None))


if __name__ == "__main__":
    unittest.main()
