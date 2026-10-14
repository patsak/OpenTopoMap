"""Loading the config: defaults, overrides, and the mistakes it refuses."""

import tempfile
import unittest
from pathlib import Path

from routing import config


class TestConfig(unittest.TestCase):
    def raw(self, **extra):
        return {"areas": [{"name": "a", "bbox": [33.0, 67.0, 34.0, 68.0]}], **extra}

    def testDefaultsAndOverrides(self):
        cfg = config.from_dict(self.raw(ford={"max_catchment_km2": 20}), data_dir=Path("/tmp/x"))
        self.assertEqual(cfg.ford.max_catchment_km2, 20.0)
        self.assertEqual(cfg.ford.max_width_m, config.FordParams().max_width_m)
        self.assertEqual(cfg.area_dir("a"), Path("/tmp/x/routing/a"))
        self.assertEqual(cfg.terrain_speed["scrub"], config.DEFAULT_TERRAIN_SPEED["scrub"])

    def testShippedConfigLoads(self):
        cfg = config.load(config.DEFAULT_CONFIG)
        self.assertTrue(cfg.areas)
        # the YAML spells out the defaults, so they must agree with the code
        defaults = config.from_dict({"areas": []}, data_dir=cfg.data_dir)
        self.assertEqual(cfg.model_params(), defaults.model_params())
        self.assertEqual(cfg.pack, defaults.pack)

    def testPackIsNoRebuild(self):
        base = config.from_dict(self.raw(), data_dir=Path("/tmp"))
        heavy = config.from_dict(self.raw(pack={"climb_per_10kg": 0.2}), data_dir=Path("/tmp"))
        self.assertEqual(heavy.pack.climb_per_10kg, 0.2)
        self.assertEqual(base.fingerprint(base.area("a")), heavy.fingerprint(heavy.area("a")))

    def testUnknownKeysAreErrors(self):
        for raw in (self.raw(graf={}), self.raw(ford={"max_catchment": 1}),
                    {"areas": [{"name": "a", "bbox": [0, 0, 1, 1], "colour": "red"}]}):
            with self.assertRaises(ValueError):
                config.from_dict(raw, data_dir=Path("/tmp"))

    def testRejectsBadValues(self):
        for raw in (
            self.raw(terrain_speed={"grass": 1.5}),  # faster than Tobler breaks A*
            self.raw(terrain_speed={"lava": 0.1}),
            self.raw(graph={"hydrology": "magic"}),
            self.raw(graph={"lattice_penalty": 0.5}),
            self.raw(graph={"bank_penalty": 0.9}),
            self.raw(graph={"bank_penalty": 1.3}),
            self.raw(graph={"pass_bypass_radius_m": 100}),
            self.raw(side_slope={"penalty_per_10deg": -1}),
            self.raw(pack={"rough_per_10kg": -0.1}),
            {"areas": [{"name": "a", "bbox": [1, 0, 0, 1]}]},
            {"areas": [{"name": "a", "bbox": [0, 0, 1, 1]}, {"name": "a", "bbox": [0, 0, 1, 1]}]},
        ):
            with self.assertRaises(ValueError, msg=raw):
                config.from_dict(raw, data_dir=Path("/tmp"))

    def testFingerprintFollowsTheModel(self):
        base = config.from_dict(self.raw(), data_dir=Path("/tmp"))
        same = config.from_dict(self.raw(), data_dir=Path("/elsewhere"))
        other = config.from_dict(self.raw(ford={"max_catchment_km2": 10}), data_dir=Path("/tmp"))
        area = base.area("a")
        self.assertEqual(base.fingerprint(area), same.fingerprint(area))
        self.assertNotEqual(base.fingerprint(area), other.fingerprint(area))

    def testRelativePathsFollowTheFile(self):
        tmp = Path(tempfile.mkdtemp())
        path = tmp / "c.yaml"
        path.write_text("areas:\n  - {name: a, bbox: [0, 0, 1, 1], pbf: x.osm.pbf}\ndata_dir: /tmp\n")
        self.assertEqual(config.load(path).area("a").pbf, ((tmp / "x.osm.pbf").resolve(),))


if __name__ == "__main__":
    unittest.main()
