"""Overpass QL → ``overpass.syntax``.

A recursive-descent parser working straight on the text rather than on a token
stream: what counts as a token in Overpass QL depends on where it stands. An
unquoted tag value may hold ``:`` and ``/`` (``[website=http://x]``), ``-`` is
a set difference at the start of a union member and a minus sign inside an
``if:``, and ``.a`` is an input set after a type name but a statement of its own
at the start of one.

Errors carry the line and the word Overpass itself uses - ``parse error`` for
text that is not Overpass QL, ``static error`` for valid Overpass QL this
service does not run (``[date:]``, ``make``, ``(user:)``...). Clients show the
message as is, so keeping that shape keeps them readable.
"""

from __future__ import annotations

import re
import unicodedata

from overpass.syntax import (
    COUNT_TYPES,
    DEFAULT_SET,
    FUNCTIONS,
    AreaFilter,
    AroundFilter,
    Binary,
    BboxFilter,
    Call,
    CsvFormat,
    Expr,
    Filter,
    ForEach,
    GlobalBboxFilter,
    IdFilter,
    IfFilter,
    IsIn,
    Literal,
    MapToArea,
    Out,
    PivotFilter,
    PolyFilter,
    Query,
    Recurse,
    RecurseFilter,
    Script,
    SetCopy,
    Settings,
    Statement,
    TagFilter,
    TagValue,
    Ternary,
    Unary,
    Union,
)

QUERY_TYPES = {
    "node": "node",
    "way": "way",
    "rel": "relation",
    "relation": "relation",
    "nwr": "nwr",
    "nw": "nw",
    "nr": "nr",
    "wr": "wr",
    "area": "area",
}

# Valid Overpass QL that needs data a GOL does not have (history, metadata) or
# machinery the prototype has not grown yet. Refused up front, by name, so the
# caller learns why instead of getting an empty result.
UNSUPPORTED_STATEMENTS = {
    "derived": "derived elements",
    "make": "make",
    "convert": "convert",
    "complete": "complete",
    "retro": "retro",
    "compare": "compare",
    "timeline": "timeline",
    "local": "local",
    "if": "if blocks",
    "for": "for loops",
}
UNSUPPORTED_FILTERS = {
    "newer": "it needs element timestamps, which a GOL does not store",
    "changed": "it needs history, which a GOL does not store",
    "user": "it needs element metadata, which a GOL does not store",
    "uid": "it needs element metadata, which a GOL does not store",
    "user_touched": "it needs history, which a GOL does not store",
    "uid_touched": "it needs history, which a GOL does not store",
}
UNSUPPORTED_SETTINGS = {
    "date": "a GOL is a single snapshot, without history",
    "diff": "a GOL is a single snapshot, without history",
    "adiff": "a GOL is a single snapshot, without history",
}

OUT_VERBOSITY = {"ids", "skel", "body", "tags", "meta", "count"}
OUT_GEOMETRY = {"geom", "bb", "center"}
OUT_IGNORED = {"asc", "qt", "noids"}
RECURSE_FILTERS = {"w", "r", "bn", "bw", "br"}

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?")
# An unquoted key or value: anything up to a character with a meaning of its own.
# "<" and ">" are among them although a tag filter has no use for them: they
# make [ele>4000] a parse error, as in Overpass, instead of a key "ele>4000".
_BARE = re.compile(r"[^\s\[\]()\"'=!~,;{}<>]+")
_CSV_SPECIAL = re.compile(r"[A-Za-z_]+(:[A-Za-z_]+)*")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'"}


class QLError(Exception):
    def __init__(self, line: int, kind: str, message: str):
        super().__init__(f"line {line}: {kind} error: {message}")
        self.line = line
        self.kind = kind
        self.message = message


