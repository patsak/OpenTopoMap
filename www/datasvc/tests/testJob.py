import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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
    """Which of build, update or nothing a pass picks - gol itself faked."""

    def setUp(self):
        super().setUp()
        self.cfg = Config(data_dir=self.tmp, regions=self.cfg.regions, gol=True)
        self.target = self.tmp / "gol" / "regions.gol"
        self.results = [syncResult("a", pbf=self.tmp / "a.osm.pbf"), syncResult("b", pbf=self.tmp / "b.osm.pbf")]
        for r in self.results:
            r.region.updates_url = f"https://example.org/{r.region.region_id}-updates"
        self.at = {"a": 10, "b": 20}
        self.snapshot = self.patch(
            "datasvc.job._extract_snapshot",
            side_effect=lambda cfg, r: (self.at[r.region.region_id], f"{r.pbf.name} fp"),
        )
        self.build = self.patch("datasvc.job.gol.build")
        self.update = self.patch("datasvc.job.gol.update")
        self.download = self.patch(
            "datasvc.job.download_osc_range",
            side_effect=lambda url, first, last, dest: [dest / f"{s}.osc.gz" for s in range(first, last + 1)],
        )

    def patch(self, target, **kw):
        patcher = mock.patch(target, **kw)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def holding(self, a, b, built_days_ago=1):
        """A GOL on disk at sequences *a* and *b*."""
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.target.write_bytes(b"gol")
        built = datetime.now(timezone.utc) - timedelta(days=built_days_ago)
        state = job.gol.State(
            sequences={"a.osm.pbf": a, "b.osm.pbf": b}, built=built.strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        job.gol._write_state(self.target, state)

    def assertBuilt(self):
        self.build.assert_called_once_with(
            [self.tmp / "a.osm.pbf", self.tmp / "b.osm.pbf"],
            self.target,
            sequences={"a.osm.pbf": 10, "b.osm.pbf": 20},
            expected_sources=["a.osm.pbf fp", "b.osm.pbf fp"],
        )

    def testNoGolYetIsBuiltWithItsSequences(self):
        self.assertTrue(job.build_gol(self.cfg, self.results))
        self.assertBuilt()
        self.update.assert_not_called()

    def testAGolAtTheSyncedSequencesIsLeftAlone(self):
        self.holding(10, 20)
        self.assertFalse(job.build_gol(self.cfg, self.results))
        self.build.assert_not_called()
        self.update.assert_not_called()

    def testAGolBehindIsUpdatedWithTheDiffsItMisses(self):
        self.holding(8, 20)
        self.assertTrue(job.build_gol(self.cfg, self.results))
        self.build.assert_not_called()
        # Only a's diffs, 9 and 10, from a's own replication stream.
        url, first, last, _ = self.download.call_args.args
        self.assertEqual((url, first, last), ("https://example.org/a-updates", 9, 10))
        changes = self.update.call_args.args[1]
        self.assertEqual(list(changes), ["a.osm.pbf"])
        seq, files = changes["a.osm.pbf"]
        self.assertEqual((seq, [f.name for f in files]), (10, ["9.osc.gz", "10.osc.gz"]))
        self.assertEqual(self.update.call_args.kwargs["sources"], ["a.osm.pbf fp", "b.osm.pbf fp"])

    def testTheDownloadedDiffsAreRemovedAfterwards(self):
        self.holding(8, 20)
        job.build_gol(self.cfg, self.results)
        self.assertEqual([p.name for p in self.target.parent.iterdir() if p.name.startswith(".gol-diffs")], [])

    def testAFailedUpdateFallsBackToABuild(self):
        self.holding(8, 20)
        self.update.side_effect = RuntimeError("Store locked")
        with self.assertLogs("datasvc.job", "ERROR"):
            self.assertTrue(job.build_gol(self.cfg, self.results))
        self.assertBuilt()

    def testADiffGeofabrikNoLongerHasFallsBackToABuild(self):
        self.holding(8, 20)
        self.download.side_effect = OSError("HTTP Error 404: Not Found")
        with self.assertLogs("datasvc.job", "ERROR"):
            self.assertTrue(job.build_gol(self.cfg, self.results))
        self.update.assert_not_called()
        self.assertBuilt()

    def testRebuiltWhenTheLastBuildIsTooOld(self):
        self.holding(8, 20, built_days_ago=self.cfg.gol_rebuild_days)
        self.assertTrue(job.build_gol(self.cfg, self.results))
        self.update.assert_not_called()
        self.assertBuilt()

    def testAnOldBuildWithNothingNewIsStillLeftAlone(self):
        self.holding(10, 20, built_days_ago=100)
        self.assertFalse(job.build_gol(self.cfg, self.results))
        self.build.assert_not_called()

    def testRebuiltWhenTheRegionsChanged(self):
        self.holding(10, 20)
        extra = syncResult("c", pbf=self.tmp / "c.osm.pbf")
        self.at["c"] = 5
        self.assertTrue(job.build_gol(self.cfg, self.results + [extra]))
        self.update.assert_not_called()
        self.build.assert_called_once()

    def testRebuiltWhenARegionIsFarBehind(self):
        self.holding(10 - job.GOL_MAX_UPDATE_DIFFS - 1, 20)
        self.assertTrue(job.build_gol(self.cfg, self.results))
        self.update.assert_not_called()
        self.assertBuilt()

    def testRebuiltWhenARegionWentBack(self):
        self.holding(11, 20)
        self.assertTrue(job.build_gol(self.cfg, self.results))
        self.update.assert_not_called()
        self.assertBuilt()

    def testAGolFromBeforeSequencesIsRebuiltOnce(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"gol")
        job.gol.state_path(self.target).write_text("timestamp=2026-10-01T00:00:00Z\nsource=a 1 2\n")
        self.assertTrue(job.build_gol(self.cfg, self.results))
        self.assertBuilt()

    def testAnExtractWithoutSequenceFollowsItsFiles(self):
        self.at["b"] = None
        with mock.patch("datasvc.job.gol.is_current", return_value=True):
            self.assertFalse(job.build_gol(self.cfg, self.results))
        with mock.patch("datasvc.job.gol.is_current", return_value=False):
            self.assertTrue(job.build_gol(self.cfg, self.results))
        self.build.assert_called_once_with([self.tmp / "a.osm.pbf", self.tmp / "b.osm.pbf"], self.target)
        self.update.assert_not_called()

    def testNoGolConfiguredSkipsTheStep(self):
        cfg = Config(data_dir=self.tmp, regions=self.cfg.regions)
        self.assertFalse(job.build_gol(cfg, self.results))
        self.build.assert_not_called()
        self.snapshot.assert_not_called()

if __name__ == "__main__":
    unittest.main()
