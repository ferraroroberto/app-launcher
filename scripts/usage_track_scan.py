"""The HTML and JS readers of the usage track ratchet: controls, dialogs and ids, per file.

`usage_track_ratchet.py` owns the configuration, the scope, git, the baseline and
the exit codes; this sibling only reads one file's text and reports raw hits as
`(line, rule, message)`. HTML is read with `html.parser` (inline scripts handed to
the JS reader on their own lines); JS with a small tokenizer that keeps strings,
template literals (their holes tokenized in place), comments and regex literals
out of the brace stack, so a `createElement` tie can be held to its block. JS
that does not close raises `NotConfirmed`.

Vendor-verbatim beside its ratchet (manifest key `usage_track_scan`), stdlib-only.
Rules and limits: docs/usage-track-ratchet.md.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser

HTML_SUFFIXES = (".html", ".htm", ".jinja", ".jinja2", ".j2")
JS_SUFFIXES = (".js", ".mjs")

#: Mirrors usage-log.js ID_PATTERN: bounded, no whitespace, no "/".
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")

CONTROL_TAGS = frozenset({"button", "summary", "select"})
CONTROL_INPUT_TYPES = frozenset({"button", "submit", "reset", "checkbox", "radio", "image"})
CONTROL_ROLES = frozenset({
    "button", "tab", "menuitem", "menuitemcheckbox", "menuitemradio", "switch", "checkbox", "radio", "option",
})
#: A tracked parent with one of these roles counts its children's clicks (the client's `closest('[data-track]')`).
GROUP_ROLES = frozenset({"group", "radiogroup", "tablist", "toolbar", "menu", "menubar", "listbox"})
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr",
})
_JS_SCRIPT_TYPES = frozenset({"", "module", "text/javascript", "application/javascript"})

#: A string literal worth reading as markup: it opens a tag.
_MARKUP = re.compile(r"<[A-Za-z]")
#: Where a hole of a template literal sits in the markup read from it: it fails the id pattern.
_HOLE = "${}"


class ConfigError(RuntimeError):
    """The `[usage_track_ratchet]` table or the baseline file is unusable."""


class NotConfirmed(RuntimeError):
    """A file in scope could not be read or tokenized, or git could not list the tree."""


def is_constant_id(value: str) -> bool:
    return ID_PATTERN.match(value) is not None


# ---------------------------------------------------------------- markers

def _comment_line(text: str) -> bool:
    """A line that opens a comment, or ends one it did not open (`... -->`): code with a trailing comment is not."""
    text = text.strip()
    if text.startswith(("//", "/*", "*", "<!--")):
        return True
    return text.endswith(("-->", "*/")) and "<!--" not in text and "/*" not in text


def _window(lines: list[str], line: int, end: int) -> list[str]:
    """The hit's own lines plus the contiguous comment block right above it."""
    above = line - 1
    while above > 0 and _comment_line(lines[above - 1]):
        above -= 1
    return lines[above:end]


def _exempt(rule: str, window: list[str]) -> bool:
    """A `<rule>-exempt: <reason>` marker in the window; a bare marker exempts nothing."""
    marker = f"{rule}-exempt"
    return any(line.split(marker, 1)[1].strip(" :/*-<>!\t") for line in window if marker in line)


# -------------------------------------------------------------------- HTML

@dataclass
class _El:
    tag: str
    tracked: bool       # carries a `data-track` attribute
    counts_children: bool  # a control or a grouping role: its id covers clicks inside it


@dataclass
class _Found:
    """What one scan saw: raw hits as (line, end line, rule, message), and whether `initUsageLog(` is called."""

    hits: list[tuple[int, int, str, str]] = field(default_factory=list)
    inits: bool = False

    def add(self, line: int, rule: str, message: str, end: int | None = None) -> None:
        self.hits.append((line, end if end is not None else line, rule, message))


