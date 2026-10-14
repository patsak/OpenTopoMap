"""The HTTP side: request forms, answers, errors, hot reload."""

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pytest

# Collected by the whole-www pytest run too, whose venvs need not have numba.
pytest.importorskip("numba")

from routing.build.osmdata import OsmData
from routing.server import create_app
from routing.tests import terrainaide as ta


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.cfg = ta.config(cls.tmp)
        xs = np.linspace(ta.XC - 1500, ta.XC - 900, 7)
        osm = OsmData(routable=[ta.osm_way(1, 1, xs, np.full(7, ta.YC), highway="path")],
                      passes=[ta.osm_point(9, ta.XC, ta.YC)])
        cls.pkg, _ = ta.build(cls.cfg, ta.two_valleys(), osm)
        ta.write(cls.cfg, cls.pkg)
        lon, lat = ta.lonlat([ta.XC - 1200, ta.XC + 1200], [ta.YC, ta.YC])
        cls.a = [float(lon[0]), float(lat[0])]
        cls.b = [float(lon[1]), float(lat[1])]

    def setUp(self):
        self.client = create_app(self.cfg).test_client()

    def post(self, body, **kwargs):
        return self.client.post("/route/api/v1/route", data=json.dumps(body),
                                content_type="application/json", **kwargs)

    def points(self, *coords, **props):
        return {"type": "FeatureCollection", "properties": props, "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": c}, "properties": {}}
            for c in coords
        ]}


class TestRoute(ServerCase):
    def testFeatureCollectionOfPoints(self):
        r = self.post(self.points(self.a, self.b))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        fc = r.get_json()
        whole = fc["features"][0]
        self.assertEqual(whole["geometry"]["type"], "LineString")
        self.assertEqual(len(whole["geometry"]["coordinates"][0]), 3)  # lon, lat, ele
        props = whole["properties"]
        self.assertGreater(props["distance_m"], 2000)
        self.assertGreater(props["duration_s"], 0)
        self.assertGreater(props["ascent_m"], 0)
        self.assertEqual(len(props["legs"]), 1)
        self.assertEqual(props["area"], "test")
        goings = {f["properties"]["going"] for f in fc["features"][1:]}
        self.assertTrue(goings <= {"trail", "offroad", "ford"})
        self.assertIn("offroad", goings)

    def testLineStringAndMultiPoint(self):
        for kind in ("LineString", "MultiPoint"):
            r = self.post({"type": kind, "coordinates": [self.a, self.b, self.a]})
            self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
            self.assertEqual(len(r.get_json()["features"][0]["properties"]["legs"]), 2)

    def testTrailsOnlyFromTheQueryString(self):
        r = self.post(self.points(self.a, self.b), query_string={"allow_offroad": "false"})
        self.assertEqual(r.status_code, 422)  # the east point is nowhere near a trail

    def testBadRequests(self):
        self.assertEqual(self.post({"type": "Point", "coordinates": self.a}).status_code, 400)
        self.assertEqual(self.post(self.points(self.a)).status_code, 400)
        self.assertEqual(self.post(self.points(self.a, [500, 1])).status_code, 400)
        r = self.client.post("/route/api/v1/route", data="nope", content_type="text/plain")
        self.assertEqual(r.status_code, 400)

    def testPassLimitOptions(self):
        r = self.post(self.points(self.a, self.b, max_pass_grade="2A", max_top_passes=1))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertIn("passes", r.get_json()["features"][0]["properties"])
        self.assertEqual(self.post(self.points(self.a, self.b, max_pass_grade="5Г")).status_code, 400)
        self.assertEqual(self.post(self.points(self.a, self.b, max_top_passes=-1)).status_code, 400)
        self.assertEqual(self.post(self.points(self.a, self.b, max_top_passes="two")).status_code, 400)

    def testOutsideEveryArea(self):
        r = self.post(self.points([10.0, 50.0], [10.1, 50.1]))
        self.assertEqual(r.status_code, 422)
        self.assertIn("error", r.get_json())

    def testCorsPreflight(self):
        r = self.client.options("/route/api/v1/route")
        self.assertEqual(r.status_code, 204)
        self.assertEqual(r.headers["Access-Control-Allow-Origin"], "*")


class TestStatus(ServerCase):
    def testAreasAndHealth(self):
        fc = self.client.get("/route/api/v1/areas").get_json()
        [area] = fc["features"]
        self.assertTrue(area["properties"]["built"])
        self.assertEqual(self.client.get("/route/health").status_code, 200)

    def testUnbuiltAreaIs503(self):
        cfg = ta.config(Path(tempfile.mkdtemp()))
        client = create_app(cfg).test_client()
        self.assertEqual(client.get("/route/health").status_code, 503)
        r = client.post("/route/api/v1/route", data=json.dumps(self.points(self.a, self.b)),
                        content_type="application/json")
        self.assertEqual(r.status_code, 503)

    def testDebugPage(self):
        r = self.client.get("/route/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"api/v1/route", r.data)


if __name__ == "__main__":
    unittest.main()
