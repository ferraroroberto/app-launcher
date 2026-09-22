"""Every hand-rolled full-screen overlay hides the phone tab bar (#1143).

The floating bottom tab bar is a body-level ``<nav class="tabs">`` at
``z-index: 120``. A hand-rolled overlay (a ``[hidden]``-toggled ``<div>``, not
a native ``<dialog>``) has to be named in two ``styles.css`` rules: the phone
``.tabs`` hide rule and the standalone ``.app { overflow: hidden }`` lock.

A forgotten entry doesn't show up in Safari, so it tends to slip through. In
the installed PWA the vendored shell makes ``.app`` ``position: fixed``, which
is a stacking context. An overlay nested inside ``.app`` then stacks under the
bar whatever its own z-index. That's how #1119's conversation viewer shipped
with the bar over it. This test reads the overlays out of ``index.html`` and
fails when either rule leaves one out, so adding an overlay means adding it
to both rules too.
"""

from __future__ import annotations

import pathlib
import re
from html.parser import HTMLParser

STATIC = pathlib.Path(__file__).resolve().parents[1] / "app" / "webapp" / "static"

# The hand-rolled overlay classes: the session overlay's chrome, which every
# full-screen Coding / Life OS view reuses, plus the three one-off overlays.
_OVERLAY_CLASSES = {"terminal-overlay", "login-overlay", "summary-modal", "system-map-lightbox"}

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
    parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    return parser.ids


def _selectors_of_rules(declaration: str, target: str) -> set:
    """Every selector in a ``styles.css`` rule that sets ``declaration`` and
    whose selectors all end at ``target``."""
    css = re.sub(r"/\*.*?\*/", "", (STATIC / "styles.css").read_text(encoding="utf-8"), flags=re.S)
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
    for known in ("terminalOverlay", "lifeOsBrowser", "loginOverlay", "summaryModal"):
        assert known in ids, f"overlay scan lost #{known}: {ids}"


def test_every_overlay_hides_the_phone_tab_bar() -> None:
    missing = _missing(".tabs", "visibility: hidden")
    assert not missing, (
        f"overlays not in the phone .tabs hide rule in styles.css: {missing} — "
        "the floating bottom bar stays on top of them (#1143)"
    )


def test_every_overlay_locks_the_standalone_app_scroller() -> None:
    missing = _missing(".app", "overflow: hidden")
    assert not missing, (
        f"overlays not in the standalone .app overflow lock in styles.css: {missing} — "
        ".app stays touch-scrollable behind them in the installed PWA (#1143)"
    )