def is_control(tag: str, attrs: dict[str, str]) -> bool:
    if attrs.get("role", "").strip().lower() in CONTROL_ROLES or tag in CONTROL_TAGS:
        return True
    if tag == "input":
        return attrs.get("type", "text").strip().lower() in CONTROL_INPUT_TYPES
    href = attrs.get("href")
    return tag == "a" and href is not None and href.strip().lower().startswith(("#", "javascript:"))


class _HtmlScanner(HTMLParser):
    """Controls, dialogs and ids in one HTML document or fragment; inline scripts are handed to `on_script`."""

    def __init__(self, found: _Found, line_offset: int, on_script: Callable[[str, int], None] | None) -> None:
        super().__init__(convert_charrefs=True)
        self.found = found
        self.line_base = line_offset  # not `offset`: HTMLParser keeps its column there
        self.on_script = on_script
        self.stack: list[_El] = []
        self.script: list[str] | None = None  # chunks of the open inline JS script
        self.script_line = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, {k: v or "" for k, v in attrs}, void=tag in _VOID_TAGS)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag, {k: v or "" for k, v in attrs}, void=True)

    def _start(self, tag: str, attrs: dict[str, str], *, void: bool) -> None:
        line = self.getpos()[0] + self.line_base
        end = line + (self.get_starttag_text() or "").count("\n")
        for name in ("data-track", "data-track-view"):
            if name in attrs and not is_constant_id(attrs[name]):
                self.found.add(line, "dynamic-id", f"<{tag} {name}={attrs[name]!r}> is not a constant id", end)
        control = is_control(tag, attrs)
        if control and not self._tracked(attrs):
            self.found.add(line, "untracked-control", f"<{tag}> has no data-track and no tracked parent", end)
        if tag == "dialog" and "data-track-view" not in attrs:
            self.found.add(line, "untracked-sheet", "<dialog> has no data-track-view", end)
        if tag == "script" and not void and self.on_script is not None:
            kind = attrs.get("type", "").strip().lower()
            self.script = [] if "src" not in attrs and kind in _JS_SCRIPT_TYPES else None
            self.script_line = end
        if not void:
            grouping = attrs.get("role", "").strip().lower() in GROUP_ROLES
            self.stack.append(_El(tag, "data-track" in attrs, control or grouping))

    def _tracked(self, attrs: dict[str, str]) -> bool:
        if "data-track" in attrs:
            return True
        if "data-tab" in attrs and any(el.tag == "nav" for el in self.stack):
            return True  # the nav's onChange records the tab as a view
        for el in reversed(self.stack):
            if el.tracked:  # the id the client's closest('[data-track]') resolves
                return el.counts_children
        return False

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.script is not None and self.on_script is not None:
            self.on_script("".join(self.script), self.script_line)
            self.script = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        if self.script is not None:
            self.script.append(data)


def _scan_markup(text: str, found: _Found, line_offset: int,
                 on_script: Callable[[str, int], None] | None = None) -> None:
    parser = _HtmlScanner(found, line_offset, on_script)
    parser.feed(text)
    parser.close()


# ---------------------------------------------------------------------- JS

_JS_STRING = re.compile(r""""(?:[^"\\\n]|\\[\s\S])*"|'(?:[^'\\\n]|\\[\s\S])*'""")
_JS_TOKEN = re.compile(
    r"""(?P<space>[^\S\n]+)
      | (?P<newline>\n)
      | (?P<comment>//[^\n]*)
      | (?P<string>"(?:[^"\\\n]|\\[\s\S])*"|'(?:[^'\\\n]|\\[\s\S])*')
      | (?P<name>[A-Za-z_$\u0080-￿][\w$\u0080-￿]*)
      | (?P<number>\.?\d[\w.]*)
      | (?P<op>===|!==|==|!=|<=|>=|=>|&&=|\|\|=|\?\?=|&&|\|\||\?\?|\?\.|\+\+|--|\+=|-=|\*=|\.\.\.)
      | (?P<punct>[\s\S])""",
    re.VERBOSE,
)
_JS_TEMPLATE_TEXT = re.compile(r"(?:[^`\\$]|\\[\s\S]|\$(?!\{))*")
_JS_REGEX = re.compile(r"/(?![*/])(?:[^/\\\[\n]|\\.|\[(?:[^\]\\\n]|\\.)*\])+/[A-Za-z]*")
#: After these words a `/` opens a regex literal, not a division.
_JS_REGEX_AFTER_WORD = frozenset(
    {"return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case", "do", "else",
     "yield", "await"}
)
_ESCAPE = re.compile(r"\\([\s\S])")


