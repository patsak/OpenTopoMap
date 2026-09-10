"""Installing a pinned distribution, without touching the real download site.

The zips are served from a throwaway HTTP server on localhost so the checksum
path — the reason the manifest exists at all — is exercised for real rather
than stubbed out.
"""

import functools
import http.server
import threading
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from garminsvc import artifacts, fetch

DIST = "faketool-r1"


def makeDist(path: Path, lib: bool = True) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{DIST}/faketool.jar", b"jar")
        if lib:
            zf.writestr(f"{DIST}/lib/fastutil.jar", b"fastutil")
        zf.writestr(f"{DIST}/doc/manual.html", b"doc")
        zf.writestr(f"{DIST}/examples/sample.osm", b"osm")


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


class Serving:
    """Serve a directory over HTTP for the duration of a with-block."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def __enter__(self) -> str:
        handler = functools.partial(QuietHandler, directory=str(self._directory))
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class Fixture:
    """A served distribution zip plus the artifact that pins it."""

    def __init__(self, stack: TemporaryDirectory, lib: bool = True, sha256: str | None = None) -> None:
        root = Path(stack.name)
        self.served = root / "served"
        self.tools = root / "tools"
        self.served.mkdir()
        self.zip = self.served / f"{DIST}.zip"
        makeDist(self.zip, lib=lib)
        self._serving = Serving(self.served)
        base = self._serving.__enter__()
        self.artifact = artifacts.Artifact(
            name="faketool",
            version=DIST,
            sha256=sha256 or fetch.file_sha256(self.zip),
            jar="faketool.jar",
            members=("*.jar", "lib/*.jar"),
            base_url=base,
        )

    def close(self) -> None:
        self._serving.__exit__(None, None, None)


class ArtifactTestCase(unittest.TestCase):
    def fixture(self, **kwargs) -> Fixture:
        stack = TemporaryDirectory()
        self.addCleanup(stack.cleanup)
        fx = Fixture(stack, **kwargs)
        self.addCleanup(fx.close)
        return fx

    def install(self, fx: Fixture) -> Path:
        return artifacts.install(fx.artifact, fx.tools, log=lambda _: None)


class TestInstall(ArtifactTestCase):
    def testInstallsJarAndLibAndNothingElse(self):
        fx = self.fixture()
        jar = self.install(fx)
        self.assertEqual(jar, fx.tools / "faketool" / "faketool.jar")
        self.assertEqual(jar.read_bytes(), b"jar")
        self.assertTrue((fx.tools / "faketool" / "lib" / "fastutil.jar").is_file())
        self.assertFalse((fx.tools / "faketool" / "doc").exists())
        self.assertFalse((fx.tools / "faketool" / "examples").exists())

    def testLeavesNoArchiveBehind(self):
        fx = self.fixture()
        self.install(fx)
        self.assertEqual(sorted(p.name for p in fx.tools.iterdir()), ["faketool"])

    def testStampMakesReinstallANoop(self):
        fx = self.fixture()
        self.install(fx)
        self.assertTrue(artifacts.is_installed(fx.artifact, fx.tools))
        lines: list[str] = []
        artifacts.install(fx.artifact, fx.tools, log=lines.append)
        self.assertIn("already installed", lines[0])

    def testBumpedVersionIsNotConsideredInstalled(self):
        fx = self.fixture()
        self.install(fx)
        bumped = artifacts.Artifact(**{**fx.artifact.__dict__, "version": "faketool-r2"})
        self.assertFalse(artifacts.is_installed(bumped, fx.tools))

    def testChecksumMismatchInstallsNothing(self):
        fx = self.fixture(sha256="00" * 32)
        with self.assertRaises(RuntimeError) as ctx:
            self.install(fx)
        self.assertIn("sha256 mismatch", str(ctx.exception))
        self.assertFalse(artifacts.jar_path(fx.artifact, fx.tools).exists())

    def testDistributionWithoutLibIsRejected(self):
        fx = self.fixture(lib=False)
        with self.assertRaises(RuntimeError) as ctx:
            self.install(fx)
        self.assertIn("lib/*.jar", str(ctx.exception))
        self.assertFalse(artifacts.jar_path(fx.artifact, fx.tools).exists())


class TestManifest(unittest.TestCase):
    def testMissingReportsEveryPinnedArtifact(self):
        with TemporaryDirectory() as tmp:
            absent = artifacts.missing(Path(tmp))
            self.assertEqual(len(absent), len(artifacts.ARTIFACTS))
            self.assertTrue(any(artifacts.MKGMAP.version in line for line in absent))

    def testJarPathIsExactNotSearched(self):
        tools = Path("/nowhere")
        self.assertEqual(artifacts.jar_path(artifacts.SPLITTER, tools), tools / "splitter" / "splitter.jar")

    def testEveryArtifactPinsASha256(self):
        for artifact in artifacts.ARTIFACTS.values():
            self.assertRegex(artifact.sha256, r"^[0-9a-f]{64}$", artifact.name)
            self.assertTrue(artifact.url.startswith("https://"), artifact.name)


if __name__ == "__main__":
    unittest.main()
