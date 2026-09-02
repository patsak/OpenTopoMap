import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

from shapely.geometry import box

from otmlib import regionsync


class RegionSyncCase(unittest.TestCase):
    """The replication-sequence state machine: first sync, up to date, behind
    by N diffs, and the two ways a diff range can be unusable."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pbf = self.tmp / "armenia-latest.osm.pbf"
        self.pbf.write_bytes(b"pbf")

        self.region = mock.Mock()
        self.region.region_id = "armenia"
        self.region.name = "Armenia"
        self.region.pbf_url = "https://download.geofabrik.de/asia/armenia-latest.osm.pbf"
        self.region.updates_url = "https://download.geofabrik.de/asia/armenia-updates"
        self.region.geometry = box(43.0, 38.0, 47.0, 42.0)

        self.state: dict[str, int] = {}
        patches = [
            mock.patch.object(regionsync.geofabrik, "download_full_pbf", return_value=self.pbf),
            mock.patch.object(regionsync.pgmeta, "upsert_region"),
            mock.patch.object(
                regionsync.pgmeta, "get_replication_state", side_effect=self.state.get
            ),
            mock.patch.object(
                regionsync.pgmeta,
                "set_replication_state",
                side_effect=lambda region_id, seq: self.state.__setitem__(region_id, seq),
            ),
            mock.patch.object(regionsync.geofabrik, "apply_osc_files"),
            mock.patch.object(regionsync.geofabrik, "retain_last_osc"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def notFound(self):
        return HTTPError("http://x", 404, "Not Found", {}, None)


class TestFirstSync(RegionSyncCase):
    def testTheSequenceIsSeededFromTheDownloadedPbf(self):
        with mock.patch.object(regionsync.geofabrik, "pbf_sequence", return_value=100):
            with mock.patch.object(
                regionsync.geofabrik, "fetch_latest_sequence", return_value=(100, None)
            ):
                result = regionsync.sync_region(self.region, self.tmp)
        self.assertEqual(self.state["armenia"], 100)
        self.assertEqual(result.sequence, 100)
        self.assertFalse(result.changed)

    def testAnExtractWithNoSequenceIsReportedAsUntrackable(self):
        with mock.patch.object(regionsync.geofabrik, "pbf_sequence", return_value=None):
            with mock.patch.object(regionsync.geofabrik, "fetch_latest_sequence") as fetch:
                result = regionsync.sync_region(self.region, self.tmp)
        # Nothing to apply diffs from, so the stream is never consulted and the
        # extract counts as fresh - the caller re-tiles from what it just got.
        fetch.assert_not_called()
        self.assertIsNone(result.sequence)
        self.assertTrue(result.changed)


class TestUpToDate(RegionSyncCase):
    def testNoDiffsAreDownloadedWhenAlreadyCurrent(self):
        self.state["armenia"] = 42
        with mock.patch.object(
            regionsync.geofabrik, "fetch_latest_sequence", return_value=(42, None)
        ) as fetch:
            with mock.patch.object(regionsync.geofabrik, "download_osc_range") as download:
                result = regionsync.sync_region(self.region, self.tmp)
        fetch.assert_called_once_with(self.region.updates_url)
        download.assert_not_called()
        self.assertFalse(result.changed)
        self.assertEqual(result.sequence, 42)

    def testTheRegionRowIsRefreshedEvenWithNothingToApply(self):
        self.state["armenia"] = 42
        with mock.patch.object(
            regionsync.geofabrik, "fetch_latest_sequence", return_value=(42, None)
        ):
            with mock.patch.object(regionsync.pgmeta, "upsert_region") as upsert:
                regionsync.sync_region(self.region, self.tmp)
        upsert.assert_called_once_with("armenia", "Armenia", self.region.geometry.bounds)


class TestBehind(RegionSyncCase):
    def testOnlyTheMissingRangeIsDownloadedAndApplied(self):
        self.state["armenia"] = 42
        osc = [self.tmp / "000000043.osc.gz", self.tmp / "000000044.osc.gz"]
        with mock.patch.object(
            regionsync.geofabrik, "fetch_latest_sequence", return_value=(44, None)
        ):
            with mock.patch.object(
                regionsync.geofabrik, "download_osc_range", return_value=osc
            ) as download:
                with mock.patch.object(regionsync.geofabrik, "apply_osc_files") as apply_osc:
                    result = regionsync.sync_region(self.region, self.tmp)
        updates = regionsync.geofabrik.updates_dir(self.tmp, self.region)
        download.assert_called_once_with(self.region.updates_url, 43, 44, updates, None)
        apply_osc.assert_called_once_with(self.pbf, osc)
        self.assertEqual(self.state["armenia"], 44)
        self.assertTrue(result.changed)

    def testTheSequenceAdvancesOnlyAfterTheApplySucceeds(self):
        self.state["armenia"] = 42
        with mock.patch.object(
            regionsync.geofabrik, "fetch_latest_sequence", return_value=(43, None)
        ):
            with mock.patch.object(
                regionsync.geofabrik, "download_osc_range", return_value=[self.tmp / "a.osc.gz"]
            ):
                with mock.patch.object(
                    regionsync.geofabrik, "apply_osc_files", side_effect=RuntimeError("osmium")
                ):
                    with self.assertRaises(RuntimeError):
                        regionsync.sync_region(self.region, self.tmp)
        self.assertEqual(self.state["armenia"], 42)

    def testFarBehindRedownloadsInsteadOfApplyingEveryDiff(self):
        self.state["armenia"] = 1
        latest = 1 + regionsync.MAX_OSC_CATCHUP + 1
        with mock.patch.object(
            regionsync.geofabrik, "fetch_latest_sequence", return_value=(latest, None)
        ):
            with mock.patch.object(regionsync.geofabrik, "download_osc_range") as download:
                with mock.patch.object(regionsync.geofabrik, "refetch_full_pbf", return_value=self.pbf) as full:
                    with mock.patch.object(
                        regionsync.geofabrik, "pbf_sequence", return_value=latest
                    ):
                        result = regionsync.sync_region(self.region, self.tmp)
        download.assert_not_called()
        full.assert_called_once()
        self.assertEqual(self.state["armenia"], latest)
        self.assertTrue(result.changed)


class TestUnusableDiffRange(RegionSyncCase):
    def testARotatedAwayDiffFallsBackToAFullDownload(self):
        self.state["armenia"] = 42
        with mock.patch.object(
            regionsync.geofabrik, "fetch_latest_sequence", return_value=(43, None)
        ):
            with mock.patch.object(
                regionsync.geofabrik, "download_osc_range", side_effect=self.notFound()
            ):
                with mock.patch.object(regionsync.geofabrik, "refetch_full_pbf", return_value=self.pbf) as full:
                    with mock.patch.object(regionsync.geofabrik, "pbf_sequence", return_value=43):
                        result = regionsync.sync_region(self.region, self.tmp)
        full.assert_called_once()
        self.assertEqual(self.state["armenia"], 43)
        self.assertTrue(result.changed)

    def testARealDownloadErrorIsNotSwallowed(self):
        self.state["armenia"] = 42
        with mock.patch.object(
            regionsync.geofabrik, "fetch_latest_sequence", return_value=(43, None)
        ):
            with mock.patch.object(
                regionsync.geofabrik,
                "download_osc_range",
                side_effect=HTTPError("http://x", 500, "Boom", {}, None),
            ):
                with mock.patch.object(regionsync.geofabrik, "refetch_full_pbf") as full:
                    with self.assertRaises(HTTPError):
                        regionsync.sync_region(self.region, self.tmp)
        full.assert_not_called()

    def testAnExtractWithoutAReplicationStreamKeepsWhatItHas(self):
        self.state["armenia"] = 42
        with mock.patch.object(
            regionsync.geofabrik, "fetch_latest_sequence", side_effect=self.notFound()
        ):
            result = regionsync.sync_region(self.region, self.tmp)
        self.assertFalse(result.changed)
        self.assertEqual(result.sequence, 42)


class TestRevision(RegionSyncCase):
    def testTheTilesetRevisionIsOrderIndependent(self):
        a = regionsync.SyncResult(region=self.region, pbf=self.pbf, sequence=1, changed=False)
        other = mock.Mock(region_id="georgia")
        b = regionsync.SyncResult(region=other, pbf=self.pbf, sequence=2, changed=False)
        self.assertEqual(
            regionsync.tileset_revision([a, b]), regionsync.tileset_revision([b, a])
        )

    def testAChangedSequenceChangesTheRevision(self):
        a = regionsync.SyncResult(region=self.region, pbf=self.pbf, sequence=1, changed=False)
        b = regionsync.SyncResult(region=self.region, pbf=self.pbf, sequence=2, changed=True)
        self.assertNotEqual(a.revision, b.revision)


class CoverageCase(unittest.TestCase):
    """Which bboxes a preview may be offered for: the configured regions only."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.regions = []
        patches = [
            mock.patch.object(regionsync.geofabrik, "load_regions", side_effect=lambda **kw: self.regions),
            mock.patch.object(regionsync.pgmeta, "list_regions", side_effect=lambda: self.configured),
        ]
        self.configured = []
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def addRegion(self, region_id, name, geometry, configured=True):
        region = mock.Mock()
        region.region_id = region_id
        region.name = name
        region.geometry = geometry
        self.regions.append(region)
        if configured:
            self.configured.append((region_id, name))
        return region