def _unescape(raw: str) -> str:
    """A literal's text with each backslash escape reduced to its character: `\\"` reads as `"` in markup."""
    return _ESCAPE.sub(r"\1", raw)


@dataclass
class Tok:
    kind: str   # "name", "str" (string literal), "tpl" (template literal), "num", "regex", "op" or "punct"
    text: str   # a name or punctuation; a string's or template's content (holes as `${}`)
    line: int
    block: int = -1  # index of the innermost open `{`, -1 at module level
    holes: int = 0   # a template literal's `${...}` count


class _Lexer:
    """Tokens of one JS source, with each `{` matched to its `}`.

    Strings, template literals (their `${}` code tokenized in place), comments
    and regex literals never move the brace stack. Anything that does not close
    -- a string, a template, a comment, a brace -- raises `NotConfirmed`: the
    scope of every later token would be a guess.
    """

    def __init__(self, text: str, path: str, line: int) -> None:
        self.text, self.path, self.line = text, path, line
        self.toks: list[Tok] = []
        self.braces: list[int] = []      # open `{` token indexes
        self.closes: dict[int, int] = {}  # `{` index -> `}` index

    def lex(self) -> list[Tok]:
        self._code(0, in_hole=False)
        if self.braces:
            raise NotConfirmed(f"{self.path}:{self.toks[self.braces[-1]].line}: a `{{` that never closes")
        return self.toks

    def _fail(self, what: str) -> NotConfirmed:
        return NotConfirmed(f"{self.path}:{self.line}: {what}")

    def _code(self, pos: int, *, in_hole: bool) -> int:
        """Tokens from `pos` to the end, or (in a template hole) to the `}` that closes it."""
        text, depth = self.text, len(self.braces)
        while pos < len(text):
            char = text[pos]
            if char == "`":
                pos = self._template(pos + 1)
                continue
            if char == "}" and in_hole and len(self.braces) == depth:
                return pos + 1
            if char == "/" and text.startswith("/*", pos):
                end = text.find("*/", pos + 2)
                if end < 0:
                    raise self._fail("a `/*` comment that never closes")
                self.line += text.count("\n", pos, end)
                pos = end + 2
                continue
            if char == "/" and not text.startswith("//", pos) and self._regex_allowed():
                regex = _JS_REGEX.match(text, pos)
                if regex:
                    self._push("regex", regex.group())
                    pos = regex.end()
                    continue
            if char in "\"'" and not _JS_STRING.match(text, pos):
                raise self._fail("a string that never closes")
            pos = self._token(pos)
        if in_hole:
            raise self._fail("a template `${` that never closes")
        return pos

    def _token(self, pos: int) -> int:
        m = _JS_TOKEN.match(self.text, pos)
        assert m is not None  # the last alternative matches any character
        kind, value = m.lastgroup or "punct", m.group()
        if kind == "string":
            self._push("str", _unescape(value[1:-1]))
        elif kind in ("name", "number", "op"):
            self._push("num" if kind == "number" else kind, value)
        elif kind == "punct":
            self._punct(value)
        self.line += value.count("\n")
        return m.end()

    def _punct(self, value: str) -> None:
        if value == "{":
            self._push("punct", value)
            self.braces.append(len(self.toks) - 1)
            return
        if value == "}":
            if not self.braces:
                raise self._fail("a `}` with no `{`")
            self.closes[self.braces.pop()] = len(self.toks)
        self._push("punct", value)

    def _template(self, pos: int) -> int:
        """A template literal from just after its backtick; its holes' code is tokenized after it."""
        index, line = len(self.toks), self.line
        self.toks.append(Tok("tpl", "", line, self._block()))
        parts: list[str] = []
        holes = 0
        while True:
            m = _JS_TEMPLATE_TEXT.match(self.text, pos)
            assert m is not None  # the pattern matches the empty string
            parts.append(_unescape(m.group()))
            self.line += m.group().count("\n")
            pos = m.end()
            if pos >= len(self.text):
                raise NotConfirmed(f"{self.path}:{line}: a template literal that never closes")
            if self.text[pos] == "`":
                break
            holes += 1
            parts.append(_HOLE)
            pos = self._code(pos + 2, in_hole=True)
        self.toks[index] = Tok("tpl", "".join(parts), line, self.toks[index].block, holes)
        return pos + 1

    def _block(self) -> int:
        return self.braces[-1] if self.braces else -1

    def _push(self, kind: str, text: str) -> None:
        self.toks.append(Tok(kind, text, self.line, self._block()))

    def _regex_allowed(self) -> bool:
        if not self.toks:
            return True
        prev = self.toks[-1]
        if prev.kind == "name":
            return prev.text in _JS_REGEX_AFTER_WORD
        return prev.kind in ("punct", "op") and prev.text not in (")", "]", "++", "--")


