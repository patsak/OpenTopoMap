import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from garminsvc import fetch


def makeZip(path: Path, entries: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, payload in entries.items():
            zf.writestr(name, payload)


class TestExtract(unittest.TestCase):
    def testStripsTopLevelDirectory(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            makeZip(root / "a.zip", {"dist-r1/mkgmap.jar": b"jar", "dist-r1/lib/fastutil.jar": b"lib"})
            written = fetch.extract(root / "a.zip", root / "out", strip_top=True)
            self.assertEqual(sorted(written), ["lib/fastutil.jar", "mkgmap.jar"])
            self.assertEqual((root / "out" / "mkgmap.jar").read_bytes(), b"jar")

    def testKeepsOnlyRequestedMembers(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            makeZip(
                root / "a.zip",
                {
                    "dist-r1/mkgmap.jar": b"jar",
                    "dist-r1/lib/fastutil.jar": b"lib",
                    "dist-r1/doc/index.html": b"doc",
                    "dist-r1/examples/sample.osm": b"osm",
                    "dist-r1/finnix.iso.torrent": b"junk",
                },
            )
            fetch.extract(root / "a.zip", root / "out", members=("*.jar", "lib/*.jar"), strip_top=True)
            self.assertTrue((root / "out" / "mkgmap.jar").is_file())
            self.assertTrue((root / "out" / "lib" / "fastutil.jar").is_file())
            self.assertFalse((root / "out" / "doc").exists())
            self.assertFalse((root / "out" / "examples").exists())
            self.assertFalse((root / "out" / "finnix.iso.torrent").exists())

    def testRefusesToEscapeDestination(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            makeZip(root / "a.zip", {"dist/../../escaped.jar": b"x", "dist/ok.jar": b"y"})
            written = fetch.extract(root / "a.zip", root / "out", strip_top=True)
            self.assertEqual(written, ["ok.jar"])
            self.assertFalse((root / "escaped.jar").exists())


class TestFileSha256(unittest.TestCase):
    def testMatchesKnownDigest(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "f"
            path.write_bytes(b"abc")
            self.assertEqual(
                fetch.file_sha256(path),
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            )


if __name__ == "__main__":
    unittest.main()
