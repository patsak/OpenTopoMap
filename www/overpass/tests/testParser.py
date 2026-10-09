import unittest

from overpass.parser import QLError, parse
from overpass.syntax import (
    AreaFilter,
    AroundFilter,
    Binary,
    BboxFilter,
    Call,
    ForEach,
    GlobalBboxFilter,
    IdFilter,
    IfFilter,
    IsIn,
    MapToArea,
    Out,
    PolyFilter,
    Query,
    Recurse,
    RecurseFilter,
    SetCopy,
    TagFilter,
    TagValue,
    Union,
)


def statements(text):
    return parse(text).statements


class TestSettings(unittest.TestCase):
    def testDefaultsToXmlAndOverpassTimeout(self):
        settings = parse("node(1);out;").settings
        self.assertEqual(settings.output, "xml")
        self.assertEqual(settings.timeout, 180)

    def testReadsOutTimeoutAndBbox(self):
        settings = parse("[out:json][timeout:25][bbox:50.0,10.0,50.1,10.1];node(1);out;").settings
        self.assertEqual(settings.output, "json")
        self.assertEqual(settings.timeout, 25)
        self.assertEqual(settings.bbox, (50.0, 10.0, 50.1, 10.1))

    def testReadsCsvFieldsHeaderAndSeparator(self):
        settings = parse('[out:csv(::id, name, "addr:street"; false; ",")];node(1);out;').settings
        self.assertEqual(settings.output, "csv")
        self.assertEqual(settings.csv.fields, ["::id", "name", "addr:street"])
        self.assertFalse(settings.csv.header)
        self.assertEqual(settings.csv.separator, ",")

    def testRefusesHistoryAsStaticError(self):
        with self.assertRaises(QLError) as ctx:
            parse('[date:"2020-01-01T00:00:00Z"];node(1);out;')
        self.assertEqual(ctx.exception.kind, "static")


class TestTagFilters(unittest.TestCase):
    def filters(self, text):
        return statements(text)[0].filters

    def testAllOperators(self):
        got = self.filters('node[a][!b][c=d][e!=f][g~"^h"][i!~j]["k l"="m n"];')
        self.assertEqual(
            got,
            [
                TagFilter(op="exists", key="a"),
                TagFilter(op="not_exists", key="b"),
                TagFilter(op="=", key="c", value="d"),
                TagFilter(op="!=", key="e", value="f"),
                TagFilter(op="~", key="g", value="^h"),
                TagFilter(op="!~", key="i", value="j"),
                TagFilter(op="=", key="k l", value="m n"),
            ],
        )

    def testCaseInsensitiveRegexAndKeyRegex(self):
        got = self.filters('node[name~"anchor",i][~"^addr:"~"."];')
        self.assertEqual(got[0], TagFilter(op="~", key="name", value="anchor", ignore_case=True))
        self.assertEqual(got[1], TagFilter(op="key~", key="^addr:", value="."))

    def testBareValuesKeepColonsAndSlashes(self):
        got = self.filters("way[name:en=Main][website=http://example.org/x];")
        self.assertEqual(got[0], TagFilter(op="=", key="name:en", value="Main"))
        self.assertEqual(got[1].value, "http://example.org/x")

    def testRegexEscapesReachTheRegex(self):
        got = self.filters(r'node[housenumber~"^\d+$"];')
        self.assertEqual(got[0].value, r"^\d+$")

    def testEmptyValue(self):
        self.assertEqual(self.filters('node[note=""];')[0], TagFilter(op="=", key="note", value=""))

    def testComparisonInsideBracketsIsAParseError(self):
        # Overpass has no [ele>4000]; it must not turn into a key "ele>4000".
        with self.assertRaises(QLError):
            parse("node[ele>4000];out;")

    def testInvalidRegexIsAStaticError(self):
        with self.assertRaises(QLError) as ctx:
            parse('node[name~"("];')
        self.assertEqual(ctx.exception.kind, "static")


class TestParenFilters(unittest.TestCase):
    def filters(self, text):
        return statements(text)[0].filters

    def testBboxIdAndIdList(self):
        self.assertEqual(self.filters("node(50,10,51,11);"), [BboxFilter(50, 10, 51, 11)])
        self.assertEqual(self.filters("node(42);"), [IdFilter([42])])
        self.assertEqual(self.filters("node(id:1,2,3);"), [IdFilter([1, 2, 3])])
        self.assertEqual(self.filters("node(bbox);"), [GlobalBboxFilter()])

    def testAroundForms(self):
        self.assertEqual(self.filters("node(around:100,50.0,10.0);"), [AroundFilter(100, None, [(50.0, 10.0)])])
        self.assertEqual(
            self.filters("node(around:5,50,10,51,11);"), [AroundFilter(5, None, [(50.0, 10.0), (51.0, 11.0)])]
        )
        self.assertEqual(self.filters("node(around.p:20);"), [AroundFilter(20, "p", [])])
        self.assertEqual(self.filters("node(around:20);"), [AroundFilter(20, "_", [])])

    def testPolyAreaPivotAndRecursion(self):
        self.assertEqual(
            self.filters('node(poly:"50 10 50 11 51 11");'),
            [PolyFilter([(50.0, 10.0), (50.0, 11.0), (51.0, 11.0)])],
        )
        self.assertEqual(self.filters("node(area.a);"), [AreaFilter(input_set="a")])
        self.assertEqual(self.filters("node(area);"), [AreaFilter(input_set="_")])
        self.assertEqual(self.filters("node(area:3600001000);"), [AreaFilter(area_id=3600001000)])
        self.assertEqual(self.filters('node(r.t:"stop");'), [RecurseFilter("r", "t", "stop")])
        self.assertEqual(self.filters("way(bn);"), [RecurseFilter("bn", "_", None)])

    def testIfExpressionPrecedence(self):
        (f,) = self.filters('node(if: t["ele"] > 1000 && is_tag("name"));')
        self.assertIsInstance(f, IfFilter)
        self.assertEqual(f.expr.op, "&&")
        self.assertEqual(f.expr.left, Binary(">", TagValue("ele"), f.expr.left.right))
        self.assertEqual(f.expr.right, Call("is_tag", [f.expr.right.args[0]]))

    def testUnknownFunctionIsAStaticError(self):
        with self.assertRaises(QLError) as ctx:
            parse("node(if: frobnicate());")
        self.assertEqual(ctx.exception.kind, "static")

    def testMetadataFiltersAreRefused(self):
        for text in ('node(user:"x");', 'node(newer:"2020-01-01T00:00:00Z");'):
            with self.subTest(text=text), self.assertRaises(QLError) as ctx:
                parse(text)
            self.assertEqual(ctx.exception.kind, "static")


