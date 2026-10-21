"""Fields, the composer and a disabled in-card button share one fill (#1525).

Dark's ``--card-off`` is true black (``#010409``), so a text field or a disabled
button painted with it reads as a black well inside a card, next to a composer
painted ``--card-2``. Owner decision fleet-config#1368: one control fill,
``--input-bg`` (light ``canvas-subtle``, dark ``border-muted``). This guard
fails when one of those controls goes back to a literal or to ``--card-off``,
or when dark's ``--input-bg`` drifts back to a true-black fill.
"""

from __future__ import annotations

import pathlib
import re

import pytest

STATIC = pathlib.Path(__file__).resolve().parents[1] / "app" / "webapp" / "static"

# Selectors whose background is the control fill. ``--card-off`` stays for
# off-card wells (code blocks, the segmented track), so it is not listed.
_CONTROLS = [
    ".tr-ask-input",
    ".compose-input",
    ".input-native",
    ".login-card input",
    '.session-mode-btn[aria-disabled="true"]',
    ".key-btn:disabled",
    '.board-drawer-action[aria-disabled="true"],\n.board-drawer-action[aria-disabled="true"]:active',
]


def _css() -> str:
    css = (STATIC / "styles.css").read_text(encoding="utf-8").replace("\r\n", "\n")
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _background_of(selector: str) -> str:
    for sel_text, body in re.findall(r"([^{}]+)\{([^{}]*)\}", _css()):
        if sel_text.strip() == selector:
            m = re.search(r"(?<![\w-])background:\s*([^;]+);", body)
            assert m, f"{selector} sets no background"
            return m.group(1).strip()
    raise AssertionError(f"no rule for {selector!r} in styles.css")


@pytest.mark.parametrize("selector", _CONTROLS)
def test_control_paints_the_shared_fill(selector: str) -> None:
    assert _background_of(selector) == "var(--input-bg)"


def _dark_token(name: str) -> str:
    """``name`` in the dark block, followed through ``var()`` to its value."""
    dark = re.search(r'\[data-theme="dark"\]\s*\{([^{}]*)\}', _css())
    assert dark, "dark token block not found"
    tokens = dict(re.findall(r"(--[\w-]+):\s*([^;]+);", dark.group(1)))
    value = tokens.get(name)
    assert value, f"dark block does not define {name}"
    while (ref := re.fullmatch(r"var\((--[\w-]+)\)", value.strip())):
        assert ref.group(1) in tokens, f"{ref.group(1)} is not defined in the dark block"
        value = tokens[ref.group(1)]
    return value.strip().lower()


def test_dark_control_fill_is_not_true_black() -> None:
    # Resolved, not spelled: a palette change moves all of these together.
    fill = _dark_token("--input-bg")
    assert fill != _dark_token("--card-off"), "dark controls are back on the black well"
    assert fill == _dark_token("--card-2"), "fields and the composer no longer share a fill"


def test_light_control_fill_is_unchanged() -> None:
    root = re.search(r":root\s*\{(.*?)\n\}", _css(), flags=re.S)
    assert root, ":root block not found"
    assert re.search(r"--input-bg:\s*var\(--card-off\);", root.group(1))
