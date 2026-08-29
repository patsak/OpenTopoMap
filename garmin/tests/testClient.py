import unittest
from uuid import uuid4

from mapsvc.client import parse_client_id, resolve_client_id


class TestClientId(unittest.TestCase):
    def testParsesCanonicalUuid(self):
        raw = str(uuid4())
        self.assertEqual(parse_client_id(raw), raw)

    def testParsesUuidWithSpaces(self):
        raw = str(uuid4())
        self.assertEqual(parse_client_id(f"  {raw}  "), raw)

    def testRejectsEmptyAndGarbage(self):
        self.assertIsNone(parse_client_id(""))
        self.assertIsNone(parse_client_id(None))
        self.assertIsNone(parse_client_id("not-a-uuid"))

    def testResolveReusesValidCookie(self):
        raw = str(uuid4())
        client_id, is_new = resolve_client_id(raw)
        self.assertEqual(client_id, raw)
        self.assertFalse(is_new)

    def testResolveIssuesNewIdWhenMissing(self):
        client_id, is_new = resolve_client_id(None)
        self.assertTrue(is_new)
        self.assertEqual(parse_client_id(client_id), client_id)


if __name__ == "__main__":
    unittest.main()
