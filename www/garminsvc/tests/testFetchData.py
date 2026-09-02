import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from garminsvc import fetchdata


class TestCheck(unittest.TestCase):
    def testExitsNonZeroWhenDataIsAbsent(self):
        with mock.patch.object(fetchdata, "sea_bounds_ready", return_value=False):
            self.assertEqual(fetchdata.main(["--check"]), 1)

    def testExitsZeroWhenDataIsPresent(self):
        with mock.patch.object(fetchdata, "sea_bounds_ready", return_value=True):
            self.assertEqual(fetchdata.main(["--check"]), 0)

    def testCheckDownloadsNothing(self):
        with mock.patch.object(fetchdata, "download_sea_bounds") as download:
            with mock.patch.object(fetchdata, "sea_bounds_ready", return_value=False):
                fetchdata.main(["--check"])
            download.assert_not_called()


class TestDownload(unittest.TestCase):
    def testReportsBothDirectories(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(
                fetchdata, "download_sea_bounds", return_value=(root / "sea", root / "bounds")
            ) as download:
                self.assertEqual(fetchdata.main([]), 0)
            download.assert_called_once()

    def testFailureIsAnExitCodeNotATraceback(self):
        with mock.patch.object(fetchdata, "download_sea_bounds", side_effect=RuntimeError("nope")):
            self.assertEqual(fetchdata.main([]), 1)


if __name__ == "__main__":
    unittest.main()
