"""The installed phone app starts its content below the safe area (#2244).

iOS renders the header card soft when the page content starts right at the
safe-area edge of the standalone app, and 20px lower renders it sharp on the
device. Desktop engines cannot show the softness, so this pins the structure:
the offset has one home, it is plain padding on top of the safe area on the
single ``.app`` container every tab lives in, it is scoped to standalone on a
phone, it comes after the vendored nav's phone ``.app`` padding, and the rule
adds no layer.
"""

from __future__ import annotations

import re

from tests._css import STATIC, stylesheet_text

_STANDALONE_PHONE = (
    r"@media \(display-mode: standalone\) and \(pointer: coarse\) and \(max-width: 520px\) \{(.*?)\n\}"
)


def test_app_applies_the_offset_in_the_installed_phone_app_only() -> None:
    css = stylesheet_text()
    assert css.count("--standalone-head-offset: 20px;") == 1
    block = re.search(_STANDALONE_PHONE, css, flags=re.S)
    assert block, "the offset rule must be scoped to the installed phone app"
    body = block.group(1)
    assert ".app {" in body
    assert "padding-top: calc(env(safe-area-inset-top, 0px) + var(--standalone-head-offset));" in body
    for prop in ("transform", "will-change", "filter", "position"):
        assert prop not in body, prop


def test_offset_rule_follows_the_vendored_phone_padding() -> None:
    """Equal specificity: the later sheet wins, so the app's must come last."""
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert index.index("_vendored/nav/nav-tabs.css") < index.index("styles/base.css")


def test_every_tab_lives_inside_the_one_app_container() -> None:
    """The offset is on ``.app`` once, so no tab doubles it or misses it."""
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert len(re.findall(r'^\s*<main class="app">', index, flags=re.M)) == 1
    assert index.index('<nav class="tabs"') < index.index("\n  <main class=\"app\">")
