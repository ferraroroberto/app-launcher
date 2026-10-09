"""The Life tab asks through the vendored dialogs, never the browser's (#1439).

A native ``confirm()`` / ``prompt()`` is the unstyled system sheet the modal
contract (#545) replaced: rename, delete and the handoff confirmation go
through ``confirmDialog`` and the rename dialog instead. Comments may still
name the old calls, so they are stripped before the search.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_STATIC = Path(__file__).resolve().parents[1] / "app" / "webapp" / "static"
_COMMENTS = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
_NATIVE = re.compile(r"(?<![\w.$])(?:window\.)?(confirm|prompt|alert)\s*\(")


@pytest.mark.parametrize("module", ["life-os.js", "life-os-viewer.js"])
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
