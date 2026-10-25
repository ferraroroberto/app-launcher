"""The app asks through the vendored dialogs, never the browser's.

A native ``confirm()`` / ``prompt()`` is the unstyled system sheet the modal
contract (#545) replaced. The Life tab went first (#1439); #1501 moved the
last callers (session Stop, chief Restart, passkeys) to ``confirmDialog`` /
``promptDialog``, so every first-party module is checked now, outside the
vendored components and the terminal modules. Comments may still name the
old calls, so they are stripped before the search.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_STATIC = Path(__file__).resolve().parents[1] / "app" / "webapp" / "static"
_COMMENTS = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
_NATIVE = re.compile(r"(?<![\w.$])(?:window\.)?(confirm|prompt|alert)\s*\(")


# First-party modules only, the tab subfolders (board/, jobs/) included.
_MODULES = sorted(
    p.relative_to(_STATIC).as_posix() for p in _STATIC.rglob("*.js")
    if not p.name.startswith("terminal") and not {"_vendored", "vendor"} & set(p.relative_to(_STATIC).parts)
)


def test_the_module_list_is_real() -> None:
    assert "life-os.js" in _MODULES and "board/drawer.js" in _MODULES, _MODULES


@pytest.mark.parametrize("module", _MODULES)
def test_no_native_dialog_is_left(module: str) -> None:
    code = _COMMENTS.sub("", (_STATIC / module).read_text(encoding="utf-8"))
    found = [m.group(0) for m in _NATIVE.finditer(code)]
    assert not found, f"{module} still calls a native dialog: {found}"


def test_the_search_finds_a_native_call() -> None:
    """The regex itself, so a pass above means something."""
    assert _NATIVE.search("if (!confirm('x')) return;")
    assert _NATIVE.search("const v = window.prompt('x', '');")
    assert not _NATIVE.search("await confirmDialog({ title: 'x' });")
    assert not _NATIVE.search("els.lifeOsRenameDialog.showModal();")
