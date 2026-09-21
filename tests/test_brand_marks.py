"""Issue #1078: the OpenAI logo must not come back as the Codex CLI mark.

`b-codex` in the brand sprite carried simple-icons' `openai` path from #361
until simple-icons removed that icon in v16. The owner decided to replace it
with a hand-drawn glyph, as Pi and Grok already are. The removed path is
distinctive enough to grep for, so a re-vendor, a copy-paste from an old
branch or a revert brings it back with a red build instead of silently.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = REPO_ROOT / "app" / "webapp" / "static"
INDEX_HTML = STATIC_DIR / "index.html"

# The opening commands of simple-icons' removed `openai` path: long enough to
# be unique to that mark, short enough to survive a re-rounded tail.
_OPENAI_PATH_PREFIX = "M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108"


def test_no_static_file_carries_the_openai_mark() -> None:
    hits = [
        str(path.relative_to(REPO_ROOT))
        for path in STATIC_DIR.rglob("*")
        if path.is_file()
        and path.suffix in {".html", ".svg", ".js", ".css"}
        and _OPENAI_PATH_PREFIX in path.read_text(encoding="utf-8", errors="replace")
    ]
    assert hits == [], f"OpenAI mark path found in: {hits} (#1078)"


def test_codex_symbol_stays_in_the_sprite() -> None:
    # The replacement must not have left the Coding row without an icon.
    html = INDEX_HTML.read_text(encoding="utf-8")
    match = re.search(r'<symbol id="b-codex" viewBox="0 0 24 24">(.*?)</symbol>', html, re.S)
    assert match, "b-codex symbol missing or its 24x24 viewBox changed"
    assert "currentColor" in match.group(1), "b-codex must paint with currentColor"
