import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tilesvc import tilemaker


class TilemakerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.tiles = self.tmp / "vector-tiles"
        self.store = self.tmp / "store"
        self.config = self.tmp / "config.json"
        self.config.write_text("{}", encoding="utf-8")
        self.input = self.tmp / "in.osm.pbf"
        self.input.write_bytes(b"pbf")

        patches = [
            mock.patch.object(tilemaker.runner, "tilemaker_bin", return_value="/usr/local/bin/tilemaker"),
            mock.patch.object(tilemaker, "style_dir", return_value=self.tmp),
            mock.patch.object(tilemaker.pgmeta, "set_tile_state"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def fakeRun(self, size=1024):
        """Stand in for proc.run, writing the output file tilemaker would."""

        def run(cmd, cwd=None):
            output = Path(cmd[cmd.index("--output") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"x" * size)
            self.cmd = cmd
            self.cwd = cwd

        return run


class TestNeedsRebuild(TilemakerCase):
    def testMissingFileAlwaysNeedsABuild(self):
        with mock.patch.object(tilemaker.pgmeta, "get_tile_state", return_value="rev"):
            self.assertTrue(tilemaker.needs_rebuild("otm", "rev", self.tiles))

    def testEmptyFileAlwaysNeedsABuild(self):
        self.tiles.mkdir(parents=True)
        (self.tiles / "otm.mbtiles").touch()
        with mock.patch.object(tilemaker.pgmeta, "get_tile_state", return_value="rev"):
            self.assertTrue(tilemaker.needs_rebuild("otm", "rev", self.tiles))

    def testMatchingRevisionSkipsTheBuild(self):
        self.tiles.mkdir(parents=True)
        (self.tiles / "otm.mbtiles").write_bytes(b"x")
        with mock.patch.object(tilemaker.pgmeta, "get_tile_state", return_value="rev"):
            self.assertFalse(tilemaker.needs_rebuild("otm", "rev", self.tiles))

    def testADifferentRevisionTriggersARebuild(self):
        self.tiles.mkdir(parents=True)
        (self.tiles / "otm.mbtiles").write_bytes(b"x")
        with mock.patch.object(tilemaker.pgmeta, "get_tile_state", return_value="old"):
            self.assertTrue(tilemaker.needs_rebuild("otm", "new", self.tiles))


class TestBuildTileset(TilemakerCase):
    def testWritesThroughATempFileAndRenames(self):
        with mock.patch.object(tilemaker.runner, "run", side_effect=self.fakeRun()):
            output = tilemaker.build_tileset(
                "otm",
                "rev",
                config=self.config,
                tiles_dir=self.tiles,
                store_dir=self.store,
                input_pbf=self.input,
            )
        self.assertEqual(output, self.tiles / "otm.mbtiles")
        self.assertTrue(output.is_file())
        # The staging file must not survive: Martin serves this directory.
        self.assertEqual(sorted(p.name for p in self.tiles.iterdir()), ["otm.mbtiles"])
        self.assertIn("--output", self.cmd)
        staged = self.cmd[self.cmd.index("--output") + 1]
        self.assertNotEqual(staged, str(output))

    def testStoreAndShardingAreAlwaysOn(self):
        with mock.patch.object(tilemaker.runner, "run", side_effect=self.fakeRun()):
            tilemaker.build_tileset(
                "otm",
                "rev",
                config=self.config,
                tiles_dir=self.tiles,
                store_dir=self.store,
                input_pbf=self.input,
            )
        self.assertIn("--shard-stores", self.cmd)
        self.assertEqual(self.cmd[self.cmd.index("--store") + 1], str(self.store))

    def testRecordsTheRevisionOnlyAfterASuccessfulBuild(self):
        with mock.patch.object(tilemaker.runner, "run", side_effect=self.fakeRun()):
            with mock.patch.object(tilemaker.pgmeta, "set_tile_state") as setState:
                tilemaker.build_tileset(
                    "otm",
                    "rev",
                    config=self.config,
                    tiles_dir=self.tiles,
                    store_dir=self.store,
                    input_pbf=self.input,
                )
        setState.assert_called_once_with("otm", "rev")

    def testAFailedRunLeavesNoTempFileAndNoRevision(self):
        with mock.patch.object(tilemaker.runner, "run", side_effect=RuntimeError("boom")):
            with mock.patch.object(tilemaker.pgmeta, "set_tile_state") as setState:
                with self.assertRaises(RuntimeError):
                    tilemaker.build_tileset(
                        "otm",
                        "rev",
                        config=self.config,
                        tiles_dir=self.tiles,
                        store_dir=self.store,
                        input_pbf=self.input,
                    )
        setState.assert_not_called()
        self.assertEqual(list(self.tiles.glob("*")), [])

    def testAnEmptyOutputIsTreatedAsAFailure(self):
        with mock.patch.object(tilemaker.runner, "run", side_effect=self.fakeRun(size=0)):
            with mock.patch.object(tilemaker.pgmeta, "set_tile_state") as setState:
                with self.assertRaises(RuntimeError):
                    tilemaker.build_tileset(
                        "otm",
                        "rev",
                        config=self.config,
                        tiles_dir=self.tiles,
                        store_dir=self.store,
                        input_pbf=self.input,
                    )
        setState.assert_not_called()
        self.assertFalse((self.tiles / "otm.mbtiles").exists())

    def testABboxOnlyBuildNeedsNoInput(self):
        with mock.patch.object(tilemaker.runner, "run", side_effect=self.fakeRun()):
            tilemaker.build_tileset(
                "otm-ocean",
                "rev",
                config=self.config,
                tiles_dir=self.tiles,
                store_dir=self.store,
                bbox=(-180.0, -85.0, 180.0, 85.0),
            )
        self.assertNotIn("--input", self.cmd)
        self.assertEqual(self.cmd[self.cmd.index("--bbox") + 1], "-180.0,-85.0,180.0,85.0")

    def testNeitherInputNorBboxIsRejected(self):
        with self.assertRaises(ValueError):
            tilemaker.build_tileset(
                "otm",
                "rev",
                config=self.config,
                tiles_dir=self.tiles,
                store_dir=self.store,
            )


class TestStagingName(TilemakerCase):
    def testItBuildsIntoAnMbtilesName(self):
        # tilemaker picks its output format from the extension, so a staging
        # name ending in ".tmp" makes it write a directory of loose tiles; the
        # build then fails with "produced no output" every night.
        with mock.patch.object(tilemaker.runner, "run", side_effect=self.fakeRun()):
            tilemaker.build_tileset(
                "otm",
                "rev",
                config=self.config,
                tiles_dir=self.tiles,
                store_dir=self.store,
                input_pbf=self.input,
            )
        staged = Path(self.cmd[self.cmd.index("--output") + 1])
        self.assertEqual(staged.suffix, ".mbtiles")
        self.assertNotEqual(staged.name, "otm.mbtiles")

    def testTheStagedFileIsNotLeftBehind(self):
        with mock.patch.object(tilemaker.runner, "run", side_effect=self.fakeRun()):
            tilemaker.build_tileset(
                "otm",
                "rev",
                config=self.config,
                tiles_dir=self.tiles,
                store_dir=self.store,
                input_pbf=self.input,
            )
        self.assertEqual(sorted(p.name for p in self.tiles.iterdir()), ["otm.mbtiles"])


class TestThreads(TilemakerCase):
    def testTheThreadCountIsOverridable(self):
        with mock.patch.dict(os.environ, {"OTM_TILEMAKER_THREADS": "3"}):
            with mock.patch.object(tilemaker.runner, "run", side_effect=self.fakeRun()):
                tilemaker.build_tileset(
                    "otm",
                    "rev",
                    config=self.config,
                    tiles_dir=self.tiles,
                    store_dir=self.store,
                    input_pbf=self.input,
                )
        self.assertEqual(self.cmd[self.cmd.index("--threads") + 1], "3")


if __name__ == "__main__":
    unittest.main()
