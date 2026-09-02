import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import numpy as np
from osgeo import gdal

from otmlib import dem
from otmlib.tests import demaide

# Elbrus and its degree cell.
LAT, LON = 43, 42


class DemCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cache = self.tmp / "hgt"


class TestCache(DemCase):
    def testReportsTheDegreesItWouldHaveToFetch(self):
        demaide.makeHgtTile(self.cache, LAT, LON, demaide.ramp(1000.0, 2000.0))
        self.assertEqual(dem.missing_hgt_tiles(42.2, 43.2, 42.8, 43.8, self.cache), [])
        self.assertEqual(dem.missing_hgt_tiles(6.2, 45.2, 6.8, 45.8, self.cache), [(45, 6)])

    def testAPartialTileCountsAsMissing(self):
        self.cache.mkdir(parents=True)
        (self.cache / f"{dem.tile_name(LAT, LON)}.hgt").write_bytes(b"\x00" * 128)
        self.assertEqual(dem.missing_hgt_tiles(42.2, 43.2, 42.8, 43.8, self.cache), [(LAT, LON)])


class TestConcurrentFetch(DemCase):
    def testParallelCallersFetchADegreeOnce(self):
        calls: list[str] = []

        def fake_fetch(lat, lon, dest, **kwargs):
            calls.append(dest.name)
            demaide.makeHgtTile(dest.parent, lat, lon, demaide.ramp(0.0, 100.0))
            return dest

        with mock.patch.object(dem, "_fetch_full_hgt_tile", side_effect=fake_fetch):
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(
                    pool.map(
                        lambda _: dem.ensure_hgt_tiles(42.2, 43.2, 42.8, 43.8, self.cache),
                        range(4),
                    )
                )

        self.assertEqual(len(calls), 1)
        for tiles in results:
            self.assertTrue(all(path.is_file() for path in tiles))


class TestHgtWriting(DemCase):
    def testWritesTheBigEndianLayoutTheReaderExpects(self):
        source = demaide.makeHgtTile(self.tmp / "src", LAT, LON, demaide.ramp(500.0, 1500.0))
        dataset = gdal.Open(str(source))
        out = self.cache / "written.hgt"

        dem._write_hgt_tile(dataset, float(LON), float(LAT), float(LON + 1), float(LAT + 1), out)

        self.assertEqual(out.stat().st_size, dem.HGT_BYTES)
        written = np.frombuffer(out.read_bytes(), dtype=">i2").reshape(dem.HGT_SIDE, dem.HGT_SIDE)
        expected = np.frombuffer(source.read_bytes(), dtype=">i2").reshape(
            dem.HGT_SIDE, dem.HGT_SIDE
        )
        # Same grid, warped onto itself: bilinear resampling keeps it within a metre.
        self.assertLess(np.abs(written.astype(int) - expected.astype(int)).max(), 2)


if __name__ == "__main__":
    unittest.main()
