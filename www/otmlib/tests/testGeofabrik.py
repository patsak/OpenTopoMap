import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import URLError

from shapely.geometry import box, mapping

from otmlib import geofabrik


def _region() -> geofabrik.Region:
    return geofabrik.Region(
        region_id="northwestern-fed-district",
        name="Northwestern Federal District",
        parent="russia",
        pbf_url="https://download.geofabrik.de/russia/northwestern-fed-district-latest.osm.pbf",
        updates_url="https://download.geofabrik.de/russia/northwestern-fed-district-updates",
        geometry=box(20.0, 55.0, 70.0, 82.0),
    )


class TestRegionById(unittest.TestCase):
    def setUp(self):
        self.region = _region()
        patch = mock.patch.object(geofabrik, "load_regions", return_value=[self.region])
        patch.start()
        self.addCleanup(patch.stop)

    def testAcceptsTheIndexId(self):
        self.assertIs(geofabrik.region_by_id("northwestern-fed-district"), self.region)

    def testAcceptsTheDownloadPath(self):
        """config.yaml writes russia/…, the form the download URL uses."""
        self.assertIs(geofabrik.region_by_id("russia/northwestern-fed-district"), self.region)

    def testUnknownIdIsAKeyError(self):
        with self.assertRaises(KeyError):
            geofabrik.region_by_id("russia/north-caucasus-fed-district")


class TestRebaseUrl(unittest.TestCase):
    def testSwapsTheGeofabrikHost(self):
        rebased = geofabrik._rebase_url(
            "https://download.geofabrik.de/russia/northwestern-fed-district-latest.osm.pbf",
            "file:///mirror",
        )
        self.assertEqual(rebased, "file:///mirror/russia/northwestern-fed-district-latest.osm.pbf")

    def testLeavesTheRealSiteUntouched(self):
        url = "https://download.geofabrik.de/russia/northwestern-fed-district-latest.osm.pbf"
        self.assertEqual(geofabrik._rebase_url(url, geofabrik.GEOFABRIK_BASE_URL), url)

    def testLeavesAnUnrelatedUrlUntouched(self):
        url = "https://example.com/something.osm.pbf"
        self.assertEqual(geofabrik._rebase_url(url, "file:///mirror"), url)


class TestIsNotFoundError(unittest.TestCase):
    def testHttp404(self):
        from urllib.error import HTTPError

        exc = HTTPError("http://x", 404, "Not Found", {}, None)
        self.assertTrue(geofabrik.is_not_found_error(exc))

    def testHttpOtherStatus(self):
        from urllib.error import HTTPError

        exc = HTTPError("http://x", 500, "Server Error", {}, None)
        self.assertFalse(geofabrik.is_not_found_error(exc))

    def testMissingLocalFile(self):
        exc = URLError(FileNotFoundError(2, "No such file or directory"))
        self.assertTrue(geofabrik.is_not_found_error(exc))

    def testOtherUrlError(self):
        exc = URLError(ConnectionRefusedError())
        self.assertFalse(geofabrik.is_not_found_error(exc))


class LocalMirrorCase(unittest.TestCase):
    """A local mirror laid out with Geofabrik's own relative paths should work
    end-to-end through the real file:// URL handling urllib gives us, not just
    in the abstract - this exercises actual disk I/O, no mocking.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.mirror = self.tmp / "mirror"
        (self.mirror / "russia").mkdir(parents=True)

        index = {
            "features": [
                {
                    "properties": {
                        "id": "northwestern-fed-district",
                        "name": "Northwestern Federal District",
                        "parent": "russia",
                        "urls": {
                            "pbf": (
                                "https://download.geofabrik.de/russia/"
                                "northwestern-fed-district-latest.osm.pbf"
                            ),
                            "updates": (
                                "https://download.geofabrik.de/russia/"
                                "northwestern-fed-district-updates"
                            ),
                        },
                    },
                    "geometry": mapping(box(20.0, 55.0, 70.0, 82.0)),
                }
            ]
        }
        (self.mirror / "index-v1.json").write_text(json.dumps(index), encoding="utf-8")
        (self.mirror / "russia" / "northwestern-fed-district-latest.osm.pbf").write_bytes(
            b"not-really-a-pbf"
        )

        updates_dir = self.mirror / "russia" / "northwestern-fed-district-updates"
        updates_dir.mkdir()
        (updates_dir / "state.txt").write_text(
            "sequenceNumber=42\ntimestamp=2024-01-02T03\\:04\\:05Z\n", encoding="utf-8"
        )
        # _seq_relpath(42) == "000/000/042"
        osc_dir = updates_dir / "000" / "000"
        osc_dir.mkdir(parents=True)
        (osc_dir / "042.osc.gz").write_bytes(b"osc-bytes")

        self.base_url = geofabrik.mirror_base_url(self.mirror)

    def _region(self) -> geofabrik.Region:
        return geofabrik.region_by_id("northwestern-fed-district", base_url=self.base_url)

    def testMirrorBaseUrlIsAFileUri(self):
        self.assertEqual(self.base_url, self.mirror.resolve().as_uri())

    def testRegionByIdReadsTheLocalIndex(self):
        region = self._region()
        self.assertTrue(region.pbf_url.startswith("file://"))
        self.assertTrue(
            region.pbf_url.endswith("russia/northwestern-fed-district-latest.osm.pbf")
        )
        self.assertTrue(region.updates_url.startswith("file://"))

    def testDownloadFullPbfReadsFromTheMirror(self):
        pbf = geofabrik.download_full_pbf(self._region(), self.tmp / "dl")
        self.assertEqual(pbf.read_bytes(), b"not-really-a-pbf")

    def testSecondCallReusesTheCacheEvenIfTheMirrorPublishedANewerFile(self):
        dest_dir = self.tmp / "dl"
        first = geofabrik.download_full_pbf(self._region(), dest_dir)
        self.assertEqual(first.read_bytes(), b"not-really-a-pbf")

        (self.mirror / "russia" / "northwestern-fed-district-latest.osm.pbf").write_bytes(
            b"a-newer-pbf"
        )

        second = geofabrik.download_full_pbf(self._region(), dest_dir)
        self.assertEqual(second, first)
        self.assertEqual(second.read_bytes(), b"not-really-a-pbf")

    def testFetchLatestSequenceReadsStateTxt(self):
        seq, ts = geofabrik.fetch_latest_sequence(self._region().updates_url)
        self.assertEqual(seq, 42)
        self.assertIsNotNone(ts)

    def testDownloadOscRangeReadsNumberedFiles(self):
        files = geofabrik.download_osc_range(
            self._region().updates_url, 42, 42, self.tmp / "osc"
        )
        self.assertEqual([f.read_bytes() for f in files], [b"osc-bytes"])

    def testMissingOscRaisesAFileNotFoundUrlError(self):
        with self.assertRaises(URLError) as ctx:
            geofabrik.download_osc_range(self._region().updates_url, 99, 99, self.tmp / "osc")
        self.assertTrue(geofabrik.is_not_found_error(ctx.exception))


if __name__ == "__main__":
    unittest.main()
