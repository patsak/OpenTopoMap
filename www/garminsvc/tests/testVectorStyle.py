"""Checks on the shipped MapLibre style that a unit test can make cheaply.

The rendering itself is not testable here, but the wiring between the style and
the tileset that feeds it is: isolines start at the contour floor, the ridge
lines that replace them below that floor are actually drawn, and every label
layer reads the tilemaker layer that carries a name rather than the geometry
layer that does not.
"""

import json
import sys
import unittest
from pathlib import Path

from otmlib import constants

REPO = Path(__file__).resolve().parents[3]
STYLE = REPO / "vector/maplibregljs/otm_layers.json"
TILEMAKER = REPO / "vector/tilemaker"
CONFIG_REGION = TILEMAKER / "tilemaker-config-otm-region.json"
CONFIG_OCEAN = TILEMAKER / "tilemaker-config-otm-ocean.json"
PROCESS_LUA = TILEMAKER / "process-otm.lua"
NATURAL_LINES_MINZOOM = 9  # ridge/arete floor in process-otm.lua
RIDGE_TYPES = ("ridge", "arete")

sys.path.insert(0, str(REPO / "vector/tools"))
import validate_style  # noqa: E402


def styleLayers() -> list[dict]:
    layers = validate_style.load_style(STYLE)
    return layers["layers"] if isinstance(layers, dict) else layers


def configLayer(path: Path, name: str) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["layers"][name]


def ridgeLayers(layers: list[dict]) -> list[dict]:
    return [
        layer
        for layer in layers
        if layer.get("source-layer") == "natural_lines"
        and RIDGE_TYPES[0] in json.dumps(layer.get("filter", []))
    ]


class TestContourZooms(unittest.TestCase):
    def setUp(self):
        self.layers = styleLayers()

    def testNoContourLayerDrawsBelowTheServedFloor(self):
        onContours = [layer for layer in self.layers if layer.get("source") == "contour-source"]
        self.assertTrue(onContours, "style draws nothing from the contour source")
        for layer in onContours:
            self.assertGreaterEqual(
                layer.get("minzoom", 0),
                constants.CONTOUR_MINZOOM,
                f"{layer['id']} would draw isolines below the contour floor",
            )


class TestRidgeLines(unittest.TestCase):
    def setUp(self):
        self.layers = styleLayers()

    def testRidgeLinesAreDrawn(self):
        self.assertTrue(ridgeLayers(self.layers), "no natural=ridge layer in the style")

    def testRidgeLinesCoverTheZoomsBelowTheContours(self):
        floors = [layer.get("minzoom", 0) for layer in ridgeLayers(self.layers)]
        self.assertLess(
            min(floors),
            constants.CONTOUR_MINZOOM,
            "ridges start no earlier than the contours, so low zoom has no relief",
        )

    def testTheTilesetReachesDownToTheRidgeFloor(self):
        floor = min(layer.get("minzoom", 0) for layer in ridgeLayers(self.layers))
        self.assertLessEqual(
            NATURAL_LINES_MINZOOM,
            floor,
            f"natural_lines starts at z{NATURAL_LINES_MINZOOM}, style asks from z{floor}",
        )
        self.assertIn(
            "natural_lines",
            validate_style.tile_layers(CONFIG_REGION),
            "the tilemaker config must emit natural_lines",
        )
        self.assertLessEqual(
            configLayer(CONFIG_REGION, "natural_lines")["minzoom"],
            floor,
            "the natural_lines layer itself is configured above the ridge floor",
        )

    def testRidgesAndAretesReachTheTile(self):
        """Neither type is in the upstream Geofabrik schema; they are a hike: addition."""
        lua = PROCESS_LUA.read_text(encoding="utf-8")
        natural_values = lua.split('local natural_values = Set {', 1)[1].split("}", 1)[0]
        for kind in RIDGE_TYPES:
            self.assertIn(f'"{kind}"', natural_values)