#: `el.dataset.<key> = ...` -> the attribute it sets.
_DATASET_ATTRS = {"track": "data-track", "trackView": "data-track-view"}


def _literal(tok: Tok) -> str | None:
    """The text of a string literal, or of a template literal with no holes; else None."""
    if tok.kind == "str" or (tok.kind == "tpl" and not tok.holes):
        return tok.text
    return None


class _JsScanner:
    """The JS rules over one token list; `closes` matches each `{` to its `}`."""

    def __init__(self, toks: list[Tok], closes: dict[int, int], found: _Found,
                 builders: frozenset[str], builder_bodies: list[tuple[int, int]]) -> None:
        self.toks, self.closes, self.found = toks, closes, found
        self.builders = builders
        self.builder_bodies = builder_bodies  # token ranges of a declared builder's own body
        self.consts = self._module_consts()

    def _at(self, i: int, kind: str, text: str | None = None) -> bool:
        if not 0 <= i < len(self.toks):
            return False
        tok = self.toks[i]
        return tok.kind == kind and (text is None or tok.text == text)

    def _module_consts(self) -> frozenset[str]:
        """Names bound at module level by `const NAME = '<id>'` (and nothing more)."""
        names = set()
        for i, tok in enumerate(self.toks):
            if (tok.kind == "name" and tok.text == "const" and tok.block == -1 and self._at(i + 1, "name")
                    and self._at(i + 2, "punct", "=") and len(self._expr(i + 3)) == 1):
                value = _literal(self.toks[i + 3])
                if value is not None and is_constant_id(value):
                    names.add(self.toks[i + 1].text)
        return frozenset(names)

    def _expr(self, i: int) -> list[Tok]:
        """The tokens of the expression starting at `i`: up to `;`, `,` or a closer at its own depth, or a new line."""
        out: list[Tok] = []
        depth = 0
        while i < len(self.toks):
            tok = self.toks[i]
            if tok.kind == "punct" and tok.text in "([{":
                depth += 1
            elif tok.kind == "punct" and tok.text in ")]}":
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and ((tok.kind == "punct" and tok.text in ";,")
                                 or (out and tok.line > out[-1].line and tok.kind != "punct")):
                break
            out.append(tok)
            i += 1
        return out

    def _is_constant(self, expr: list[Tok]) -> bool:
        if len(expr) != 1:
            return False
        value = _literal(expr[0])
        if value is not None:
            return is_constant_id(value)
        return expr[0].kind == "name" and expr[0].text in self.consts

    def _in_builder(self, i: int) -> bool:
        return any(lo < i < hi for lo, hi in self.builder_bodies)

    def _check_id(self, at: int, value_at: int, what: str) -> None:
        if not self._in_builder(at) and not self._is_constant(self._expr(value_at)):
            self.found.add(self.toks[at].line, "dynamic-id", f"{what} is not a constant id")

    def scan(self) -> None:
        for i, tok in enumerate(self.toks):
            if tok.kind in ("str", "tpl") and _MARKUP.search(tok.text):
                _scan_markup(tok.text, self.found, tok.line - 1)
            elif tok.kind == "name":
                self._name(i, tok)

    def _name(self, i: int, tok: Tok) -> None:
        after_dot = self._at(i - 1, "punct", ".") or self._at(i - 1, "op", "?.")
        call = self._at(i + 1, "punct", "(")
        if after_dot and call and tok.text in ("track", "trackView"):
            if not self._at(i + 2, "punct", ")"):  # a usage call always passes an id; `x.track()` is another API
                self._check_id(i, i + 2, f"`.{tok.text}(...)` argument")
        elif after_dot and tok.text == "dataset" and self._at(i + 1, "punct", ".") \
                and self._at(i + 2, "name") and self.toks[i + 2].text in _DATASET_ATTRS \
                and self._at(i + 3, "punct", "="):
            self._check_id(i, i + 4, f"`dataset.{self.toks[i + 2].text} =` value")
        elif after_dot and call and tok.text == "setAttribute" and self._at(i + 2, "str") \
                and self.toks[i + 2].text in ("data-track", "data-track-view") and self._at(i + 3, "punct", ","):
            self._check_id(i, i + 4, f"`setAttribute('{self.toks[i + 2].text}', ...)` value")
        elif after_dot and call and tok.text == "createElement" and self._at(i + 2, "str") \
                and self._at(i + 3, "punct", ")"):
            self._created(i, self.toks[i + 2].text.lower())
        elif call and not after_dot and tok.text == "initUsageLog" and not self._at(i - 1, "name", "function"):
            self.found.inits = True
        elif call and not after_dot and tok.text in self.builders and not self._at(i - 1, "name", "function"):
            self._builder_call(i, tok)

    # ------------------------------------------------------- createElement

    def _bound_name(self, dot: int) -> str | None:
        """`NAME = document.createElement(...)`: NAME, from the `.` before `createElement`."""
        start = dot - 1
        while self._at(start - 1, "punct", ".") and self._at(start - 2, "name"):
            start -= 2
        if self._at(start - 1, "punct", "=") and self._at(start - 2, "name"):
            return self.toks[start - 2].text
        return None

    def _created(self, i: int, tag: str) -> None:
        if tag not in CONTROL_TAGS | {"input", "a", "dialog", "div", "span", "li"}:
            return
        name = self._bound_name(i - 1)
        props = self._props(name, i) if name else {}
        line = self.toks[i].line
        if tag == "dialog":
            if "data-track-view" not in props:
                self.found.add(line, "untracked-sheet", "createElement('dialog') gets no data-track-view")
            return
        if is_control(tag, props) and "data-track" not in props and not self._in_builder(i):
            self.found.add(line, "untracked-control", f"createElement('{tag}') gets no data-track in its block")

    def _props(self, name: str, start: int) -> dict[str, str]:
        """Attributes the code sets on `name` after `start`, in the same block (nested blocks included)."""
        block = self.toks[start].block
        end = self.closes.get(block, len(self.toks)) if block >= 0 else len(self.toks)
        props: dict[str, str] = {}
        for j in range(start + 1, end):
            if not (self._at(j, "name", name) and self._at(j + 1, "punct", ".") and self._at(j + 2, "name")):
                continue
            member = self.toks[j + 2].text
            if member == "dataset" and self._at(j + 3, "punct", ".") and self._at(j + 5, "punct", "=") \
                    and self._at(j + 4, "name") and self.toks[j + 4].text in _DATASET_ATTRS:
                props[_DATASET_ATTRS[self.toks[j + 4].text]] = self._literal_at(j + 6)
            elif member == "setAttribute" and self._at(j + 3, "punct", "(") and self._at(j + 4, "str"):
                props[self.toks[j + 4].text.lower()] = self._literal_at(j + 6)
            elif member in ("type", "role", "href") and self._at(j + 3, "punct", "="):
                props[member] = self._literal_at(j + 4)
        return props

    def _literal_at(self, i: int) -> str:
        """The literal text at `i`, or "" when the value there is computed."""
        return (_literal(self.toks[i]) if i < len(self.toks) else None) or ""

    # ------------------------------------------------------------ builders

    def _builder_call(self, i: int, tok: Tok) -> None:
        """A declared builder's caller passes `track: '<id>'` in an object literal argument."""
        if self._in_builder(i):
            return
        depth, j = 0, i + 1
        while j < len(self.toks):
            t = self.toks[j]
            if t.kind == "punct" and t.text in "([{":
                depth += 1
            elif t.kind == "punct" and t.text in ")]}":
                depth -= 1
                if depth == 0:
                    break
            elif depth == 2 and (t.kind == "name" and t.text == "track" or t.kind == "str"
                                 and t.text in ("track", "data-track")) and self._at(j + 1, "punct", ":"):
                self._check_id(j, j + 2, f"`{tok.text}(...)` `track:` value")
                return
            j += 1
        self.found.add(tok.line, "untracked-control", f"call to builder `{tok.text}(...)` passes no `track:` id")


