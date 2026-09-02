import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.vectoraide import makeStyleDir
from garminsvc import vectorbasemap


class VectorBasemapCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.style = makeStyleDir(self.tmp / "maplibregljs")
        patches = [
            mock.patch.object(vectorbasemap, "VECTOR_STYLE_DIRS", (self.style,)),
            mock.patch.dict(
                os.environ,
                {
                    "OTM_DEM_URL": "",
                    "OTM_PREVIEW_PUBLIC_URL": "http://previews.example:8081",
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

    def testServesTheStyleAssetsAndNoTileset(self):
        config = vectorbasemap.config()
        self.assertTrue(config["available"])
        self.assertEqual(config["sprite"], "/vector/assets/otm_sprite")
        self.assertEqual(config["style"], "/vector/assets/otm_style.js")
        self.assertEqual(config["layers"], "/vector/assets/otm_layers.json")
        # The whole-region tileset is no longer offered as a basemap: the only
        # OTM-styled map the picker shows is a preview of the drawn bbox.
        self.assertNotIn("tiles", config)
        self.assertNotIn("ocean", config)

    def testMapCenterFromPostgresWhenAvailable(self):
        # Patched at the source module: vectorbasemap imports it lazily inside
        # _map_center, so there is no name on vectorbasemap to patch.
        with mock.patch(
            "otmlib.pgmeta.coverage_center",
            return_value={"center": [69.0, 33.0], "zoom": 7},
        ):
            config = vectorbasemap.config()
        self.assertEqual(config["center"], [69.0, 33.0])
        self.assertEqual(config["zoom"], 7)

    def testAnAbsentDatabaseCostsOnlyTheOpeningView(self):
        with mock.patch("otmlib.pgmeta.coverage_center", side_effect=RuntimeError("no pg")):
            config = vectorbasemap.config()
        self.assertTrue(config["available"])
        self.assertNotIn("center", config)


class TestPreviewUrl(VectorBasemapCase):
    def testBuiltOnThePublishedPreviewBase(self):
        self.assertEqual(
            vectorbasemap.preview_tiles_url("abc123.pmtiles"),
            "http://previews.example:8081/abc123.pmtiles",
        )

    def testFallsBackToTheLocalNginx(self):
        with mock.patch.dict(os.environ, {"OTM_PREVIEW_PUBLIC_URL": ""}):
            url = vectorbasemap.preview_tiles_url("abc123.pmtiles")
        self.assertTrue(url.startswith(vectorbasemap.DEFAULT_PREVIEW_PUBLIC_URL))

    def testATrailingSlashDoesNotDoubleUp(self):
        with mock.patch.dict(os.environ, {"OTM_PREVIEW_PUBLIC_URL": "https://maps.example/p/"}):
            url = vectorbasemap.preview_tiles_url("abc123.pmtiles")
        self.assertEqual(url, "https://maps.example/p/abc123.pmtiles")


class TestDem(VectorBasemapCase):

    def testDemComesFromMapterhorn(self):
        config = vectorbasemap.config()
        self.assertEqual(config["dem"]["tiles"], vectorbasemap.DEFAULT_DEM_URL)
        self.assertEqual(config["dem"]["encoding"], "terrarium")
        self.assertEqual(config["dem"]["tileSize"], vectorbasemap.DEFAULT_DEM_TILESIZE)
        self.assertIn("mapterhorn", config["dem"]["attribution"].lower())

    def testDemUrlOverrideWins(self):
        with mock.patch.dict(os.environ, {"OTM_DEM_URL": "https://dem/{z}/{x}/{y}.png"}):
            config = vectorbasemap.config()
        self.assertEqual(config["dem"]["tiles"], "https://dem/{z}/{x}/{y}.png")


if __name__ == "__main__":
    unittest.main()
