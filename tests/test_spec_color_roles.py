"""The fleet spec's text-on-tint, control and type roles are defined and used (#1156).

fleet-config#963 split every hue that is both a fill and a text color into two
tokens: a base hue fails AA as text on its own 16% tint, so text on a tint takes
the matching ``*-text`` role. The same change added ``control-border`` (the
input boundary, 3:1 against the card) and ``neutral-soft`` (the chip fill), and
#992 added ``body-sm`` for secondary lines. The vendored components reference
these names, so a theme that doesn't define one hands those components an
unset variable.

This file pins three things in ``styles.css``:

- each role is defined in both themes at the spec value;
- no rule sets a base hue as text over that hue's own tint;
- no text is uppercased outside the ``overline`` role.

The spec values are copied here on purpose. CI runs on a machine without
``~/.claude/design.md``, and a spec change should show up as a failing diff.
"""

from __future__ import annotations

import pathlib
import re

STYLES = pathlib.Path(__file__).resolve().parents[1] / "app" / "webapp" / "static" / "styles.css"

# design.md / design.dark.md colors (fleet-config#963) and typography.body-sm (#992).
_LIGHT = {
    "--accent-fill": "#0969da",
    "--accent-text": "#0550ae",
    "--success-text": "#116329",
    "--danger-text": "#a40e26",
    "--attention-text": "#7d4e00",
    "--control-border": "#818b98",
    "--neutral-soft": "color-mix(in srgb, var(--muted) 16%, transparent)",
    "--font-body-sm": "0.875rem",
}
_DARK = {
    "--accent-fill": "#1f6feb",
    "--accent-text": "#58a6ff",
    "--success-text": "#56d364",
    "--danger-text": "#ff7b72",
    "--attention-text": "#e3b341",
    "--control-border": "#6e7681",
}

_HUES = ("accent", "success", "danger", "attention")


def _css() -> str:
    return re.sub(r"/\*.*?\*/", "", STYLES.read_text(encoding="utf-8"), flags=re.S)


def _block(selector: str) -> str:
    """The body of the first top-level rule whose selector is exactly ``selector``."""
    m = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{([^{}]*)\}", _css())
    assert m, f"no top-level {selector} rule in styles.css"
    return m.group(1)


def _decls(body: str) -> dict:
    return {k.strip(): v.strip() for k, v in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body)}


def test_light_theme_defines_the_spec_roles() -> None:
    got = _decls(_block(":root"))
    wrong = {k: got.get(k) for k, v in _LIGHT.items() if got.get(k) != v}
    assert not wrong, f":root role values off the spec (got): {wrong}"


def test_dark_theme_defines_the_spec_roles() -> None:
    got = _decls(_block('[data-theme="dark"]'))
    wrong = {k: got.get(k) for k, v in _DARK.items() if got.get(k) != v}
    assert not wrong, f'[data-theme="dark"] role values off the spec (got): {wrong}'


def test_no_base_hue_text_on_its_own_tint() -> None:
    offenders = []
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", _css()):
        color = re.search(r"(?<![-\w])color\s*:\s*var\(--(\w+)\)", body)
        bg = re.search(r"background(?:-color)?\s*:\s*([^;]+);", body)
        if not (color and bg) or color.group(1) not in _HUES:
            continue
        hue, fill = color.group(1), bg.group(1)
        on_own_tint = f"from var(--{hue})" in fill or (hue == "accent" and "var(--accent-soft)" in fill)
        if on_own_tint:
            offenders.append(f"{sel.strip().splitlines()[-1]} (color: var(--{hue}) on {fill.strip()})")
    assert not offenders, "base hue as text on its own tint; use the *-text role: " + "; ".join(offenders)


def test_caps_only_through_the_overline_role() -> None:
    stray = [
        sel.strip().splitlines()[-1]
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", _css())
        if re.search(r"text-transform\s*:\s*uppercase", body) and "overline" not in sel
    ]
    assert not stray, f"uppercase outside the overline role (design.md Typography): {stray}"
