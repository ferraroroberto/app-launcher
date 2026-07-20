"""Coverage check: every icon() call / #i-NAME reference must resolve to a
<symbol> defined in the vendored Lucide sprite (#565).

45 icon() calls silently rendered nothing because the vendored sprite never
got the matching symbols, and no test caught the drift. This is the
regression net the issue asked for.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_STATIC_DIR = _REPO_ROOT / "app" / "webapp" / "static"
_SPRITE_PATH = _STATIC_DIR / "_vendored" / "icons" / "icons-sprite.html"

_ICON_REFERENCE_RE = re.compile(r"icon\(\s*['\"]([\w-]+)['\"]|#i-([\w-]+)")
_SYMBOL_ID_RE = re.compile(r'<symbol\s+id="i-([\w-]+)"')

# icons.js's doc comment and the sprite's own header comment both use
# "#i-NAME" / "icon('NAME')" as literal placeholder examples, not real
# references — the component's own files are excluded from the scan.
_EXCLUDED_FILES = {
    _STATIC_DIR / "_vendored" / "icons" / "icons.js",
    _SPRITE_PATH,
}


def _referenced_icon_names() -> set[str]:
    names: set[str] = set()
    for path in list(_STATIC_DIR.rglob("*.js")) + list(_STATIC_DIR.rglob("*.html")):
        if path in _EXCLUDED_FILES:
            continue
        text = path.read_text(encoding="utf-8")
        for call_name, href_name in _ICON_REFERENCE_RE.findall(text):
            names.add(call_name or href_name)
    return names


def _defined_symbol_ids() -> set[str]:
    text = _SPRITE_PATH.read_text(encoding="utf-8")
    return set(_SYMBOL_ID_RE.findall(text))


def test_every_icon_reference_resolves_to_a_sprite_symbol():
    referenced = _referenced_icon_names()
    defined = _defined_symbol_ids()
    missing = sorted(referenced - defined)
    assert not missing, (
        f"{len(missing)} icon()/#i-NAME reference(s) have no matching "
        f"<symbol> in {_SPRITE_PATH.relative_to(_REPO_ROOT)}: {', '.join(missing)}"
    )
