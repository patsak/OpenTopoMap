"""How a rebuild decides that an extract moved."""

import os
import tempfile
import unittest
from pathlib import Path

from routing.build.pipeline import source_state


class TestSourceState(unittest.TestCase):
    def testFollowsTheFileNotTheDownloadSidecar(self):
        tmp = Path(tempfile.mkdtemp())
        pbf = tmp / "region-latest.osm.pbf"
        pbf.write_bytes(b"pbf")
        # Written once at download and left there; diffs do not update it.
        pbf.with_name(pbf.name + ".state.txt").write_text(
            "sequenceNumber=1\ntimestamp=2001-01-01T00\\:00\\:00Z\n", encoding="utf-8")
        first = source_state([pbf])
        os.utime(pbf, (pbf.stat().st_atime, pbf.stat().st_mtime + 10))
        pbf.write_bytes(b"pbf-updated")
        second = source_state([pbf])
        self.assertNotEqual(first, second)
        self.assertNotIn("sequenceNumber", first[0]["state"])
