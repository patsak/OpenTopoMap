import math
import shutil
import tempfile
import unittest
from pathlib import Path

from otmlib.crevasse import build_crevasse_stripes, extract_crevasse_stripes, parse_direction
from otmlib.tests.crevasseaide import glacierScene


def _tickMetres(line) -> tuple[float, float]:
    (lon0, lat0), (lon1, lat1) = line.coords[0], line.coords[-1]
    m_lat = 111_320.0
    m_lon = m_lat * math.cos(math.radians((lat0 + lat1) / 2.0))
    return abs(lon1 - lon0) * m_lon, abs(lat1 - lat0) * m_lat


class TestParseDirection(unittest.TestCase):
    def testCardinalAndIntercardinal(self):
        self.assertEqual(parse_direction("N"), 0.0)
        self.assertEqual(parse_direction("NE"), 45.0)
        self.assertEqual(parse_direction("nne"), 22.5)
        self.assertEqual(parse_direction("east"), 90.0)

    def testNumericAndRange(self):
        self.assertEqual(parse_direction("90"), 90.0)
        self.assertEqual(parse_direction("90°"), 90.0)
        self.assertEqual(parse_direction("45-90"), 67.5)

    def testRejectsEmptyAndGarbage(self):
        self.assertIsNone(parse_direction(None))
        self.assertIsNone(parse_direction(""))
        self.assertIsNone(parse_direction("uphill"))


@unittest.skipUnless(shutil.which("osmium"), "osmium-tool is required")
class TestCrevasseHatch(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def testTicksRunNorthSouthAcrossEastwardGlacier(self):
        src = glacierScene(self.tmp / "scene.osm", direction="E")
        lines = extract_crevasse_stripes(src)
        self.assertGreater(len(lines), 3)
        dlon = [_tickMetres(line)[0] for line, _ in lines]
        dlat = [_tickMetres(line)[1] for line, _ in lines]
        # Perpendicular to east is north–south: latitude span dominates.
        self.assertGreater(sorted(dlat)[len(dlat) // 2], 2 * sorted(dlon)[len(dlon) // 2])

    def testTicksRunEastWestAcrossNorthwardGlacier(self):
        src = glacierScene(self.tmp / "scene.osm", direction="N")
        lines = extract_crevasse_stripes(src)
        self.assertGreater(len(lines), 3)
        dlon = [_tickMetres(line)[0] for line, _ in lines]
        dlat = [_tickMetres(line)[1] for line, _ in lines]
        self.assertGreater(sorted(dlon)[len(dlon) // 2], 2 * sorted(dlat)[len(dlat) // 2])

    def testSkipsGlacierWithoutDirection(self):
        src = glacierScene(self.tmp / "scene.osm", direction=None)
        self.assertEqual(extract_crevasse_stripes(src), [])

    def testSkipsCrevasseOutsideGlacier(self):
        src = glacierScene(self.tmp / "scene.osm", direction="E", crevasse="outside")
        self.assertEqual(extract_crevasse_stripes(src), [])

    def testWritesOsmWhenTicksExist(self):
        src = glacierScene(self.tmp / "scene.osm", direction="SE")
        out = self.tmp / "crevasse-stripes.osm"
        self.assertEqual(build_crevasse_stripes(src, out), out)
        self.assertTrue(out.is_file())
        text = out.read_text(encoding="utf-8")
        self.assertIn('k="crevasse"', text)
        self.assertIn('v="stripe', text)


if __name__ == "__main__":
    unittest.main()
