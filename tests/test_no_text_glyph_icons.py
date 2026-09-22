"""Guard: no arrow or geometric-shape characters as UI glyphs (#1127).

The app's icons are the vendored Lucide sprite. Text stand-ins (a refresh
arrow on the Board button, a triangle caret on the dropdowns, a filled
circle for sparkline dots, arrows on the terminal keys) rendered at a
different weight and baseline from the Lucide icons beside them.
``design_lint``'s ``icon-set`` check only looks for emoji, so nothing caught
them.

This scans what can reach the page: every string, markup text node and CSS
``content`` under ``app/webapp/static``, with comments stripped. It is static
so it also covers UI that is only shown on demand (the keys popover, toasts,
empty states), which a rendered sweep would miss. Excluded:

* ``_vendored/`` and ``vendor/``: third-party bytes, not app UI.
* ``terminal-readback.js``: its bullet/arrow characters are regexes that
  *parse* agent terminal output for read-back; they are never rendered.
"""

from __future__ import annotations

import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "app" / "webapp" / "static"
_EXCLUDED_DIRS = {"_vendored", "vendor"}
_EXCLUDED_FILES = {"terminal-readback.js"}

# Arrows, geometric shapes, supplemental arrows A/B, misc symbols-and-arrows.
_GLYPH_RE = re.compile("[←-⇿■-◿⟰-⟿⤀-⥿⬀-⯿]")

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
# A `//` comment: start of line or after whitespace, so `https://` in a
# string is left alone.
_LINE_COMMENT_RE = re.compile(r"(^|\s)//[^\n]*", re.M)


def _strip_comments(path: Path, text: str) -> str:
    if path.suffix == ".html":
        text = _HTML_COMMENT_RE.sub("", text)
    text = _BLOCK_COMMENT_RE.sub("", text)
    if path.suffix in (".js", ".html"):
        text = _LINE_COMMENT_RE.sub(r"\1", text)
    return text


def test_no_arrow_or_shape_glyphs_in_ui_code() -> None:
    offenders = []
    for path in sorted(_STATIC.rglob("*")):
        if path.suffix not in (".js", ".html", ".css"):
            continue
        rel = path.relative_to(_STATIC)
        if _EXCLUDED_DIRS & set(rel.parts) or path.name in _EXCLUDED_FILES:
            continue
        code = _strip_comments(path, path.read_text(encoding="utf-8"))
        for number, line in enumerate(code.splitlines(), 1):
            found = sorted(set(_GLYPH_RE.findall(line)))
            if found:
                offenders.append(f"{rel}: {' '.join(found)} in {line.strip()[:90]!r}")
    assert not offenders, (
        "arrow/shape characters used as UI glyphs; use a Lucide icon from the "
        "sprite, or words in copy (e.g. 'tap Refresh'):\n  " + "\n  ".join(offenders)
    )
