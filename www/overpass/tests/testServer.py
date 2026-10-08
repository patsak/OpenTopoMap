"""The HTTP side: where the query is read from and what goes back."""

import json
import os
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote

import pytest

# Collected by the whole-www pytest run too, whose venvs need not have geodesk.
pytest.importorskip("geodesk")

from overpass.library import Library, LibraryUnavailable
from overpass.server import create_app
from overpass.tests.golaide import GolTestCase, gol_without_waynode_ids


class ServerCase(GolTestCase):
    def setUp(self):
        self.client = create_app(str(self.gol)).test_client()

    def post(self, text):
        return self.client.post("/api/interpreter", data={"data": text})

    def elements(self, response):
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return json.loads(response.get_data(as_text=True))["elements"]


class TestRequestForms(ServerCase):
    QUERY = "[out:json];node[amenity=pub];out ids;"

    def testGetWithDataParameter(self):
        r = self.client.get("/api/interpreter?data=" + quote(self.QUERY))
        self.assertEqual(self.elements(r), [{"type": "node", "id": 1}])

    def testPostForm(self):
        self.assertEqual(self.elements(self.post(self.QUERY)), [{"type": "node", "id": 1}])

    def testPostRawBody(self):
        r = self.client.post("/api/interpreter", data=self.QUERY.encode(), content_type="text/plain")
        self.assertEqual(self.elements(r), [{"type": "node", "id": 1}])

    def testPostBareScriptLabelledAsForm(self):
        # What `curl -d '<script>'` sends: a form content type, no data=.
        r = self.client.post(
            "/api/interpreter", data=self.QUERY.encode(), content_type="application/x-www-form-urlencoded"
        )
        self.assertEqual(self.elements(r), [{"type": "node", "id": 1}])

    def testPostMultipartForm(self):
        r = self.client.post("/api/interpreter", data={"data": self.QUERY}, content_type="multipart/form-data")
        self.assertEqual(self.elements(r), [{"type": "node", "id": 1}])

    def testPostUrlencodedBodyWithoutFormContentType(self):
        r = self.client.post("/api/interpreter", data=("data=" + quote(self.QUERY)).encode(), content_type="text/plain")
        self.assertEqual(self.elements(r), [{"type": "node", "id": 1}])

    def testCorsHeader(self):
        self.assertEqual(self.post(self.QUERY).headers["Access-Control-Allow-Origin"], "*")


class TestErrors(ServerCase):
    def testParseErrorIsAn400HtmlPage(self):
        r = self.post("node[amenity=pub;out;")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.mimetype, "text/html")
        self.assertIn("<strong style=\"color:#FF0000\">Error</strong>: line 1: parse error:", r.get_data(as_text=True))

    def testXmlQueryLanguageIsRefused(self):
        r = self.post('<osm-script><query type="node"/></osm-script>')
        self.assertEqual(r.status_code, 400)
        self.assertIn("static error", r.get_data(as_text=True))

    def testEmptyQuery(self):
        self.assertEqual(self.client.get("/api/interpreter").status_code, 400)

    def testRuntimeErrorKeepsStatus200AndAddsRemark(self):
        r = self.post("[out:json][timeout:0];node[amenity];out;")
        self.assertEqual(r.status_code, 200)
        self.assertIn("runtime error", json.loads(r.get_data(as_text=True))["remark"])