class TestPeakAndSaddleLabels(unittest.TestCase):
    def setUp(self):
        self.layers = {layer["id"]: layer for layer in styleLayers()}
        self.lua = PROCESS_LUA.read_text(encoding="utf-8")

    def testPeaksAndSaddlesComeFromPois(self):
        for layer_id in ("peak-labels", "saddle-labels", "poi-symbols"):
            self.assertEqual(self.layers[layer_id]["source-layer"], "pois")

    def testSaddleTypeMatchesGarminAndSprite(self):
        """Garmin maps natural=saddle and mountain_pass=yes to 0x661a (sprite 'saddle')."""
        self.assertIn('mountain_pass == "yes"', self.lua)
        self.assertIn('type_tag = "saddle"', self.lua)
        self.assertNotIn('type_tag = "mountain_pass"', self.lua)
        # Everything the pass rule matches arrives as type "saddle", so the
        # style must not also filter on a "mountain_pass" type that never comes.
        saddle_filter = json.dumps(self.layers["saddle-labels"]["filter"])
        self.assertIn("saddle", saddle_filter)
        self.assertNotIn("mountain_pass", saddle_filter)

    def testTheSaddleIconIsAddressedByTypeAlone(self):
        icon = json.dumps(self.layers["poi-symbols"]["layout"]["icon-image"])
        self.assertNotIn("mountain_pass", icon)

    def testElevationLabelsRoundANumber(self):
        for layer_id in ("peak-labels", "saddle-labels"):
            field = json.dumps(self.layers[layer_id]["layout"]["text-field"])
            self.assertIn("round", field, layer_id)

    def testTheEleAttributeIsNumericInTheTile(self):
        """round() over a string drops the whole label, so ele must not be text."""
        self.assertIn('AttributeNumeric("ele", ele)', self.lua)


class TestLabelLayers(unittest.TestCase):
    """tilemaker splits labels off into their own layers, carrying "name" only
    where there is one. Reading a name off the geometry layer draws nothing."""

    def setUp(self):
        self.layers = {layer["id"]: layer for layer in styleLayers()}

    def testEachLabelLayerReadsTheLabelTileLayer(self):
        expected = {
            "glacier-labels": "water_polygons_labels",
            "water-polygon-labels": "water_polygons_labels",
            "water-line-labels": "water_lines_labels",
            "street-names": "street_labels",
        }
        for layer_id, source_layer in expected.items():
            self.assertEqual(self.layers[layer_id]["source-layer"], source_layer, layer_id)

    def testNoLabelLayerGuardsOnAnEmptyName(self):
        """A missing attribute reads as null, and null != "" is true - the guard
        that PostGIS's always-present empty string needed now lets everything
        through, so it must be gone rather than merely harmless."""
        for layer_id in ("glacier-labels", "water-polygon-labels", "water-line-labels", "street-names"):
            spec = json.dumps(self.layers[layer_id].get("filter", []))
            self.assertNotIn('["get", "name"], ""', spec, layer_id)

    def testLakeNamesOnlyAtLargeScales(self):
        labels = self.layers["water-polygon-labels"]
        self.assertGreaterEqual(labels.get("minzoom", 0), 13)

    def testGlacierNamesComeEarlierThanLakeNames(self):
        """Glaciers are the primary content: they are named on approach, from the
        tile layer's own floor, while lakes wait for z13."""
        glacier = self.layers["glacier-labels"].get("minzoom", 0)
        lake = self.layers["water-polygon-labels"].get("minzoom", 0)
        self.assertLess(glacier, lake)
        self.assertLessEqual(
            configLayer(CONFIG_REGION, "water_polygons_labels")["minzoom"],
            12,
            "the label layer is configured above the zoom glacier names need",
        )


class TestBooleanAttributes(unittest.TestCase):
    """tilemaker writes intermittent/tunnel/bridge as MVT booleans, present only
    when true - the style's comparisons must be against true, not "yes"."""

    def setUp(self):
        self.layers = {layer["id"]: layer for layer in styleLayers()}

    def testIntermittentIsComparedToABoolean(self):
        for layer_id, expected in (("water-lines", False), ("water-lines-intermittent", True)):
            spec = json.dumps(self.layers[layer_id]["filter"])
            self.assertIn("intermittent", spec, layer_id)
            self.assertIn("true", spec, layer_id)
            self.assertNotIn('"yes"', spec, layer_id)
        self.assertNotEqual(
            json.dumps(self.layers["water-lines"]["filter"]),
            json.dumps(self.layers["water-lines-intermittent"]["filter"]),
        )

    def testTheLuaWritesThoseAttributesAsBooleans(self):
        lua = PROCESS_LUA.read_text(encoding="utf-8")
        for name in ("intermittent", "tunnel", "bridge"):
            self.assertIn(f'AttributeBoolean("{name}", true)', lua)


class TestOceanLayer(unittest.TestCase):
    def testOceanIsNotReadFromTheRegionalOsmTileset(self):
        """The regional config has no ocean layer; a second tileset carries the sea."""
        oceans = [layer for layer in styleLayers() if layer.get("id") == "ocean"]
        self.assertTrue(oceans)
        self.assertEqual(oceans[0]["source"], "opentopomap-ocean")
        self.assertNotIn("ocean", validate_style.tile_layers(CONFIG_REGION))
        self.assertIn("ocean", validate_style.tile_layers(CONFIG_OCEAN))


if __name__ == "__main__":
    unittest.main()