def parse(text: str) -> Script:
    return _Parser(text).script()


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.pos = 0

    # --- scanning ---------------------------------------------------------

    def line(self, pos: int | None = None) -> int:
        return self.text.count("\n", 0, self.pos if pos is None else pos) + 1

    def fail(self, message: str, pos: int | None = None) -> QLError:
        return QLError(self.line(pos), "parse", message)

    def unsupported(self, message: str, pos: int | None = None) -> QLError:
        return QLError(self.line(pos), "static", message)

    def skip(self) -> None:
        text = self.text
        while self.pos < len(text):
            ch = text[self.pos]
            if ch.isspace():
                self.pos += 1
            elif text.startswith("//", self.pos):
                end = text.find("\n", self.pos)
                self.pos = len(text) if end < 0 else end + 1
            elif text.startswith("/*", self.pos):
                end = text.find("*/", self.pos + 2)
                if end < 0:
                    raise self.fail("unterminated comment")
                self.pos = end + 2
            else:
                break

    def at_end(self) -> bool:
        self.skip()
        return self.pos >= len(self.text)

    def peek(self, literal: str) -> bool:
        self.skip()
        return self.text.startswith(literal, self.pos)

    def accept(self, literal: str) -> bool:
        if self.peek(literal):
            self.pos += len(literal)
            return True
        return False

    def expect(self, literal: str) -> None:
        if not self.accept(literal):
            raise self.fail(f'"{literal}" expected, found {self.describe()}')

    def describe(self) -> str:
        self.skip()
        if self.pos >= len(self.text):
            return "end of input"
        snippet = self.text[self.pos : self.pos + 12].split("\n")[0]
        return f'"{snippet}"'

    def peek_word(self) -> str | None:
        self.skip()
        m = _IDENT.match(self.text, self.pos)
        return m.group(0) if m else None

    def word(self) -> str:
        w = self.peek_word()
        if w is None:
            raise self.fail(f"a name expected, found {self.describe()}")
        self.pos += len(w)
        return w

    def accept_word(self, w: str) -> bool:
        if self.peek_word() == w:
            self.pos += len(w)
            return True
        return False

    def string(self) -> str:
        self.skip()
        quote = self.text[self.pos : self.pos + 1]
        if quote not in ('"', "'"):
            raise self.fail(f"a quoted string expected, found {self.describe()}")
        start = self.pos
        self.pos += 1
        out = []
        while True:
            if self.pos >= len(self.text):
                raise self.fail("unterminated string", start)
            ch = self.text[self.pos]
            if ch == quote:
                self.pos += 1
                return "".join(out)
            if ch == "\\":
                nxt = self.text[self.pos + 1 : self.pos + 2]
                if nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", self.text[self.pos + 2 : self.pos + 6]):
                    out.append(chr(int(self.text[self.pos + 2 : self.pos + 6], 16)))
                    self.pos += 6
                    continue
                # Unknown escapes keep their backslash: in a regex "\d" has to
                # reach the regex engine as \d.
                out.append(_ESCAPES.get(nxt, "\\" + nxt))
                self.pos += 2
                continue
            out.append(ch)
            self.pos += 1

    def token(self) -> str:
        """A key, a value or a role: quoted, or bare up to the next delimiter."""
        self.skip()
        if self.text[self.pos : self.pos + 1] in ('"', "'"):
            return self.string()
        m = _BARE.match(self.text, self.pos)
        if not m:
            raise self.fail(f"a key or value expected, found {self.describe()}")
        self.pos = m.end()
        return m.group(0)

    def number(self) -> float:
        self.skip()
        m = _NUMBER.match(self.text, self.pos)
        if not m:
            raise self.fail(f"a number expected, found {self.describe()}")
        self.pos = m.end()
        return float(m.group(0))

    def integer(self) -> int:
        self.skip()
        m = re.compile(r"\d+").match(self.text, self.pos)
        if not m:
            raise self.fail(f"an integer expected, found {self.describe()}")
        self.pos = m.end()
        return int(m.group(0))

    def peek_number(self) -> bool:
        self.skip()
        return bool(_NUMBER.match(self.text, self.pos))

    def set_name(self) -> str:
        """The name after a ``.``: ``_`` and plain identifiers."""
        self.skip()
        m = _IDENT.match(self.text, self.pos)
        end = m.end() if m else self.pos
        # A Cyrillic "с" or "а" typed into ".c" looks right and is another
        # name. Overpass would take it as a different, empty set and answer
        # wrongly without a word; say which character it is instead.
        nxt = self.text[end : end + 1]
        if nxt and not nxt.isascii() and nxt.isalnum():
            raise self.fail(
                f'set names are Latin letters, digits and "_": "{nxt}" is '
                f"{unicodedata.name(nxt, 'a non-Latin character')} (U+{ord(nxt):04X})",
                end,
            )
        if not m:
            raise self.fail(f"a set name expected after \".\", found {self.describe()}")
        self.pos = m.end()
        return m.group(0)

    def optional_input(self) -> str:
        """``.name`` after the keyword before it, or the default set.

        Whitespace may come between them, as in Overpass: ``foreach .c`` is
        ``foreach.c``.
        """
        if self.peek(".") and not self.peek(".."):
            self.pos += 1
            return self.set_name()
        return DEFAULT_SET

    def into(self) -> str:
        if self.accept("->"):
            self.expect(".")
            return self.set_name()
        return DEFAULT_SET

    # --- script and settings ------------------------------------------------

    def script(self) -> Script:
        settings = Settings()
        if self.peek("["):
            self.settings(settings)
        statements = self.statements(terminators=())
        if not self.at_end():
            raise self.fail(f"unexpected {self.describe()}")
        return Script(settings=settings, statements=statements)

    def settings(self, settings: Settings) -> None:
        while self.accept("["):
            pos = self.pos
            name = self.word()
            self.expect(":")
            if name in UNSUPPORTED_SETTINGS:
                raise self.unsupported(f"[{name}:] is not supported: {UNSUPPORTED_SETTINGS[name]}", pos)
            if name == "out":
                fmt = self.word()
                if fmt == "csv":
                    settings.csv = self.csv_format()
                elif fmt not in ("json", "xml"):
                    raise self.unsupported(f'output format "{fmt}" is not supported (json, xml and csv are)', pos)
                settings.output = fmt
            elif name == "timeout":
                settings.timeout = self.integer()
            elif name == "maxsize":
                settings.maxsize = self.integer()
            elif name == "bbox":
                south, west, north, east = self.coordinates(4)
                settings.bbox = (south, west, north, east)
            else:
                raise self.fail(f'unknown setting "{name}"', pos)
            self.expect("]")
        self.expect(";")

    def csv_format(self) -> CsvFormat:
        self.expect("(")
        fields = []
        while True:
            if self.accept("::"):
                # ::id, ::lat... and ::count:nodes, which has a colon of its own.
                m = _CSV_SPECIAL.match(self.text, self.pos)
                if not m:
                    raise self.fail(f"a field name expected after \"::\", found {self.describe()}")
                self.pos = m.end()
                fields.append("::" + m.group(0))
            else:
                fields.append(self.token())
            if not self.accept(","):
                break
        fmt = CsvFormat(fields=fields)
        if self.accept(";"):
            flag = self.word()
            if flag not in ("true", "false"):
                raise self.fail('"true" or "false" expected for the csv header flag')
            fmt.header = flag == "true"
            if self.accept(";"):
                fmt.separator = self.string()
        self.expect(")")
        return fmt

    def coordinates(self, count: int) -> list[float]:
        values = [self.number()]
        for _ in range(count - 1):
            self.expect(",")
            values.append(self.number())
        return values

    # --- statements ---------------------------------------------------------

    def statements(self, terminators: tuple[str, ...]) -> list[Statement]:
        out = []
        while not self.at_end() and not any(self.peek(t) for t in terminators):
            out.append(self.statement())
        return out

    def statement(self) -> Statement:
        self.skip()
        line = self.line()
        if self.peek("("):
            return self.union(line)
        if self.peek(".") and not self.peek(".."):
            self.pos += 1
            input_set = self.set_name()
            return self.after_input_set(line, input_set)
        if self.peek(">") or self.peek("<"):
            return self.recurse(line, DEFAULT_SET)

        pos = self.pos
        w = self.peek_word()
        if w is None:
            raise self.fail(f"a statement expected, found {self.describe()}")
        if w in QUERY_TYPES:
            self.pos += len(w)
            return self.query(line, QUERY_TYPES[w])
        if w in ("out", "foreach", "map_to_area", "is_in"):
            self.pos += len(w)
            return self.keyword_statement(line, w, None)
        if w in UNSUPPORTED_STATEMENTS:
            raise self.unsupported(f'"{w}" is not supported by this service yet', pos)
        raise self.fail(f'unknown type "{w}"', pos)

    def after_input_set(self, line: int, input_set: str) -> Statement:
        if self.peek("->"):
            into = self.into()
            self.expect(";")
            return SetCopy(line=line, input_set=input_set, into=into)
        if self.accept(";"):
            return SetCopy(line=line, input_set=input_set)
        if self.peek(">") or self.peek("<"):
            return self.recurse(line, input_set)
        pos = self.pos
        w = self.peek_word()
        if w in ("out", "foreach", "map_to_area", "is_in"):
            self.pos += len(w)
            return self.keyword_statement(line, w, input_set)
        raise self.fail(f"a statement expected after .{input_set}, found {self.describe()}", pos)

    def keyword_statement(self, line: int, keyword: str, input_set: str | None) -> Statement:
        # The new syntax glues the input set to the keyword (foreach.a), the old
        # one puts it in front (.a foreach). Accept both, not both at once.
        glued = self.optional_input()
        if input_set is None:
            input_set = glued
        elif glued != DEFAULT_SET:
            raise self.fail(f"two input sets for {keyword}")

        if keyword == "out":
            return self.out(line, input_set)
        if keyword == "foreach":
            loop_set = self.into()
            if self.accept("{"):
                closing = "}"
            else:
                self.expect("(")
                closing = ")"
            body = self.statements(terminators=(closing,))
            self.expect(closing)
            self.accept(";")
            return ForEach(line=line, body=body, input_set=input_set, loop_set=loop_set)
        if keyword == "map_to_area":
            into = self.into()
            self.expect(";")
            return MapToArea(line=line, input_set=input_set, into=into)
        # is_in
        point = None
        if self.accept("("):
            lat, lon = self.coordinates(2)
            self.expect(")")
            point = (lat, lon)
        into = self.into()
        self.expect(";")
        return IsIn(line=line, point=point, input_set=input_set, into=into)

    def union(self, line: int) -> Union:
        self.expect("(")
        members = []
        while not self.peek(")"):
            if self.at_end():
                raise self.fail('")" expected to close the block, found end of input')
            negated = self.accept("-")
            if negated and members and any(neg for neg, _ in members):
                raise self.fail("a difference takes exactly one statement after \"-\"")
            members.append((negated, self.statement()))
        self.expect(")")
        if any(neg for neg, _ in members) and (len(members) != 2 or not members[1][0]):
            raise self.fail('a difference is "(statement; - statement;)"', self.pos)
        into = self.into()
        self.expect(";")
        return Union(line=line, members=members, into=into)

    def recurse(self, line: int, input_set: str) -> Recurse:
        for op in (">>", "<<", ">", "<"):
            if self.accept(op):
                into = self.into()
                self.expect(";")
                return Recurse(line=line, op=op, input_set=input_set, into=into)
        raise self.fail(f"a recursion expected, found {self.describe()}")

    def out(self, line: int, input_set: str) -> Out:
        out = Out(line=line, input_set=input_set)
        while not self.accept(";"):
            if self.at_end():
                raise self.fail('";" expected after out')
            if self.peek_number():
                out.limit = self.integer()
                continue
            if self.accept("("):
                # out geom(s,w,n,e): the clip box. Accepted and not applied -
                # the whole geometry is returned, which is a superset.
                self.coordinates(4)
                self.expect(")")
                continue
            pos = self.pos
            w = self.word()
            if w in OUT_VERBOSITY:
                out.verbosity = w
            elif w in OUT_GEOMETRY:
                out.geometry = w
            elif w not in OUT_IGNORED:
                raise self.fail(f'unknown out option "{w}"', pos)
        return out

    # --- queries ------------------------------------------------------------

    def query(self, line: int, element_type: str) -> Query:
        input_sets = []
        while self.peek(".") and not self.peek(".."):
            self.pos += 1
            input_sets.append(self.set_name())
        filters: list[Filter] = []
        while True:
            if self.peek("["):
                self.pos += 1
                filters.append(self.tag_filter())
            elif self.peek("(") :
                self.pos += 1
                filters.append(self.paren_filter(element_type))
            else:
                break
        into = self.into()
        self.expect(";")
        if not filters and not input_sets:
            # Overpass refuses "node;" too: it would mean the whole database.
            raise self.fail(f"a {element_type} query needs at least one condition", self.pos)
        return Query(line=line, element_type=element_type, input_sets=input_sets, filters=filters, into=into)

    def tag_filter(self) -> TagFilter:
        pos = self.pos
        if self.accept("!"):
            key = self.token()
            self.expect("]")
            return TagFilter(op="not_exists", key=key)
        if self.accept("~"):
            key = self.token()
            self.expect("~")
            value = self.token()
            ignore_case = self.case_flag()
            self.expect("]")
            self.check_regex(key, ignore_case, pos)
            self.check_regex(value, ignore_case, pos)
            return TagFilter(op="key~", key=key, value=value, ignore_case=ignore_case)
        key = self.token()
        if self.accept("]"):
            return TagFilter(op="exists", key=key)
        for op in ("!=", "!~", "=", "~"):
            if self.accept(op):
                break
        else:
            raise self.fail(f'"=", "!=", "~", "!~" or "]" expected, found {self.describe()}')
        value = "" if self.peek("]") and op == "=" else self.token()
        ignore_case = self.case_flag()
        self.expect("]")
        if op in ("~", "!~"):
            self.check_regex(value, ignore_case, pos)
        elif ignore_case:
            raise self.fail('",i" only goes with a regular expression', pos)
        return TagFilter(op=op, key=key, value=value, ignore_case=ignore_case)

    def case_flag(self) -> bool:
        if self.accept(","):
            if self.word() != "i":
                raise self.fail('"i" expected after ","')
            return True
        return False

    def check_regex(self, pattern: str, ignore_case: bool, pos: int) -> None:
        try:
            re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            raise QLError(self.line(pos), "static", f'invalid regular expression "{pattern}": {exc}') from None

    def paren_filter(self, element_type: str) -> Filter:
        pos = self.pos
        if self.peek_number():
            values = [self.number()]
            while self.accept(","):
                values.append(self.number())
            self.expect(")")
            if len(values) == 4:
                south, west, north, east = values
                return BboxFilter(south=south, west=west, north=north, east=east)
            if all(v.is_integer() and v > 0 for v in values):
                return IdFilter(ids=[int(v) for v in values])
            raise self.fail("an id or a bounding box (south,west,north,east) expected", pos)

        w = self.word()
        if w == "bbox":
            self.expect(")")
            return GlobalBboxFilter()
        if w == "id":
            self.expect(":")
            ids = [self.integer()]
            while self.accept(","):
                ids.append(self.integer())
            self.expect(")")
            return IdFilter(ids=ids)
        if w == "around":
            input_set = self.optional_input() if self.peek(".") else None
            self.expect(":")
            radius = self.number()
            points = []
            while self.accept(","):
                lat, lon = self.number(), None
                self.expect(",")
                lon = self.number()
                points.append((lat, lon))
            self.expect(")")
            if points and input_set is not None:
                raise self.fail("around takes either an input set or coordinates, not both", pos)
            if not points and input_set is None:
                input_set = DEFAULT_SET
            return AroundFilter(radius=radius, input_set=input_set, points=points)
        if w == "poly":
            self.expect(":")
            raw = self.string()
            self.expect(")")
            try:
                nums = [float(v) for v in raw.split()]
            except ValueError:
                raise self.fail("poly expects \"lat lon lat lon ...\"", pos) from None
            if len(nums) < 6 or len(nums) % 2:
                raise self.fail("poly needs at least three lat/lon pairs", pos)
            return PolyFilter(points=list(zip(nums[0::2], nums[1::2])))
        if w == "area":
            if self.accept(":"):
                area_id = self.integer()
                self.expect(")")
                return AreaFilter(area_id=area_id)
            input_set = self.optional_input()
            self.expect(")")
            return AreaFilter(input_set=input_set)
        if w == "pivot":
            input_set = self.optional_input()
            self.expect(")")
            return PivotFilter(input_set=input_set)
        if w == "if":
            self.expect(":")
            expr = self.expr()
            self.expect(")")
            return IfFilter(expr=expr)
        if w in RECURSE_FILTERS:
            input_set = self.optional_input()
            role = None
            if self.accept(":"):
                role = self.token()
            self.expect(")")
            if element_type == "area":
                raise self.fail(f"({w}) does not apply to areas", pos)
            return RecurseFilter(kind=w, input_set=input_set, role=role)
        if w in UNSUPPORTED_FILTERS:
            raise self.unsupported(f"({w}:) is not supported: {UNSUPPORTED_FILTERS[w]}", pos)
        raise self.fail(f'unknown filter "({w}"', pos)

    # --- if: expressions ----------------------------------------------------

    def expr(self) -> Expr:
        cond = self.binary(0)
        if self.accept("?"):
            then = self.expr()
            self.expect(":")
            otherwise = self.expr()
            return Ternary(cond=cond, then=then, otherwise=otherwise)
        return cond

    # Lowest precedence first. Two-character operators come before their
    # one-character prefixes so that "<=" is not read as "<" followed by "=".
    _LEVELS = (("||",), ("&&",), ("==", "!="), ("<=", ">=", "<", ">"), ("+", "-"), ("*", "/"))

    def binary(self, level: int) -> Expr:
        if level == len(self._LEVELS):
            return self.unary()
        left = self.binary(level + 1)
        while True:
            for op in self._LEVELS[level]:
                if self.accept(op):
                    left = Binary(op=op, left=left, right=self.binary(level + 1))
                    break
            else:
                return left

    def unary(self) -> Expr:
        if self.peek("!") and not self.peek("!="):
            self.pos += 1
            return Unary(op="!", operand=self.unary())
        if self.peek("-") and not self.peek_number():
            self.pos += 1
            return Unary(op="-", operand=self.unary())
        return self.primary()

    def primary(self) -> Expr:
        pos = self.pos
        if self.accept("("):
            inner = self.expr()
            self.expect(")")
            return inner
        self.skip()
        if self.text[self.pos : self.pos + 1] in ('"', "'"):
            return Literal(self.string())
        if self.peek_number():
            self.skip()
            m = _NUMBER.match(self.text, self.pos)
            self.pos = m.end()
            return Literal(m.group(0))
        name = self.word()
        if name == "t" and self.accept("["):
            key = self.token()
            self.expect("]")
            return TagValue(key=key)
        if name == "count":
            # An aggregate over the set _, not a function of the element:
            # count(ways) in "node._(if:count(ways) == 0)" counts the ways in _.
            self.expect("(")
            what = self.word()
            if what not in COUNT_TYPES:
                raise QLError(self.line(pos), "static", f'count() takes one of {", ".join(COUNT_TYPES)}, not "{what}"')
            self.expect(")")
            return Call(name="count", args=[Literal(what)])
        if name not in FUNCTIONS:
            raise QLError(self.line(pos), "static", f'unknown function "{name}"')
        self.expect("(")
        args = []
        if not self.accept(")"):
            args.append(self.expr())
            while self.accept(","):
                args.append(self.expr())
            self.expect(")")
        if len(args) != FUNCTIONS[name]:
            raise QLError(self.line(pos), "static", f"{name}() takes {FUNCTIONS[name]} argument(s), got {len(args)}")
        return Call(name=name, args=args)
