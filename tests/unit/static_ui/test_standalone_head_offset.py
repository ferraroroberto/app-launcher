"""The session view starts where the main tabs' header card starts (#2396).

In the installed phone app the vendored nav sets the main tabs' content start
(``.tabs ~ .app``, the height of its ``.nav-top-edge`` strip). The session
overlay is fixed outside ``.app``, so it carries that same offset itself. Desktop
engines cannot show the iOS softness this guards, so the structure is pinned:
one shared value, equal to the nav's, plain padding on the overlay, after its
base rule, with the band above the bar in the page colour.
"""

from __future__ import annotations

import re

from tests._css import STATIC, stylesheet_text

_STANDALONE_PHONE = (
    r"@media \(display-mode: standalone\) and \(pointer: coarse\) and \(max-width: 520px\) \{(.*?)\n\}"
)
_PAD = "padding-top: calc(env(safe-area-inset-top, 0px) + var(--standalone-head-offset));"


def _nav_offset_px() -> str:
    """The pixels the nav puts between the safe area and the tabs' first card."""
    nav = (STATIC / "_vendored" / "nav" / "nav-tabs.css").read_text(encoding="utf-8")
    found = re.findall(
        r"\.tabs ~ \.app \{ padding-top: calc\(env\(safe-area-inset-top, 0px\) \+ (\d+)px\); \}", nav
    )
    assert found, "the nav's installed-phone `.tabs ~ .app` offset moved; re-point this test"
    assert len(set(found)) == 1, found
    return found[0]


def test_offset_equals_the_navs_own_start_of_content() -> None:
    css = stylesheet_text()
    assert css.count(f"--standalone-head-offset: {_nav_offset_px()}px;") == 1
    assert css.count("--standalone-head-offset:") == 1


def test_overlay_takes_the_offset_after_its_own_padding() -> None:
    """The overlay's base rule sets the safe area; the standalone rule must follow it."""
    css = stylesheet_text()
    overlay_base = css.index(".terminal-overlay {")
    matches = list(re.finditer(_STANDALONE_PHONE, css, flags=re.S))
    blocks = [m for m in matches if ".terminal-overlay {" in m.group(1)]
    assert len(blocks) == 1, "the overlay needs exactly one standalone-phone rule"
    assert blocks[0].start() > overlay_base
    body = blocks[0].group(1)
    assert _PAD in body
    for prop in ("transform", "will-change", "filter", "position"):
        assert prop not in body, prop


def test_band_above_the_bar_is_the_page_colour() -> None:
    """The terminal screen's own colour must not show above the bar, as it is not on the tabs."""
    css = stylesheet_text()
    block = next(m for m in re.finditer(_STANDALONE_PHONE, css, flags=re.S) if ".terminal-overlay {" in m.group(1))
    assert "linear-gradient(var(--bg), var(--bg))" in block.group(1)
    assert "calc(env(safe-area-inset-top, 0px) + var(--standalone-head-offset))" in block.group(1)


def test_app_container_has_no_competing_standalone_offset() -> None:
    """The nav owns `.app`'s top; a second rule of the app's would be outranked and dead."""
    css = stylesheet_text()
    for m in re.finditer(_STANDALONE_PHONE, css, flags=re.S):
        assert not re.search(r"(^|\n)\s*\.app \{", m.group(1))


def test_the_nav_loads_before_the_app_styles() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert index.index("_vendored/nav/nav-tabs.css") < index.index("styles/base.css")


def test_every_tab_lives_inside_the_one_app_container() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert len(re.findall(r'^\s*<main class="app">', index, flags=re.M)) == 1
    assert index.index('<nav class="tabs"') < index.index("\n  <main class=\"app\">")
