import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.vectoraide import makeMbtiles, makeStyleDir
from garminsvc import vectorbasemap


class VectorBasemapCase(unittest.TestCase):
    """Redirects the module to a temporary data layout for the duration of a test."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.tiles_dir = self.tmp / "vector-tiles"
        self.tiles_dir.mkdir()
        self.style = makeStyleDir(self.tmp / "maplibregljs")
        patches = [
            mock.patch.object(vectorbasemap, "VECTOR_TILES_DIR", self.tiles_dir),
            mock.patch.object(vectorbasemap, "VECTOR_STYLE_DIRS", (self.style,)),
            mock.patch.dict(
                os.environ,
                {
                    "OTM_VECTOR_TILES_URL": "",
                    "OTM_VECTOR_CONTOURS_URL": "",
                    "OTM_MARTIN_PUBLIC_URL": "http://tiles.example:3000",
                },
            ),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)


class TestConfig(VectorBasemapCase):
    def testUnavailableWithoutStyleFiles(self):
        with mock.patch.object(vectorbasemap, "VECTOR_STYLE_DIRS", (self.tmp / "nowhere",)):
            self.assertEqual(vectorbasemap.config()["available"], False)

    def testUnavailableWithoutTiles(self):
        self.assertEqual(vectorbasemap.config()["available"], False)

    def testPointsAtMartinForLocalMbtiles(self):
        makeMbtiles(self.tiles_dir / "otm.mbtiles", [])
        config = vectorbasemap.config()
        self.assertTrue(config["available"])
        self.assertEqual(config["tiles"], "http://tiles.example:3000/otm/{z}/{x}/{y}")
        self.assertIsNone(config["contours"])
        self.assertEqual(config["sprite"], "/vector/assets/otm_sprite")

    def testRemoteUrlWinsOverLocalTileset(self):
        makeMbtiles(self.tiles_dir / "otm.mbtiles", [])
        with mock.patch.dict(os.environ, {"OTM_VECTOR_TILES_URL": "https://cdn/{z}/{x}/{y}.pbf"}):
            config = vectorbasemap.config()
        self.assertEqual(config["tiles"], "https://cdn/{z}/{x}/{y}.pbf")

    def testPicksUpLocalContourTilesetViaMartin(self):
        makeMbtiles(self.tiles_dir / "otm.mbtiles", [])
        makeMbtiles(self.tiles_dir / "otm-contours.mbtiles", [])
        contours = vectorbasemap.config()["contours"]
        self.assertEqual(contours["tiles"], "http://tiles.example:3000/otm-contours/{z}/{x}/{y}")


if __name__ == "__main__":
    unittest.main()
