import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tilesvc import job
from tilesvc.config import Config, Region


def syncResult(regionId="test", sequence=42, changed=True, pbf=Path("/tmp/x.osm.pbf")):
    """A regionsync.SyncResult stand-in with only what the job reads off it."""
    result = mock.Mock()
    result.region = mock.Mock(region_id=regionId)
    result.region.name = regionId
    result.pbf = pbf
    result.sequence = sequence
    result.changed = changed
    result.revision = f"{regionId}@{sequence}"
    return result


class JobCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = Config(data_dir=self.tmp, regions=[Region("russia/north-caucasus-fed-district")])
        self.region = mock.Mock(region_id="test")

        patches = [
            mock.patch("tilesvc.job.pg.ensure_schema"),
            mock.patch("tilesvc.job.pgmeta.prune_regions"),
            mock.patch("tilesvc.job.region_by_id", return_value=self.region),
            mock.patch("tilesvc.job.regionsync.sync_regions", return_value=[syncResult()]),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)


class TestSyncRegions(JobCase):
    def testSyncUsesTheRealSiteByDefault(self):
        with mock.patch("tilesvc.job.region_by_id", return_value=self.region) as regionById:
            job.sync_regions(self.cfg)
        self.assertEqual(
            regionById.call_args.kwargs["base_url"], "https://download.geofabrik.de"
        )

    def testSyncUsesAConfiguredMirror(self):
        mirror = self.tmp / "mirror"
        mirror.mkdir()
        cfg = Config(
            data_dir=self.tmp,
            regions=[Region("russia/north-caucasus-fed-district")],
            geofabrik_mirror=mirror,
        )
        with mock.patch("tilesvc.job.region_by_id", return_value=self.region) as regionById:
            job.sync_regions(cfg)
        self.assertEqual(regionById.call_args.kwargs["base_url"], mirror.resolve().as_uri())

    def testSyncPassesEveryRegionToOneSync(self):
        cfg = Config(
            data_dir=self.tmp,
            regions=[Region("armenia"), Region("russia/north-caucasus-fed-district")],
        )
        with mock.patch(
            "tilesvc.job.regionsync.sync_regions", return_value=[syncResult()]
        ) as sync:
            job.sync_regions(cfg)
        sync.assert_called_once()
        regions, cache = sync.call_args[0]
        self.assertEqual(len(regions), 2)
        self.assertEqual(cache, cfg.geofabrik_cache)

    def testSyncPrunesRegionsNoLongerConfigured(self):
        with mock.patch("tilesvc.job.pgmeta.prune_regions") as prune:
            job.sync_regions(self.cfg)
        prune.assert_called_once_with(["test"])


class TestRunOnce(JobCase):
    def testTheWholePassIsTheSync(self):
        with mock.patch.object(job, "sync_regions", return_value=[]) as sync:
            job.run_once(self.cfg)
        sync.assert_called_once_with(self.cfg)


if __name__ == "__main__":
    unittest.main()
