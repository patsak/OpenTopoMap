"""The tilemaker command line, assembled once for whoever renders tiles.

The binary is never run here — what is worth pinning down is the shape of the
command, because every one of these flags has cost someone a broken build:
--store and --shard-stores (a region does not fit in RAM without them), the
thread count, and the output extension, which is what tilemaker reads to decide
between one file and a directory of loose tiles.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from otmlib import tilemaker


class TilemakerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = self.tmp / "config.json"
        self.config.write_text("{}", encoding="utf-8")
        self.process = self.tmp / "process-otm.lua"
        self.process.write_text("-- lua", encoding="utf-8")
        self.input = self.tmp / "in.osm.pbf"
        self.input.write_bytes(b"pbf")
        self.store = self.tmp / "store"
        self.output = self.tmp / "out.pmtiles"

        patch = mock.patch.object(
            tilemaker, "tilemaker_bin", return_value="/usr/local/bin/tilemaker"
        )
        patch.start()
        self.addCleanup(patch.stop)

    def build(self, **kwargs):
        """Run build() with proc.run captured, and return the argv it produced."""
        seen = {}

        def fakeRun(cmd, cwd=None):
            seen["cmd"] = cmd
            seen["cwd"] = cwd

        with mock.patch.object(tilemaker, "run", side_effect=fakeRun):
            tilemaker.build(
                output=kwargs.pop("output", self.output),
                config=self.config,
                process=self.process,
                store_dir=self.store,
                **kwargs,
            )
        self.cmd = seen["cmd"]
        self.cwd = seen["cwd"]
        return self.cmd

    def valueOf(self, flag):
        return self.cmd[self.cmd.index(flag) + 1]


class TestCommand(TilemakerCase):
    def testTheBinaryComesFirst(self):
        self.build(input_pbf=self.input)
        self.assertEqual(self.cmd[0], "/usr/local/bin/tilemaker")

    def testOutputConfigAndProcessArePassed(self):
        self.build(input_pbf=self.input)
        self.assertEqual(self.valueOf("--output"), str(self.output))
        self.assertEqual(self.valueOf("--config"), str(self.config))
        self.assertEqual(self.valueOf("--process"), str(self.process))

    def testTheInputIsPassedWhenGiven(self):
        self.build(input_pbf=self.input)
        self.assertEqual(self.valueOf("--input"), str(self.input))

    def testABboxOnlyRunNeedsNoInput(self):
        self.build(bbox=(1.0, 2.0, 3.0, 4.0))
        self.assertNotIn("--input", self.cmd)
        self.assertEqual(self.valueOf("--bbox"), "1.0,2.0,3.0,4.0")

    def testInputAndBboxCanBeGivenTogether(self):
        # The bbox is not a clip: it is the extent tilemaker stamps into the
        # output header, which a preview needs so MapLibre knows its coverage.
        self.build(input_pbf=self.input, bbox=(1.0, 2.0, 3.0, 4.0))
        self.assertIn("--input", self.cmd)
        self.assertIn("--bbox", self.cmd)

    def testNeitherInputNorBboxIsRejected(self):
        with self.assertRaises(ValueError):
            self.build()

    def testTheCwdIsPassedThrough(self):
        self.build(input_pbf=self.input, cwd=self.tmp)
        self.assertEqual(self.cwd, self.tmp)


class TestStore(TilemakerCase):
    def testStoreAndShardingAreAlwaysOn(self):
        self.build(input_pbf=self.input)
        self.assertEqual(self.valueOf("--store"), str(self.store))
        self.assertIn("--shard-stores", self.cmd)

    def testTheStoreDirectoryIsCreated(self):
        self.build(input_pbf=self.input)
        self.assertTrue(self.store.is_dir())

    def testTheOutputDirectoryIsCreated(self):
        nested = self.tmp / "deep" / "out.pmtiles"
        self.build(input_pbf=self.input, output=nested)
        self.assertTrue(nested.parent.is_dir())


class TestThreads(TilemakerCase):
    def testTheThreadCountIsOverridable(self):
        with mock.patch.dict(os.environ, {tilemaker.THREADS_ENV: "3"}):
            self.build(input_pbf=self.input)
        self.assertEqual(self.valueOf("--threads"), "3")

    def testWithoutAnOverrideItIsAPositiveNumber(self):
        with mock.patch.dict(os.environ, {tilemaker.THREADS_ENV: ""}):
            self.build(input_pbf=self.input)
        self.assertGreaterEqual(int(self.valueOf("--threads")), 1)


class TestBinaryLookup(unittest.TestCase):
    def testAnExplicitBinaryWins(self):
        with mock.patch.dict(os.environ, {tilemaker.BIN_ENV: "/opt/tilemaker"}):
            self.assertEqual(tilemaker.tilemaker_bin(), "/opt/tilemaker")

    def testOtherwiseItComesFromPath(self):
        with mock.patch.dict(os.environ, {tilemaker.BIN_ENV: ""}):
            with mock.patch.object(tilemaker.shutil, "which", return_value="/usr/bin/tilemaker"):
                self.assertEqual(tilemaker.tilemaker_bin(), "/usr/bin/tilemaker")

    def testAMissingBinarySaysWhereToGetIt(self):
        with mock.patch.dict(os.environ, {tilemaker.BIN_ENV: ""}):
            with mock.patch.object(tilemaker.shutil, "which", return_value=None):
                with self.assertRaises(RuntimeError) as caught:
                    tilemaker.tilemaker_bin()
        self.assertIn("ghcr.io/systemed/tilemaker", str(caught.exception))


class TestStyleDir(unittest.TestCase):
    def testItFindsTheDirectoryHoldingTheLua(self):
        styles = tilemaker.style_dir()
        self.assertTrue((styles / tilemaker.PROCESS_LUA).is_file())
        self.assertTrue((styles / tilemaker.CONFIG_REGION).is_file())

    def testAMissingStyleTreeIsAnError(self):
        with mock.patch.object(tilemaker, "STYLE_DIRS", (Path("/nowhere"),)):
            with self.assertRaises(RuntimeError):
                tilemaker.style_dir()


if __name__ == "__main__":
    unittest.main()