def builder_body(toks: list[Tok], closes: dict[int, int], name: str) -> tuple[int, int] | None:
    """Token range of `function NAME(...) {...}` or `NAME = (...) => {...}` / `NAME = function (...) {...}`."""
    for i, tok in enumerate(toks):
        if tok.kind != "name" or tok.text != name:
            continue
        declared = i > 0 and toks[i - 1].kind == "name" and toks[i - 1].text == "function"
        assigned = i + 1 < len(toks) and toks[i + 1].kind == "punct" and toks[i + 1].text == "="
        if not (declared or assigned):
            continue
        parens = 0  # a `{` inside the parameter list is a default value, not the body
        for j in range(i + 1, len(toks)):
            t = toks[j]
            if t.kind != "punct":
                continue
            if t.text in "([":
                parens += 1
            elif t.text in ")]":
                parens -= 1
            elif t.text == "{" and parens == 0:
                return j, closes[j]
            elif t.text == ";" and parens == 0:
                break
    return None


def _scan_js(text: str, path: str, line: int, found: _Found, builders: frozenset[str],
             own_builders: tuple[str, ...]) -> None:
    lexer = _Lexer(text, path, line)
    toks = lexer.lex()
    bodies = []
    for name in own_builders:
        body = builder_body(toks, lexer.closes, name)
        if body is None:
            raise ConfigError(f"[usage_track_ratchet] builder `{name}` is not defined in {path}")
        bodies.append(body)
    _JsScanner(toks, lexer.closes, found, builders, bodies).scan()


RawHit = tuple[int, str, str]  # (line, rule, message)


def scan_text(text: str, path: str, rules: tuple[str, ...],
              builders: tuple[tuple[str, str], ...]) -> tuple[list[RawHit], bool]:
    """(hits of `rules` in one file, not exempted, by line; does it call `initUsageLog(`).

    `builders` are the declared `(file, function name)` control builders. Raises
    `NotConfirmed` when a JS source (a file, or an inline script) does not
    tokenize, and `ConfigError` when this file is a builder's and does not define it.
    """
    found = _Found()
    names = frozenset(name for _, name in builders)
    own = tuple(name for file, name in builders if file == path)
    if path.lower().endswith(JS_SUFFIXES):
        _scan_js(text, path, 1, found, names, own)
    else:
        def on_script(js: str, line: int) -> None:
            _scan_js(js, path, line, found, names, ())
        _scan_markup(text, found, 0, on_script)
    lines = text.splitlines()
    hits = [(line, rule, message) for line, end, rule, message in found.hits
            if rule in rules and not _exempt(rule, _window(lines, line, end))]
    return sorted(hits, key=lambda h: (h[0], h[1])), found.inits
