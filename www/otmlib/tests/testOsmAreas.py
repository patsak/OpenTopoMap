import shutil
import tempfile
import unittest
from pathlib import Path

from otmlib.osm_areas import filtered_subset, glacier_subset, load_area_geoms
from otmlib.tests.crevasseaide import glacierScene


class GlacierSubsetCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.scene = glacierScene(self.tmp / "scene.osm", forest=True)
        self.output = self.tmp / "cache" / "glaciers.osm.pbf"


class TestGlacierSubset(GlacierSubsetCase):
    def testWritesThePersistentFileTheJobPublishes(self):
        result = glacier_subset(self.scene, self.output)
        self.assertEqual(result, self.output)
        self.assertTrue(self.output.is_file())
        self.assertGreater(self.output.stat().st_size, 0)

    def testKeepsIceAndCrevasses(self):
        glacier_subset(self.scene, self.output)
        self.assertEqual(len(load_area_geoms(self.output, "natural", "glacier", prefiltered=True)), 1)
        self.assertEqual(
            len(load_area_geoms(self.output, "natural", "crevasse", prefiltered=True)), 1
        )

    def testDropsEverythingElse(self):
        glacier_subset(self.scene, self.output)
        self.assertEqual(load_area_geoms(self.output, "natural", "wood", prefiltered=True), [])

    def testLeavesNoPartialFileBehind(self):
        glacier_subset(self.scene, self.output)
        leftovers = [p.name for p in self.output.parent.iterdir() if ".part" in p.name]
        self.assertEqual(leftovers, [])

    def testRerunReplacesTheOldSubset(self):
        glacier_subset(self.scene, self.output)
        first = self.output.stat().st_size
        without_ice = glacierScene(self.tmp / "bare.osm", crevasse=None)
        glacier_subset(without_ice, self.output)
        self.assertNotEqual(
            len(load_area_geoms(self.output, "natural", "crevasse", prefiltered=True)), 1
        )
        self.assertGreater(first, 0)


class TestFilteredSubset(GlacierSubsetCase):
    def testAppliesTheGivenOsmiumExpressions(self):
        filtered_subset(self.scene, ["nwr/natural=wood"], self.output)
        self.assertEqual(len(load_area_geoms(self.output, "natural", "wood", prefiltered=True)), 1)
        self.assertEqual(load_area_geoms(self.output, "natural", "glacier", prefiltered=True), [])


if __name__ == "__main__":
    unittest.main()