class TestJson(ServerCase):
    def testEnvelope(self):
        doc = json.loads(self.post("[out:json];node(1);out;").get_data(as_text=True))
        self.assertEqual(doc["version"], 0.6)
        self.assertIn("timestamp_osm_base", doc["osm3s"])
        self.assertEqual(
            doc["elements"],
            [{"type": "node", "id": 1, "lat": 50.01, "lon": 10.01, "tags": {"amenity": "pub", "name": "The Anchor"}}],
        )

    def testWayGeometry(self):
        (way,) = self.elements(self.post("[out:json];way(101);out geom;"))
        self.assertEqual(way["nodes"], [30, 31, 32, 33, 30])
        self.assertEqual(way["bounds"], {"minlat": 50.01, "minlon": 10.03, "maxlat": 50.012, "maxlon": 10.032})
        self.assertEqual(way["geometry"][1], {"lat": 50.01, "lon": 10.032})
        self.assertEqual(way["tags"], {"building": "yes", "name": "Hall"})

    def testCenterAndTagsOnly(self):
        (way,) = self.elements(self.post("[out:json];way(101);out tags center;"))
        self.assertEqual(way["center"], {"lat": 50.011, "lon": 10.031})
        self.assertNotIn("nodes", way)

    def testRelationMembersWithGeometry(self):
        (rel,) = self.elements(self.post("[out:json];rel(2000);out geom;"))
        by_ref = {(m["type"], m["ref"]): m for m in rel["members"]}
        self.assertEqual(by_ref[("node", 1)], {"type": "node", "ref": 1, "role": "stop", "lat": 50.01, "lon": 10.01})
        self.assertEqual(len(by_ref[("way", 200)]["geometry"]), 3)
        self.assertNotIn("geometry", by_ref[("relation", 1000)])

    def testSkelHasNoTags(self):
        (way,) = self.elements(self.post("[out:json];way(200);out skel;"))
        self.assertEqual(way, {"type": "way", "id": 200, "nodes": [20, 21, 22]})

    def testAreaAndCount(self):
        elements = self.elements(self.post("[out:json];area[name=Testtown];out;out count;"))
        self.assertEqual(elements[0]["type"], "area")
        self.assertEqual(elements[0]["id"], 3600001000)
        self.assertEqual(elements[1]["tags"]["areas"], "1")


class TestXmlAndCsv(ServerCase):
    def testXmlIsTheDefault(self):
        r = self.post("way(200);out geom;")
        self.assertEqual(r.mimetype, "application/osm3s+xml")
        root = ET.fromstring(r.get_data())
        way = root.find("way")
        self.assertEqual(way.get("id"), "200")
        self.assertEqual([nd.get("ref") for nd in way.findall("nd")], ["20", "21", "22"])
        self.assertEqual(way.find("nd").get("lat"), "50.0200000")
        self.assertEqual(root.find("meta").get("osm_base") is not None, True)

    def testXmlEscapesValues(self):
        r = self.post('node[name="Café"];out;')
        root = ET.fromstring(r.get_data())
        self.assertEqual(root.find("node/tag[@k='name']").get("v"), "Café")

    def testCsv(self):
        r = self.post('[out:csv(::id,::type,name,::lat,::lon;true;",")];node[amenity];out;')
        self.assertEqual(r.mimetype, "text/csv")
        lines = r.get_data(as_text=True).splitlines()
        self.assertEqual(lines[0], "@id,@type,name,@lat,@lon")
        self.assertEqual(lines[1], "1,node,The Anchor,50.0100000,10.0100000")
        self.assertEqual(len(lines), 4)

    def testCsvCount(self):
        r = self.post("[out:csv(::count,::count:nodes;false)];node[amenity];out count;")
        self.assertEqual(r.get_data(as_text=True), "3\t3\n")


class TestStatusAndHealth(ServerCase):
    def testStatus(self):
        text = self.client.get("/api/status").get_data(as_text=True)
        self.assertIn("slots available now", text)

    def testHealth(self):
        self.assertEqual(self.client.get("/health").get_json()["status"], "ok")


class TestLibrary(GolTestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)

    def testMissingGolAnswers503UntilItAppears(self):
        target = self.work / "late.gol"
        client = create_app(str(target)).test_client()
        self.assertEqual(client.post("/api/interpreter", data={"data": "node(1);out;"}).status_code, 503)
        self.assertEqual(client.get("/health").status_code, 503)
        shutil.copyfile(self.gol, target)
        self.assertEqual(client.post("/api/interpreter", data={"data": "node(1);out;"}).status_code, 200)

    def testReplacedGolIsReopened(self):
        target = self.work / "swap.gol"
        shutil.copyfile(self.gol, target)
        library = Library(target)
        first = library.features()
        tmp = self.work / "swap.gol.new"
        shutil.copyfile(self.gol, tmp)
        os.replace(tmp, target)
        self.assertIsNot(library.features(), first)

    def testGolWithoutWaynodeIdsIsRefused(self):
        with self.assertRaises(LibraryUnavailable) as ctx:
            Library(gol_without_waynode_ids()).features()
        self.assertIn("-w", str(ctx.exception))

    def testTimestampComesFromTheStateFile(self):
        target = self.work / "stamped.gol"
        shutil.copyfile(self.gol, target)
        (self.work / "stamped.gol.state.txt").write_text("sequenceNumber=1\ntimestamp=2026-09-01T20\\:20\\:50Z\n")
        self.assertEqual(Library(target).timestamp(), "2026-09-01T20:20:50Z")


if __name__ == "__main__":
    unittest.main()
