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


class TestLakeElevationMarks(unittest.TestCase):
    """A Genshtab sheet marks the water surface level at a lake, under the name
    where there is one and on its own where there is not."""

    def setUp(self):
        self.layers = {layer["id"]: layer for layer in styleLayers()}
        self.lua = PROCESS_LUA.read_text(encoding="utf-8")

    def waterPolygonsLua(self) -> str:
        body = self.lua.split("function process_water_polygons", 1)[1]
        return body.split("\nfunction ", 1)[0]

    def testTheLakeLabelPrintsTheElevation(self):
        field = json.dumps(self.layers["water-polygon-labels"]["layout"]["text-field"])
        self.assertIn("ele", field)
        self.assertIn("name", field)

    def testANamelessLakeIsLabelledByItsElevationAlone(self):
        """A "{name}"-style field or a bare concat would draw an empty line for a
        lake that has only an ele, so the field must branch on the name."""
        field = self.layers["water-polygon-labels"]["layout"]["text-field"]
        self.assertIsInstance(field, list)
        self.assertEqual(field[0], "case")

    def testTheLabelCentroidCarriesTheElevation(self):
        lua = self.waterPolygonsLua()
        self.assertIn("LayerAsCentroid(\"water_polygons_labels\")", lua)
        self.assertIn("addEleAttribute()", lua)

    def testAnEleAloneEarnsALabelCentroid(self):
        """Guarding the centroid on the name only would drop the level of every
        nameless tarn before it reaches the tile."""
        lua = self.waterPolygonsLua()
        self.assertNotIn('if Holds("name") then', lua)
        self.assertIn('if Holds("name") or ', lua)


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


class TestRiverLabels(unittest.TestCase):
    """River names are an orientation aid: they come early, are larger than a
    stream's and win label collisions against street names."""

    def setUp(self):
        self.layers = styleLayers()
        self.byId = {layer["id"]: layer for layer in self.layers}
        self.lua = PROCESS_LUA.read_text(encoding="utf-8")

    def testRiverLabelsArePlacedBeforeStreetNames(self):
        """MapLibre places symbols from the top layer down, so later wins."""
        ids = [layer["id"] for layer in self.layers]
        self.assertGreater(ids.index("water-line-labels"), ids.index("street-names"))

    def testRiversAreNamedFromZ10(self):
        body = self.lua.split("function process_water_lines", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("mz_label = math.max(10, zmin_for_length(0.25))", body)
        self.assertLessEqual(configLayer(CONFIG_REGION, "water_lines_labels")["minzoom"], 10)

    def testARiverNameIsLargerThanAStreamName(self):
        size = self.byId["water-line-labels"]["layout"]["text-size"]
        stops = size[3:]
        for value in stops[1::2]:
            self.assertEqual(value[:3], ["match", ["get", "type"], ["river", "canal"]])
            self.assertGreater(value[3], value[4])

    def testAWindingRiverStillGetsAName(self):
        """At the 45° default a braided mountain river drops its label at most zooms."""
        self.assertGreater(self.byId["water-line-labels"]["layout"].get("text-max-angle", 45), 45)

    def testGarminRiverLabelIsNotSmallFont(self):
        typ = (REPO / "garmin/style/typ/opentopomap-hike.txt").read_text(encoding="utf-8")
        river = typ.split("Type=0x1f\n", 1)[1].split("[end]", 1)[0]
        self.assertIn("FontStyle=NormalFont", river)


class TestTrailVisibility(unittest.TestCase):
    """A path with trail_visibility below intermediate is dashed more sparsely,
    on the web map and on the Garmin map alike."""

    FAINT = ["bad", "horrible", "no"]

    def setUp(self):
        self.layers = {layer["id"]: layer for layer in styleLayers()}

    def testFaintTrailsHaveTheirOwnLayer(self):
        plain = self.layers["footpaths"]
        faint = self.layers["footpaths-faint"]
        self.assertIn(["match", ["get", "trail_visibility"], self.FAINT, False, True], plain["filter"])
        self.assertIn(["match", ["get", "trail_visibility"], self.FAINT, True, False], faint["filter"])
        plainDash = plain["paint"]["line-dasharray"]
        faintDash = faint["paint"]["line-dasharray"]
        self.assertGreater(faintDash[1] / faintDash[0], plainDash[1] / plainDash[0])

    def testTheLuaWritesTrailVisibility(self):
        lua = PROCESS_LUA.read_text(encoding="utf-8")
        self.assertIn('Attribute("trail_visibility", trail_visibility)', lua)
        for value in self.FAINT:
            self.assertIn(f'"{value}"', lua.split("trail_visibility_values = Set", 1)[1].split("\n", 1)[0])

    def testGarminDrawsFaintTrailsWithTheirOwnType(self):
        lines = (REPO / "garmin/style/opentopomap-hike/lines").read_text(encoding="utf-8")
        rule = next(line for line in lines.splitlines() if "trail_visibility" in line and "[0x" in line)
        self.assertIn("[0x0e road_class=0", rule)
        self.assertLess(lines.index(rule), lines.index("highway=footway|highway=path [0x16"))
        typ = (REPO / "garmin/style/typ/opentopomap-hike.txt").read_text(encoding="utf-8")
        self.assertIn("[_line]\nType=0x0e\n", typ)


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