class TestConfiguredRegions(CoverageCase):
    def testOnlyTheConfiguredOnesComeBack(self):
        self.addRegion("armenia", "Armenia", box(43.0, 38.0, 47.0, 42.0))
        self.addRegion("georgia", "Georgia", box(40.0, 41.0, 47.0, 44.0), configured=False)
        got = regionsync.configured_regions(self.tmp)
        self.assertEqual([r.region_id for r in got], ["armenia"])

    def testNothingConfiguredIsNoRegions(self):
        self.addRegion("armenia", "Armenia", box(43.0, 38.0, 47.0, 42.0), configured=False)
        self.assertEqual(regionsync.configured_regions(self.tmp), [])


class TestCoverageGap(CoverageCase):
    def testABboxInsideTheRegionIsCovered(self):
        self.addRegion("armenia", "Armenia", box(43.0, 38.0, 47.0, 42.0))
        self.assertEqual(regionsync.bbox_coverage_gap(44.0, 39.0, 45.0, 40.0, self.tmp), "")

    def testABboxOutsideIsRejectedWithTheRegionNames(self):
        self.addRegion("armenia", "Armenia", box(43.0, 38.0, 47.0, 42.0))
        gap = regionsync.bbox_coverage_gap(10.0, 50.0, 11.0, 51.0, self.tmp)
        self.assertIn("Armenia", gap)

    def testHalfOutsideIsStillRejected(self):
        # A partly covered bbox would build a preview with a blank strip, which
        # looks like missing data rather than an area the service does not have.
        self.addRegion("armenia", "Armenia", box(43.0, 38.0, 47.0, 42.0))
        self.assertNotEqual(regionsync.bbox_coverage_gap(46.0, 41.0, 48.0, 43.0, self.tmp), "")

    def testTwoRegionsCoverABboxOnTheirBorder(self):
        self.addRegion("west", "West", box(40.0, 38.0, 44.0, 42.0))
        self.addRegion("east", "East", box(44.0, 38.0, 48.0, 42.0))
        self.assertEqual(regionsync.bbox_coverage_gap(43.0, 39.0, 45.0, 40.0, self.tmp), "")

    def testNoConfiguredRegionsSaysSo(self):
        gap = regionsync.bbox_coverage_gap(44.0, 39.0, 45.0, 40.0, self.tmp)
        self.assertIn("tilesvc-job", gap)


if __name__ == "__main__":
    unittest.main()
