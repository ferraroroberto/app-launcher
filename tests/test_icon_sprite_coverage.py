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


# The page renders from the sprite copy inlined in index.html, not from the
# vendored file above (iOS Safari cannot <use> an external sprite; see the
# icons README). A symbol present in the vendored file but missing from the
# inline copy passes the test above and still renders nothing — which is how
# the Jobs tab's `clock` and `pin` icons went blank (#1127). The vendored
# components' own sample markup (`_vendored/**`) never renders, so it is
# excluded here.
_INDEX_PATH = _STATIC_DIR / "index.html"


def test_every_rendered_icon_reference_resolves_in_the_inline_sprite():
    referenced: set[str] = set()
    for path in list(_STATIC_DIR.rglob("*.js")) + list(_STATIC_DIR.rglob("*.html")):
        if "_vendored" in path.parts:
            continue
        for call_name, href_name in _ICON_REFERENCE_RE.findall(path.read_text(encoding="utf-8")):
            referenced.add(call_name or href_name)
    inline = set(_SYMBOL_ID_RE.findall(_INDEX_PATH.read_text(encoding="utf-8")))
    missing = sorted(referenced - inline)
    assert not missing, (
        f"{len(missing)} icon()/#i-NAME reference(s) have no <symbol> in "
        f"index.html's inline sprite, so they render blank: {', '.join(missing)}"
    )
