"""Comment stripping for the JavaScript the webapp serves.

The sources carry long explanatory comments that the phone downloads, parses and
never reads: they are about 45% of the gzipped bytes of the ES-module graph a
cold launch fetches. The files on disk stay as they are; the copy sent over the
wire drops the comments and the lines they leave empty.

Only comments go. Strings, template literals and regex literals are copied
verbatim, ``/*! ... */`` banners (licences) stay, and a comment that spanned a
line break leaves one, so automatic semicolon insertion reads the same. A
source whose comment or template never ends comes back unchanged: a failed trim
costs bytes, never behaviour.

Regex literals are told apart from division by the previous token, the usual
heuristic. A mistake there can only keep a comment or, for a regex the scanner
read as division, read the regex body as code; a quote inside it then ends at
the line break, so the damage stays on that line.
"""

from __future__ import annotations

from typing import List

_REGEX_AFTER_PUNCT = frozenset("(,=:[!&|?{};+-*%<>~^/")
_REGEX_AFTER_WORDS = frozenset({
    "return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
    "throw", "case", "do", "else", "yield", "await",
})


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch in "_$" or ord(ch) > 127


class _Unterminated(Exception):
    """The scanner reached the end of the source inside a comment or template."""


class _Scanner:
    def __init__(self, source: str) -> None:
        self.s = source
        self.n = len(source)
        self.i = 0
        self.out: List[str] = []

    # -- output helpers -------------------------------------------------

    def _at_line_start(self) -> bool:
        return not self.out or self.out[-1] == "\n"

    def _drop_trailing_blanks(self) -> None:
        while self.out and self.out[-1] in (" ", "\t"):
            self.out.pop()

    def _rest_of_line_blank(self, pos: int) -> int:
        """Index just past this line's newline when only blanks follow ``pos``, else -1."""
        j = pos
        while j < self.n and self.s[j] in " \t\r":
            j += 1
        if j >= self.n:
            return j
        return j + 1 if self.s[j] == "\n" else -1

    # -- comments -------------------------------------------------------

    def _line_comment(self) -> None:
        end = self.s.find("\n", self.i)
        end = self.n if end < 0 else end
        self._drop_trailing_blanks()
        self.i = end
        if self._at_line_start() and self.i < self.n:
            self.i += 1  # the comment was the whole line: drop its newline too

    def _block_comment(self) -> None:
        end = self.s.find("*/", self.i + 2)
        if end < 0:
            raise _Unterminated
        text = self.s[self.i:end + 2]
        if text.startswith("/*!"):
            self.out.append(text)
            self.i = end + 2
            return
        self._drop_trailing_blanks()
        after = self._rest_of_line_blank(end + 2)
        if self._at_line_start() and after >= 0:
            self.i = after
            return
        self.i = end + 2
        if "\n" in text:
            self.out.append("\n")
        elif self.out and self.i < self.n and not self.out[-1].isspace() and not self.s[self.i].isspace():
            self.out.append(" ")

    # -- literals -------------------------------------------------------

    def _string(self, quote: str) -> None:
        s, n = self.s, self.n
        self.out.append(quote)
        self.i += 1
        while self.i < n:
            c = s[self.i]
            if c == "\\":
                self.out.append(s[self.i:self.i + 2])
                self.i += 2
                continue
            if c == "\n":
                return  # a string never spans a line: recover here
            self.out.append(c)
            self.i += 1
            if c == quote:
                return

    def _template(self) -> None:
        s, n = self.s, self.n
        self.out.append("`")
        self.i += 1
        while self.i < n:
            c = s[self.i]
            if c == "\\":
                self.out.append(s[self.i:self.i + 2])
                self.i += 2
            elif c == "`":
                self.out.append(c)
                self.i += 1
                return
            elif c == "$" and s[self.i + 1:self.i + 2] == "{":
                self.out.append("${")
                self.i += 2
                self._code(nested=True)
                if self.i >= n:
                    raise _Unterminated
                self.out.append("}")
                self.i += 1
            else:
                self.out.append(c)
                self.i += 1
        raise _Unterminated

    def _regex(self) -> None:
        s, n = self.s, self.n
        in_class = False
        self.out.append("/")
        self.i += 1
        while self.i < n:
            c = s[self.i]
            if c == "\\":
                self.out.append(s[self.i:self.i + 2])
                self.i += 2
                continue
            if c == "\n":
                return
            self.out.append(c)
            self.i += 1
            if c == "[":
                in_class = True
            elif c == "]":
                in_class = False
            elif c == "/" and not in_class:
                return

    # -- code -----------------------------------------------------------

    def _regex_allowed(self, prev: str) -> bool:
        return prev == "" or prev in _REGEX_AFTER_PUNCT or prev in _REGEX_AFTER_WORDS

    def _code(self, *, nested: bool) -> None:
        """Copy code up to the end, or (nested) up to the ``}`` closing a template ``${``."""
        s, n = self.s, self.n
        depth = 0
        prev = ""
        while self.i < n:
            c = s[self.i]
            if c in "'\"":
                self._string(c)
                prev = "s"
            elif c == "`":
                self._template()
                prev = "s"
            elif c == "/":
                nxt = s[self.i + 1:self.i + 2]
                if nxt == "/":
                    self._line_comment()
                elif nxt == "*":
                    self._block_comment()
                elif self._regex_allowed(prev):
                    self._regex()
                    prev = "s"
                else:
                    self.out.append("/")
                    self.i += 1
                    prev = "/"
            elif c.isspace():
                self.out.append(c)
                self.i += 1
            elif _is_word_char(c):
                j = self.i
                while j < n and _is_word_char(s[j]):
                    j += 1
                prev = s[self.i:j]
                self.out.append(prev)
                self.i = j
            else:
                if c == "{":
                    depth += 1
                elif c == "}":
                    if nested and depth == 0:
                        return
                    depth -= 1
                self.out.append(c)
                self.i += 1
                prev = c
        if nested:
            raise _Unterminated


def trim_js(source: str) -> str:
    """``source`` without its comments; ``source`` itself when it cannot be scanned to the end."""
    if "/" not in source:
        return source
    scanner = _Scanner(source)
    try:
        scanner._code(nested=False)
    except _Unterminated:
        return source
    return "".join(scanner.out)
