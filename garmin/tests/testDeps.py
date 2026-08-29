import tempfile
import unittest
from pathlib import Path

from mapsvc.deps import ConsoleProgress, data_present, download_percent, human_bytes


class TestDownloadHelpers(unittest.TestCase):
    def testPercentNoneWithoutTotal(self):
        self.assertIsNone(download_percent(10, None))
        self.assertIsNone(download_percent(10, 0))

    def testPercentCapsAtHundred(self):
        self.assertEqual(download_percent(0, 100), 0)
        self.assertEqual(download_percent(50, 100), 50)
        self.assertEqual(download_percent(150, 100), 100)

    def testHumanBytes(self):
        self.assertEqual(human_bytes(512), "512B")
        self.assertEqual(human_bytes(2048), "2.0KB")
        self.assertEqual(human_bytes(3 * 1024 * 1024), "3.0MB")

    def testDataPresentLooksForMarker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertFalse(data_present(root / "missing", "sea_*"))
            sea = root / "sea"
            sea.mkdir()
            self.assertFalse(data_present(sea, "sea_*"))
            (sea / "sea_0.pbf").write_bytes(b"x")
            self.assertTrue(data_present(sea, "sea_*"))

    def testConsoleProgressThrottlesToFivePercent(self):
        lines: list[str] = []
        progress = ConsoleProgress(lines.append)
        progress("sea.zip", 0, 100)
        progress("sea.zip", 3, 100)
        progress("sea.zip", 5, 100)
        progress("sea.zip", 100, 100)
        self.assertEqual(lines[0], "sea.zip: 0% (0B / 100B)")
        self.assertEqual(lines[1], "sea.zip: 5% (5B / 100B)")
        self.assertEqual(lines[-1], "sea.zip: 100% (100B / 100B)")
        self.assertEqual(len(lines), 3)


if __name__ == "__main__":
    unittest.main()
