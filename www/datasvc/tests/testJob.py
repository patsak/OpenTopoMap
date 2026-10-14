import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from datasvc import job
from datasvc.config import Config, Region


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
            mock.patch("datasvc.job.pg.ensure_shared_schema"),
            mock.patch("datasvc.job.pgmeta.prune_regions"),
            mock.patch("datasvc.job.region_by_id", return_value=self.region),
            mock.patch("datasvc.job.regionsync.sync_regions", return_value=[syncResult()]),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)


class TestSyncRegions(JobCase):
    def testSyncUsesTheRealSiteByDefault(self):
        with mock.patch("datasvc.job.region_by_id", return_value=self.region) as regionById:
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
        with mock.patch("datasvc.job.region_by_id", return_value=self.region) as regionById:
            job.sync_regions(cfg)
        self.assertEqual(regionById.call_args.kwargs["base_url"], mirror.resolve().as_uri())

    def testSyncPassesEveryRegionToOneSync(self):
        cfg = Config(
            data_dir=self.tmp,
            regions=[Region("armenia"), Region("russia/north-caucasus-fed-district")],
        )
        with mock.patch(
            "datasvc.job.regionsync.sync_regions", return_value=[syncResult()]
        ) as sync:
            job.sync_regions(cfg)
        sync.assert_called_once()
        regions, cache = sync.call_args[0]
        self.assertEqual(len(regions), 2)
        self.assertEqual(cache, cfg.geofabrik_cache)

    def testSyncPrunesRegionsNoLongerConfigured(self):
        with mock.patch("datasvc.job.pgmeta.prune_regions") as prune:
            job.sync_regions(self.cfg)
        prune.assert_called_once_with(["test"])


class TestRunOnce(JobCase):
    def testTheWholePassIsTheSyncThenTheGolThenTheRoutingGraphs(self):
        results = [syncResult()]
        calls = mock.Mock()
        calls.sync.return_value = results
        with (
            mock.patch.object(job, "sync_regions", new=calls.sync),
            mock.patch.object(job, "build_gol", new=calls.gol),
            mock.patch.object(job, "build_routing", new=calls.routing),
        ):
            job.run_once(self.cfg)
        # The GOL is built after the sync, never before, out of its results;
        # the routing graphs once the extracts are final.
        self.assertEqual([c[0] for c in calls.mock_calls], ["sync", "gol", "routing"])
        calls.gol.assert_called_once_with(self.cfg, results)
        calls.routing.assert_called_once_with(self.cfg)


class TestBuildRouting(JobCase):
    def setUp(self):
        super().setUp()
        self.cfg = Config(data_dir=self.tmp, regions=self.cfg.regions, routing=True)

    def run_with(self, *codes, jobs=None):
        env = {"OTM_ROUTING_JOBS": jobs} if jobs else {}
        with (
            mock.patch.dict("os.environ", env),
            mock.patch("datasvc.job.subprocess.run",
                       side_effect=[mock.Mock(returncode=c) for c in codes]) as run,
        ):
            ok = job.build_routing(self.cfg)
        return ok, [c.args[0][1:] for c in run.call_args_list]

    def testBuildsTheTilesThenCompilesTheGraphs(self):
        ok, commands = self.run_with(0, 0, jobs="3")
        self.assertTrue(ok)
        self.assertEqual(commands, [["-m", "routing.build", "--all", "--jobs", "3"],
                                    ["-m", "routing.compile", "--all"]])

    def testCompilesWhatIsWholeEvenWhenATileFailed(self):
        # the rest of the graphs move on; the failed tile is not current, so
        # the next night builds it
        ok, commands = self.run_with(1, 0)
        self.assertFalse(ok)
        self.assertEqual(len(commands), 2)

    def testOffBuildsNothing(self):
        cfg = Config(data_dir=self.tmp, regions=self.cfg.regions)
        with mock.patch("datasvc.job.subprocess.run") as run:
            self.assertFalse(job.build_routing(cfg))
        run.assert_not_called()


class TestBuildGol(JobCase):
    def setUp(self):
        super().setUp()
        self.cfg = Config(data_dir=self.tmp, regions=self.cfg.regions, gol=True)
        self.results = [syncResult("a", pbf=self.tmp / "a.osm.pbf"), syncResult("b", pbf=self.tmp / "b.osm.pbf")]

    def testBuildsFromTheSyncedExtractsInOrder(self):
        with (
            mock.patch("datasvc.job.gol.is_current", return_value=False),
            mock.patch("datasvc.job.gol.build") as build,
        ):
            self.assertTrue(job.build_gol(self.cfg, self.results))
        build.assert_called_once_with([self.tmp / "a.osm.pbf", self.tmp / "b.osm.pbf"], self.tmp / "gol" / "regions.gol")

    def testCurrentGolIsLeftAlone(self):
        with (
            mock.patch("datasvc.job.gol.is_current", return_value=True),
            mock.patch("datasvc.job.gol.build") as build,
        ):
            self.assertFalse(job.build_gol(self.cfg, self.results))
        build.assert_not_called()

    def testDecidedByTheFilesNotByWhetherTheSyncMovedAnything(self):
        # A GOL deleted, or a build that failed last night: nothing changed in
        # the sync, and the GOL is still built.
        quiet = [syncResult("a", changed=False, pbf=self.tmp / "a.osm.pbf")]
        with (
            mock.patch("datasvc.job.gol.is_current", return_value=False),
            mock.patch("datasvc.job.gol.build") as build,
        ):
            self.assertTrue(job.build_gol(self.cfg, quiet))
        build.assert_called_once()

    def testNoGolConfiguredSkipsTheStep(self):
        cfg = Config(data_dir=self.tmp, regions=self.cfg.regions)
        with mock.patch("datasvc.job.gol.build") as build:
            self.assertFalse(job.build_gol(cfg, self.results))
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
