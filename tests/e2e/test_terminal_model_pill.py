"""The session overlay header's model pill (#1383).

A neutral pill left of the context ring names the session's model ("Opus",
"Sol"). The model is derived webapp-side from the session's spawn flags (and a
Codex title) -- the session-host is untouched -- so these tests stub the
sessions list's ``model`` field rather than depend on the e2e stub child's
flags; the derivation itself is pinned by ``tests/test_session_model.py``.

What this guards in the browser: the pill's place (left of the ring, level
with it), that it is absent -- not blank -- when no model is known, and that
at 360px, with every other control showing, the header neither overflows nor
loses the actions: the title takes the squeeze and truncates.
"""

from __future__ import annotations

import json
import re
from typing import Optional

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.conftest import OVERLAY_OPEN_MS

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_PHONE = {"width": 360, "height": 740}


def _stub_model(page: Page, model: Optional[str]) -> None:
    """Make every session in the list report ``model``."""
    def _patch(route):
        resp = route.fetch()
        body = resp.json()
        for sess in body.get("sessions", []):
            sess["model"] = model
        route.fulfill(response=resp, json=body)

    page.route(re.compile(r".*/api/claude-code/sessions$"), _patch)


def _stub_context(page: Page, percent: int) -> None:
    page.route(
        re.compile(r".*/api/claude-code/sessions/[^/]+/context$"),
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"available": True, "percent": percent, "reason": None}),
        ),
    )


def _open_overlay(page: Page, base_url: str, sid: str) -> None:
    # The session-list row tap is the phone path (the ?terminal= deep link
    # would open a PC mirror window and hide parts of the bar).
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator(
        f'#sessionsList li.session-item[data-session-id="{sid}"] .session-open'
    ).click()
    page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)


def test_model_pill_sits_left_of_the_ring_and_the_header_still_fits(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    authed_page.set_viewport_size(_PHONE)
    _stub_model(authed_page, "Opus")
    _stub_context(authed_page, 42)
    _open_overlay(authed_page, base_url, launched_pty_session)

    pill = authed_page.locator("#terminalModel")
    ring = authed_page.locator("#contextRing")
    expect(pill).to_be_visible()
    expect(pill).to_have_text("Opus")
    expect(ring).to_be_visible()
    # Every control showing (read-aloud included, the usual case on a phone
    # with TTS), at a current iPhone's width: the pill reads in full.
    authed_page.evaluate("document.querySelector('#terminalSpeak').hidden = false")
    authed_page.set_viewport_size({"width": 390, "height": 740})
    wide = authed_page.evaluate(
        "() => { const p = document.querySelector('#terminalModel');"
        " return p.scrollWidth > p.clientWidth; }"
    )
    assert not wide, "the model pill clips its own text at 390px"
    # Then the narrow phone: there is no room left for the title, and the bar
    # must still not overflow.
    authed_page.set_viewport_size(_PHONE)

    order = authed_page.eval_on_selector_all(
        "#terminalOverlay .terminal-bar-actions > *", "els => els.map(el => el.id)"
    )
    assert order[:2] == ["terminalModel", "contextRing"], order

    geo = authed_page.evaluate(
        """() => {
          const r = (s) => document.querySelector(s).getBoundingClientRect();
          const bar = document.querySelector('#terminalOverlay .terminal-bar');
          const group = document.querySelector('#terminalOverlay .terminal-bar-actions');
          return {
            vw: window.innerWidth,
            pill: r('#terminalModel'), ring: r('#contextRing'),
            title: r('#terminalTitle'), menu: r('#terminalMenu'),
            back: r('#terminalBack'),
            barScroll: bar.scrollWidth, barClient: bar.clientWidth,
            groupScroll: group.scrollWidth, groupClient: group.clientWidth,
          };
        }"""
    )
    assert geo["pill"]["right"] <= geo["ring"]["left"], "the pill is not left of the ring"
    assert abs(
        (geo["pill"]["top"] + geo["pill"]["bottom"]) / 2
        - (geo["ring"]["top"] + geo["ring"]["bottom"]) / 2
    ) <= 2, "the pill is not level with the ring"
    # No overflow at 360px: the bar, the group and the right-most control.
    assert geo["barScroll"] <= geo["barClient"] + 1, geo
    assert geo["groupScroll"] <= geo["groupClient"] + 1, geo
    assert geo["menu"]["right"] <= geo["vw"], f"⋮ pushed off-screen: {geo['menu']}"
    assert geo["back"]["left"] >= 0
    # The title is what gave way, and it truncates rather than wraps.
    assert geo["title"]["right"] <= geo["pill"]["left"] + 1
    expect(authed_page.locator("#terminalTitle")).to_have_css("text-overflow", "ellipsis")
    expect(authed_page.locator("#terminalTitle")).to_have_css("white-space", "nowrap")


def test_no_pill_when_the_model_is_unknown(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    authed_page.set_viewport_size(_PHONE)
    _stub_model(authed_page, None)
    _stub_context(authed_page, 42)
    _open_overlay(authed_page, base_url, launched_pty_session)

    expect(authed_page.locator("#contextRing")).to_be_visible()
    pill = authed_page.locator("#terminalModel")
    expect(pill).to_be_hidden()
    expect(pill).to_have_text("")
