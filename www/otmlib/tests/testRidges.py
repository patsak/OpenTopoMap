import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from otmlib.ridges import build_ridges, extract_osm_ridges

# Crest lines around 43.30°N 42.40°E (Elbrus-ish).
_SCENE = """<?xml version='1.0' encoding='UTF-8'?>
<osm version="0.6">
  <node id="1" lat="43.3000" lon="42.4000"/>
  <node id="2" lat="43.3050" lon="42.4100"/>
  <node id="3" lat="43.3100" lon="42.4200"/>
  <node id="4" lat="43.3200" lon="42.4300"/>
  <node id="5" lat="43.3250" lon="42.4400"/>
  <way id="10">
    <nd ref="1"/><nd ref="2"/><nd ref="3"/>
    <tag k="natural" v="ridge"/>
    <tag k="name" v="Khrebet &amp; Co"/>
  </way>
  <way id="11">
    <nd ref="3"/><nd ref="4"/>
    <tag k="natural" v="arete"/>
  </way>
  <way id="12">
    <nd ref="4"/><nd ref="5"/>
    <tag k="natural" v="ridge"/>
    <tag k="geological" v="moraine"/>
  </way>
  <way id="13">
    <nd ref="1"/><nd ref="5"/>
    <tag k="highway" v="path"/>
  </way>
</osm>
"""


class TestRidges(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.scene = self.tmp / "scene.osm"
        self.scene.write_text(_SCENE, encoding="utf-8")

    def testKeepsRidgeAndAreteOnly(self):
        items = extract_osm_ridges(self.scene)
        self.assertEqual(len(items), 3)
        self.assertEqual([tags["natural"] for _, tags in items], ["ridge", "arete", "ridge"])
        self.assertTrue(all(tags["contour"] == "ridge" for _, tags in items))

    def testKeepsNameAndMoraine(self):
        tags = [t for _, t in extract_osm_ridges(self.scene)]
        self.assertEqual(tags[0]["name"], "Khrebet & Co")
        self.assertEqual(tags[2]["geological"], "moraine")
        self.assertNotIn("geological", tags[1])

    def testWritesOsmXml(self):
        out = build_ridges(self.scene, self.tmp / "out" / "ridges.osm")
        self.assertIsNotNone(out)
        root = ET.parse(out).getroot()
        self.assertEqual(len(root.findall("way")), 3)
        # 3 + 2 + 2 nodes, each way carrying its own copy.
        self.assertEqual(len(root.findall("node")), 7)

    def testNoRidgesWritesNothing(self):
        empty = self.tmp / "empty.osm"
        empty.write_text("<?xml version='1.0' encoding='UTF-8'?>\n<osm version=\"0.6\"/>\n", encoding="utf-8")
        out = self.tmp / "ridges.osm"
        out.write_text("stale", encoding="utf-8")
        self.assertIsNone(build_ridges(empty, out))
        self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
