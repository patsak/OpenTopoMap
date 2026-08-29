import unittest

from mapsvc.names import GARMIN_NAME_MAX, garmin_map_names


class TestGarminMapNames(unittest.TestCase):
    def testDefaultWhenEmpty(self):
        base, contours = garmin_map_names("  ")
        self.assertEqual(base, "OpenTopoMap Hike")
        self.assertTrue(contours.endswith("Contours"))

    def testStripsUnsafeCharacters(self):
        base, _contours = garmin_map_names('Arkhuz = "west"')
        self.assertNotIn("=", base)
        self.assertNotIn('"', base)

    def testContoursFitsGarminLimit(self):
        base, contours = garmin_map_names("A" * 80)
        self.assertLessEqual(len(base), GARMIN_NAME_MAX)
        self.assertLessEqual(len(contours), GARMIN_NAME_MAX)
        self.assertTrue(contours.endswith("Contours"))


if __name__ == "__main__":
    unittest.main()
