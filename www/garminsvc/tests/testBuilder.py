import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from otmlib.bbox import BBox

from garminsvc.builder import MapBuilder


def region(regionId, name, pbfUrl):
    stub = mock.Mock()
    stub.region_id = regionId
    stub.name = name
    stub.pbf_url = pbfUrl
    return stub


def syncResult(regionStub, pbf):
    result = mock.Mock()
    result.region = regionStub
    result.pbf = pbf
    return result


class BuilderCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.builder = MapBuilder(self.tmp / "job")
        self.bbox = BBox(43.0, 42.0, 44.0, 43.0)
        self.georgia = region("georgia", "Georgia", "https://x/asia/georgia-latest.osm.pbf")
        self.armenia = region("armenia", "Armenia", "https://x/asia/armenia-latest.osm.pbf")


class TestPrepareGeofabrik(BuilderCase):
    def testDownloadsTheLeafRegionsCoveringTheBbox(self):
        pbf = self.tmp / "georgia.osm.pbf"
        with mock.patch(
            "garminsvc.builder.find_leaf_regions", return_value=[self.georgia]
        ) as find:
            with mock.patch(
                "garminsvc.builder.sync_regions", return_value=[syncResult(self.georgia, pbf)]
            ) as sync:
                self.builder._prepare_geofabrik(self.bbox)
        self.assertEqual(find.call_args[0], (43.0, 42.0, 44.0, 43.0))
        sync.assert_called_once()
        self.assertEqual(self.builder._geofabrik_pbfs, [pbf])

    def testRecordsEveryRegionUrlForTheJobRecord(self):
        pbfs = [self.tmp / "georgia.osm.pbf", self.tmp / "armenia.osm.pbf"]
        results = [
            syncResult(self.georgia, pbfs[0]),
            syncResult(self.armenia, pbfs[1]),
        ]
        with mock.patch(
            "garminsvc.builder.find_leaf_regions", return_value=[self.georgia, self.armenia]
        ):
            with mock.patch("garminsvc.builder.sync_regions", return_value=results):
                self.builder._prepare_geofabrik(self.bbox)
        self.assertEqual(
            self.builder._geofabrik_urls, [self.georgia.pbf_url, self.armenia.pbf_url]
        )
        self.assertEqual(self.builder._geofabrik_pbfs, pbfs)

    def testTheSecondBboxOfAJobReusesTheFirstDownload(self):
        pbf = self.tmp / "georgia.osm.pbf"
        with mock.patch(
            "garminsvc.builder.find_leaf_regions", return_value=[self.georgia]
        ) as find:
            with mock.patch(
                "garminsvc.builder.sync_regions", return_value=[syncResult(self.georgia, pbf)]
            ):
                self.builder._prepare_geofabrik(self.bbox)
                self.builder._prepare_geofabrik(self.bbox)
        find.assert_called_once()

    def testTheSharedCacheIsUsed(self):
        from garminsvc.constants import GEOFABRIK_CACHE

        pbf = self.tmp / "georgia.osm.pbf"
        with mock.patch("garminsvc.builder.find_leaf_regions", return_value=[self.georgia]) as find:
            with mock.patch(
                "garminsvc.builder.sync_regions", return_value=[syncResult(self.georgia, pbf)]
            ) as sync:
                self.builder._prepare_geofabrik(self.bbox)
        self.assertEqual(find.call_args.kwargs["cache_dir"], GEOFABRIK_CACHE)
        self.assertEqual(sync.call_args[0][1], GEOFABRIK_CACHE)


if __name__ == "__main__":
    unittest.main()
