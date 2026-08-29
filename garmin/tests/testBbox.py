import unittest

from mapsvc.bbox import MAX_BBOX_SIDE_KM, parse_bbox, validate_bbox_size


class TestParseBbox(unittest.TestCase):
    def testParsesAndNormalizesSwappedCorners(self):
        west, south, east, north = parse_bbox(
            {"west": 42.5, "south": 43.4, "east": 42.0, "north": 43.0}
        )
        self.assertEqual((west, south, east, north), (42.0, 43.0, 42.5, 43.4))

    def testRejectsMissingField(self):
        with self.assertRaises(ValueError):
            parse_bbox({"west": 42.0, "south": 43.0, "east": 42.5})

    def testRejectsZeroSize(self):
        with self.assertRaises(ValueError):
            parse_bbox({"west": 42.0, "south": 43.0, "east": 42.0, "north": 43.4})

    def testRejectsLongitudeOutOfRange(self):
        with self.assertRaises(ValueError):
            parse_bbox({"west": -181.0, "south": 43.0, "east": 42.0, "north": 43.4})

    def testRejectsTooLargeSide(self):
        with self.assertRaises(ValueError) as ctx:
            validate_bbox_size(0.0, 0.0, 20.0, 20.0)
        self.assertIn(f"{MAX_BBOX_SIDE_KM:.0f}", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
