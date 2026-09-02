import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from otmlib import paths

from tilesvc import config


class ConfigCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, body: str) -> Path:
        path = self.tmp / "config.yaml"
        path.write_text(body, encoding="utf-8")
        return path


class TestLoad(ConfigCase):
    def testReadsRegionIds(self):
        cfg = config.load(
            self.write("regions:\n  - russia/north-caucasus-fed-district\n  - georgia\n")
        )
        self.assertEqual(
            [r.geofabrik_id for r in cfg.regions],
            ["russia/north-caucasus-fed-district", "georgia"],
        )

    def testRejectsAnEmptyRegionList(self):
        with self.assertRaises(ValueError):
            config.load(self.write("regions: []\n"))

    def testExplicitDataDirWins(self):
        cfg = config.load(self.write(f"regions: [georgia]\ndata_dir: {self.tmp}/tiles\n"))
        self.assertEqual(cfg.data_dir, self.tmp / "tiles")

    def testFallsBackToTheSharedDataDirEnv(self):
        with mock.patch.dict(os.environ, {paths.DATA_DIR_ENV: str(self.tmp / "shared")}):
            cfg = config.load(self.write("regions: [georgia]\n"))
        self.assertEqual(cfg.data_dir, self.tmp / "shared")

    def testNoMirrorByDefault(self):
        cfg = config.load(self.write("regions: [georgia]\n"))
        self.assertIsNone(cfg.geofabrik_mirror)

    def testEmptyMirrorIsNoMirror(self):
        cfg = config.load(self.write('regions: [georgia]\ngeofabrik_mirror: ""\n'))
        self.assertIsNone(cfg.geofabrik_mirror)

    def testReadsAConfiguredMirror(self):
        cfg = config.load(
            self.write(f"regions: [georgia]\ngeofabrik_mirror: {self.tmp}/mirror\n")
        )
        self.assertEqual(cfg.geofabrik_mirror, self.tmp / "mirror")


class TestPaths(ConfigCase):
    def setUp(self):
        super().setUp()
        self.cfg = config.load(self.write(f"regions: [georgia]\ndata_dir: {self.tmp}\n"))

    def testSharesTheGeofabrikCacheWithDownloads(self):
        self.assertEqual(self.cfg.geofabrik_cache, paths.geofabrik_cache(self.tmp))

    def testShapefilesUnderDataDir(self):
        self.assertEqual(self.cfg.shapefiles, paths.shapefiles(self.tmp))


class TestGeofabrikBaseUrl(ConfigCase):
    def testDefaultsToTheRealSite(self):
        cfg = config.load(self.write("regions: [georgia]\n"))
        self.assertEqual(cfg.geofabrik_base_url, "https://download.geofabrik.de")

    def testUsesTheMirrorWhenConfigured(self):
        mirror = self.tmp / "mirror"
        mirror.mkdir()
        cfg = config.load(self.write(f"regions: [georgia]\ngeofabrik_mirror: {mirror}\n"))
        self.assertEqual(cfg.geofabrik_base_url, mirror.resolve().as_uri())


if __name__ == "__main__":
    unittest.main()
