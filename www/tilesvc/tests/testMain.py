import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tilesvc.__main__ as cli


class MainCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        config = self.tmp / "config.yaml"
        config.write_text(f"regions: [georgia]\ndata_dir: {self.tmp}\n", encoding="utf-8")
        self.argv = ["--config", str(config)]


class TestEntryPoint(MainCase):
    def testRecreateIsPassedToTheJob(self):
        with mock.patch.object(cli.job, "run_once") as runOnce:
            self.assertEqual(cli.main([*self.argv, "--recreate"]), 0)
        runOnce.assert_called_once()
        self.assertTrue(runOnce.call_args.kwargs["recreate"])

    def testSyncOnlyStopsBeforeTheTileBuild(self):
        with mock.patch.object(cli.job, "sync_regions") as sync:
            with mock.patch.object(cli.job, "run_once") as runOnce:
                self.assertEqual(cli.main([*self.argv, "--sync-only"]), 0)
        sync.assert_called_once()
        runOnce.assert_not_called()


class TestFullPass(MainCase):
    def testTheDefaultPassBuildsBothTilesets(self):
        synced = [mock.Mock()]
        with mock.patch.object(cli.job, "sync_regions", return_value=synced) as sync:
            with mock.patch.object(cli.job, "build_tiles", return_value=True) as build:
                with mock.patch.object(cli.job, "build_ocean") as ocean:
                    cli.main(self.argv)
        sync.assert_called_once()
        build.assert_called_once()
        ocean.assert_called_once()


if __name__ == "__main__":
    unittest.main()
