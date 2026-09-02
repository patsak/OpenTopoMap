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
    def testItRunsTheOnePass(self):
        with mock.patch.object(cli.job, "run_once") as runOnce:
            self.assertEqual(cli.main(self.argv), 0)
        runOnce.assert_called_once()

    def testTheConfigPathIsHonoured(self):
        with mock.patch.object(cli.job, "run_once"):
            with mock.patch.object(cli.config, "load", wraps=cli.config.load) as load:
                cli.main(self.argv)
        self.assertEqual(load.call_args.args[0], Path(self.argv[1]))


if __name__ == "__main__":
    unittest.main()
