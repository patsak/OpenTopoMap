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


class TestBuildTiles(JobCase):
    def testSkipsTheBuildWhenTheTilesetIsCurrent(self):
        with mock.patch("tilesvc.job.tilemaker.needs_rebuild", return_value=False):
            with mock.patch("tilesvc.job.tilemaker.build_otm") as build:
                self.assertFalse(job.build_tiles(self.cfg, [syncResult()]))
        build.assert_not_called()

    def testBuildsFromTheMergedRegions(self):
        results = [syncResult("a", 1, pbf=self.tmp / "a.osm.pbf"), syncResult("b", 2, pbf=self.tmp / "b.osm.pbf")]
        merged = self.cfg.tiles_input / job.MERGED_INPUT_NAME
        with mock.patch("tilesvc.job.tilemaker.needs_rebuild", return_value=True):
            with mock.patch("tilesvc.job.merge_pbfs", return_value=merged) as merge:
                with mock.patch("tilesvc.job.tilemaker.build_otm") as build:
                    self.assertTrue(job.build_tiles(self.cfg, results))
        merge.assert_called_once_with([r.pbf for r in results], merged)
        self.assertEqual(build.call_args[0][1], merged)
        self.assertEqual(build.call_args.kwargs["tiles_dir"], self.cfg.vector_tiles)

    def testTheRevisionCoversEveryRegionSoOneChangeRetiles(self):
        results = [syncResult("a", 1), syncResult("b", 2)]
        with mock.patch("tilesvc.job.tilemaker.needs_rebuild", return_value=True) as needs:
            with mock.patch("tilesvc.job.merge_pbfs"):
                with mock.patch("tilesvc.job.tilemaker.build_otm"):
                    job.build_tiles(self.cfg, results)
        revision = needs.call_args[0][1]
        self.assertIn("a@1", revision)
        self.assertIn("b@2", revision)

    def testForceRebuildsEvenWhenCurrent(self):
        with mock.patch("tilesvc.job.tilemaker.needs_rebuild", return_value=False) as needs:
            with mock.patch("tilesvc.job.merge_pbfs"):
                with mock.patch("tilesvc.job.tilemaker.build_otm") as build:
                    self.assertTrue(job.build_tiles(self.cfg, [syncResult()], force=True))
        needs.assert_not_called()
        build.assert_called_once()

    def testNoRegionsMeansNoBuild(self):
        with mock.patch("tilesvc.job.tilemaker.build_otm") as build:
            self.assertFalse(job.build_tiles(self.cfg, []))
        build.assert_not_called()


class TestRunOnce(JobCase):
    def testRunOnceSyncsThenBuildsBothTilesets(self):
        with mock.patch.object(job, "build_tiles", return_value=True) as build:
            with mock.patch.object(job, "build_ocean") as ocean:
                job.run_once(self.cfg)
        build.assert_called_once()
        self.assertFalse(build.call_args.kwargs["force"])
        ocean.assert_called_once_with(self.cfg, force=False)

    def testRecreateForcesBothBuilds(self):
        with mock.patch.object(job, "build_tiles", return_value=True) as build:
            with mock.patch.object(job, "build_ocean") as ocean:
                job.run_once(self.cfg, recreate=True)
        self.assertTrue(build.call_args.kwargs["force"])
        ocean.assert_called_once_with(self.cfg, force=True)


if __name__ == "__main__":
    unittest.main()
