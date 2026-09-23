"""Visible page text carries no glyph characters standing in for icons (#1175).

The design review's COMP-01 flags arrow and geometric-shape characters in UI
text: design.md's icons are the Lucide sprite, so a `⋮` or `→` in a sentence
is an icon drawn in whatever font the device has. This scans index.html's
rendered text (comments, scripts and styles excluded — those never paint)
for the same character class the review measures with.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

_INDEX = Path(__file__).resolve().parents[1] / "app" / "webapp" / "static" / "index.html"

# Mirrors design_review/measure.py's GLYPH_ICON_RE (fleet-config): arrows,
# geometric shapes, misc symbols-and-arrows, and the ✖ ✕ × ⋮ ☰ stand-ins.
_GLYPH_RE = re.compile(r"[←-⇿■-◿⬀-⯿✖✕×⋮☰]")


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self.hits: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "template"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "template") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and _GLYPH_RE.search(data):
            self.hits.append(data.strip()[:60])


def test_index_text_has_no_glyph_icons() -> None:
    parser = _VisibleText()
    parser.feed(_INDEX.read_text(encoding="utf-8"))
    assert not parser.hits, (
        "glyph characters used as icons in page text; use the Lucide sprite "
        "(<svg class=\"icon\"><use href=\"#i-…\"></use></svg>) or words: "
        + " | ".join(parser.hits)
    )
