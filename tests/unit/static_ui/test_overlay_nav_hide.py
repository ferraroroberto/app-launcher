"""Every hand-rolled full-screen overlay hides the phone tab bar (#1143).

The floating bottom tab bar is a body-level ``<nav class="tabs">``. A
hand-rolled overlay (a ``[hidden]``-toggled ``<div>``, not a native
``<dialog>``, which the vendored nav hides the bar for by itself) has to be
named in the phone ``.tabs`` hide rule in ``styles/base.css``.

A forgotten entry is easy to miss: the bar stays pinned over the overlay's
bottom edge, which covers a composer or a "Latest" pill (#1119's conversation
viewer shipped that way). This test reads the overlays out of ``index.html``
and fails when the rule leaves one out, so adding an overlay means adding it
to the rule too.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from app.webapp.shell_html import assemble_shell
from tests._css import STATIC, stylesheet_text

# The hand-rolled overlay classes: the session overlay's chrome, which every
# full-screen Coding / Life OS view reuses, plus the three one-off overlays.
_OVERLAY_CLASSES = {"terminal-overlay", "login-overlay", "summary-modal", "image-lightbox"}

# The session overlay is keyed on the body class terminal.js sets, not on its
# [hidden] state: it stays mounted while hidden for reconnects.
_BODY_CLASS_KEYED = {"terminalOverlay": "body.terminal-open"}


class _OverlayIds(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = dict(attrs)
        if set((a.get("class") or "").split()) & _OVERLAY_CLASSES:
            assert a.get("id"), f"overlay <{tag} class={a.get('class')!r}> has no id to key on"
            self.ids.append(a["id"])


def _overlay_ids() -> list:
    parser = _OverlayIds()
    parser.feed(assemble_shell(STATIC / "index.html"))
    return parser.ids


def _selectors_of_rules(declaration: str, target: str) -> set:
    """Every selector in a stylesheet rule that sets ``declaration`` and
    whose selectors all end at ``target``."""
    css = re.sub(r"/\*.*?\*/", "", stylesheet_text(), flags=re.S)
    found = set()
    for sel_text, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if declaration not in body:
            continue
        sels = [s.strip() for s in sel_text.split(",")]
        if all(s.endswith(target) for s in sels):
            found.update(sels)
    return found


def _missing(target: str, declaration: str) -> list:
    selectors = _selectors_of_rules(declaration, target)
    missing = []
    for oid in _overlay_ids():
        prefix = _BODY_CLASS_KEYED.get(oid, f"body:has(#{oid}:not([hidden]))")
        if f"{prefix} {target}" not in selectors:
            missing.append(oid)
    return missing


def test_overlay_ids_are_found() -> None:
    ids = _overlay_ids()
    # A guard on the scan itself: if it finds nothing, the checks below pass
    # without checking anything.
    for known in ("terminalOverlay", "changesOverlay", "loginOverlay", "summaryModal"):
        assert known in ids, f"overlay scan lost #{known}: {ids}"


def test_every_overlay_hides_the_phone_tab_bar() -> None:
    missing = _missing(".tabs", "visibility: hidden")
    assert not missing, (
        f"overlays not in the phone .tabs hide rule in styles/base.css: {missing} — "
        "the floating bottom bar stays on top of them (#1143)"
    )