class TestStatements(unittest.TestCase):
    def testQueryWithInputSetsAndTarget(self):
        (q,) = statements("node.a.b[amenity]->.c;")
        self.assertEqual((q.element_type, q.input_sets, q.into), ("node", ["a", "b"], "c"))

    def testRelIsRelation(self):
        self.assertEqual(statements("rel(1);")[0].element_type, "relation")

    def testBareQueryIsRefused(self):
        # "node;" would mean every node in the database.
        with self.assertRaises(QLError):
            parse("node;")

    def testUnionDifferenceAndSetCopy(self):
        u, d, c = statements("(node(1); way(2);)->.x; (node[a]; - node[b];); .x->.y;")
        self.assertIsInstance(u, Union)
        self.assertEqual(u.into, "x")
        self.assertEqual([neg for neg, _ in d.members], [False, True])
        self.assertEqual(c, SetCopy(line=1, input_set="x", into="y"))

    def testRecursionForms(self):
        a, b, c = statements(">; .w <<->.up; (._; >;);")[:3]
        self.assertEqual(a, Recurse(line=1, op=">"))
        self.assertEqual(b, Recurse(line=1, op="<<", input_set="w", into="up"))
        self.assertIsInstance(c, Union)

    def testOutOptions(self):
        a, b, c = statements("out; .x out geom 10 qt; out count;")
        self.assertEqual(a, Out(line=1))
        self.assertEqual(b, Out(line=1, input_set="x", geometry="geom", limit=10))
        self.assertEqual(c.verbosity, "count")

    def testForeachBothSyntaxes(self):
        new, old = statements("foreach.a->.n(.n out;); .a foreach->.n{.n out;}")
        for st in (new, old):
            self.assertIsInstance(st, ForEach)
            self.assertEqual((st.input_set, st.loop_set, len(st.body)), ("a", "n", 1))

    def testSpacesBeforeSetNames(self):
        # Overpass ignores whitespace there; "foreach .c -> .d (" is how
        # overpass-turbo users often write it.
        (st,) = statements("foreach .c -> .d ( .d out; );")
        self.assertEqual((st.input_set, st.loop_set), ("c", "d"))
        (q,) = statements("node .a [amenity];")
        self.assertEqual(q.input_sets, ["a"])
        self.assertEqual(statements("node(around .p:20);")[0].filters, [AroundFilter(20, "p", [])])

    def testCountIsAnAggregateOverATypeName(self):
        (f,) = statements("node._(if: count(ways) == 0);")[0].filters
        self.assertEqual(f.expr.left, Call("count", [f.expr.left.args[0]]))
        self.assertEqual(f.expr.left.args[0].value, "ways")
        with self.assertRaises(QLError) as ctx:
            parse("node(if: count(lines) > 0);")
        self.assertEqual(ctx.exception.kind, "static")

    def testMapToAreaAndIsIn(self):
        m, i, j = statements(".r map_to_area->.a; is_in(50.0,10.0); .n is_in->.where;")
        self.assertEqual(m, MapToArea(line=1, input_set="r", into="a"))
        self.assertEqual(i, IsIn(line=1, point=(50.0, 10.0)))
        self.assertEqual(j, IsIn(line=1, input_set="n", into="where"))

    def testCommentsAndLineNumbers(self):
        text = "// first\nnode(1); /* a\nblock */\nway(2);"
        a, b = statements(text)
        self.assertEqual((a.line, b.line), (2, 4))

    def testCyrillicLetterInASetNameIsNamed(self):
        # ".с" with a Cyrillic es, next to sets named with a Latin c.
        for text in ("node(around.\u0441:100);", "node(around.c\u0441:100);", ".\u0441 out;"):
            with self.subTest(text=text), self.assertRaises(QLError) as ctx:
                parse(text)
            self.assertIn("CYRILLIC SMALL LETTER ES (U+0441)", str(ctx.exception))

    def testErrorsCarryTheLine(self):
        with self.assertRaises(QLError) as ctx:
            parse("node(1);\nout;\nnod(2);")
        self.assertEqual(ctx.exception.line, 3)
        self.assertIn("line 3: parse error", str(ctx.exception))

    def testUnsupportedStatementsAreStaticErrors(self):
        for text in ("make stat n=1;", "convert item ::id=id();", "complete { node(1); };"):
            with self.subTest(text=text), self.assertRaises(QLError) as ctx:
                parse(text)
            self.assertEqual(ctx.exception.kind, "static")

    def testQueryIsQuery(self):
        self.assertIsInstance(statements("area[name=X];")[0], Query)


if __name__ == "__main__":
    unittest.main()
