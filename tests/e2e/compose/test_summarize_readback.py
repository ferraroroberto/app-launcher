"""Regression pin for the read-aloud "summarize & read" dropdown (issue #210).

The session bar's ⋮ menu carries "Read aloud" (verbatim, #190/#203/#206) and
"Summarize & read" — the reply is condensed by the hub's ``claude-haiku-4-5``
(``POST /api/tts/summarize``) before it is spoken, for hands-free / driving
listening. The Summarize row only appears when the hub is reachable.

The summary fetch arms the hub AudioContext in the click gesture (``prepareHub``)
*before* awaiting the LLM, then reads into it (``speakHubInto``) so iOS still
lets the audio sound after the round-trip. The hub endpoints and Web Audio are
stubbed; the JS surface is driven through the ``window.__readback`` seam,
mirroring ``test_hub_readback.py``.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._audio_mock import AUDIO_MOCK
from tests.e2e._readaloud_menu import (
    READ_ITEM,
    SUMMARIZE_ITEM,
    open_menu,
    wait_for_hub_probe,
)
from tests.e2e._surface import surface
from tests.e2e.fixtures.waits import OVERLAY_OPEN_MS

pytestmark = pytest.mark.smoke

_PCM = b"\xc2\xff\xc0\xff\xc5\xff\xca\xff"


def _load_readback(page: Page, base_url: str) -> None:
    """Land on the SPA until the ``window.__readback`` seam exists. These tests
    drive the JS read-aloud surface through that seam and stubbed audio/hub
    routes, so they need the page's modules, not a live PTY session or the
    terminal overlay (#954, #1515)."""
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_function("() => window.__readback")


def _open_terminal(page: Page, base_url: str, sid: str) -> None:
    page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")
    page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)


def _route_hub(page: Page, *, available: bool = True,
               summary: str = "Build is green. No decision needed.",
               summarize_status: int = 200) -> dict:
    """Stub the hub routes. The returned dict's ``summarize_status`` is read on
    every request, so a test can turn the summarize answer into an error."""
    answer = {"summarize_status": summarize_status}
    page.route(
        "**/api/tts/health",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"available": %s}' % ("true" if available else "false"),
        ),
    )
    page.route(
        "**/api/tts/summarize",
        lambda route: route.fulfill(
            status=answer["summarize_status"], content_type="application/json",
            body='{"summary": "%s"}' % summary,
        ),
    )
    page.route(
        "**/api/tts/speak",
        lambda route: route.fulfill(
            status=200, content_type="audio/L16",
            headers={"X-Sample-Rate": "24000"}, body=_PCM,
        ),
    )
    return answer


def test_summarize_then_play_composes_and_a_failed_summary_rejects(
    authed_page: Page, base_url: str
) -> None:
    """summarizeReply() POSTs the reply to /api/tts/summarize and resolves the
    hub's condensed summary string. The summarize action's composition: arm the
    hub audio in the gesture (prepareHub), await the summary (summarizeReply),
    then read it into the armed context (speakHubInto), so the audio still plays
    after the round-trip. A failed summarize POST makes summarizeReply() reject,
    so the click handler can surface an error and skip the read."""
    authed_page.add_init_script(AUDIO_MOCK)
    answer = _route_hub(authed_page, summary="Shipped. Your call: deploy now?")
    _load_readback(authed_page, base_url)
    got = authed_page.evaluate(
        "async () => {"
        "  const handle = window.__readback.prepareHub();"
        "  const summary = await window.__readback.summarizeReply('long', "
        "    { token: 'bt', terminalToken: 'tt' });"
        "  const ok = await window.__readback.speakHubInto(handle, summary, {});"
        "  return Object.assign({ ok, summary }, window.__audioLog); }"
    )
    assert got["ok"] is True
    assert got["summary"] == "Shipped. Your call: deploy now?"
    assert got["contexts"] >= 1      # AudioContext armed in the gesture
    assert got["resumed"] >= 1       # resumed for iOS autoplay
    assert got["started"] >= 1       # the summary PCM was scheduled

    answer["summarize_status"] = 502
    rejected = authed_page.evaluate(
        "async () => { try { await window.__readback.summarizeReply('x', {});"
        " return false; } catch (_) { return true; } }"
    )
    assert rejected is True


def test_summary_modal_closes_when_reading_ends_and_on_a_tap(
    authed_page: Page, base_url: str
) -> None:
    """The summary modal is shown while the summary is read aloud and must
    auto-close the moment reading stops: wired through the speaking-state
    machine, so a natural finish, a stop, or a tab-leave all close it. Tapping
    the modal dismisses it too (and stops the read)."""
    _load_readback(authed_page, base_url)
    modal = authed_page.locator("#summaryModal")
    # Open the modal, then drive the speaking state to idle (cancelHub flips it).
    authed_page.evaluate("() => { document.getElementById('summaryModal').hidden = false; }")
    expect(modal).to_be_visible()
    authed_page.evaluate("() => window.__readback.cancelHub()")
    expect(modal).to_be_hidden()

    authed_page.evaluate("() => { document.getElementById('summaryModal').hidden = false; }")
    expect(modal).to_be_visible()
    surface(authed_page, "readback.summary-modal")
    modal.click()
    expect(modal).to_be_hidden()


def test_menu_suppressed_when_hub_unavailable(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """With the hub down, the ⋮ menu still offers Read aloud (on-device
    speech) but never Summarize & read."""
    _route_hub(authed_page, available=False)
    _open_terminal(authed_page, base_url, launched_pty_session)
    wait_for_hub_probe(authed_page, "down")
    open_menu(authed_page)
    expect(authed_page.locator(READ_ITEM)).to_be_visible()
    expect(authed_page.locator(SUMMARIZE_ITEM)).to_have_count(0)


def test_menu_offers_both_actions_and_its_row_stops_a_read_in_progress(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """When the hub is reachable, the ⋮ menu lists both "Read aloud" and
    "Summarize & read", above "Stop and kill"; there is no separate speaker
    button in the bar. So the ⋮ menu is where a read is stopped: while speech
    plays the ⋮ button pulses, the Read aloud row reads "Stop reading", the
    Summarize row steps aside, and tapping the row silences it."""
    authed_page.add_init_script(AUDIO_MOCK)
    _route_hub(authed_page, available=True)
    _open_terminal(authed_page, base_url, launched_pty_session)
    # The rows are built on every open, so wait for the probe to settle first.
    wait_for_hub_probe(authed_page, "up")
    expect(authed_page.locator("#terminalSpeak")).to_have_count(0)
    open_menu(authed_page)
    expect(authed_page.locator(READ_ITEM)).to_be_visible()
    expect(authed_page.locator(SUMMARIZE_ITEM)).to_be_visible()
    order = authed_page.locator(".terminal-menu [role=menuitem]").evaluate_all(
        "els => els.map(e => e.dataset.item || e.textContent.trim())"
    )
    stop = order.index("action-stop-close")
    assert order.index("overlay-read-aloud-btn") < order.index("overlay-summarize-read-btn") < stop, order
    open_menu(authed_page)  # the anchor toggles its menu shut
    expect(authed_page.locator(READ_ITEM)).to_be_hidden()

    authed_page.evaluate("() => window.__readback.speakHub('Reading on.', {})")
    expect(authed_page.locator("#terminalMenu")).to_have_attribute("data-speaking", "true")
    expect(authed_page.locator("#terminalMenu")).to_have_class(re.compile(r"\bspeaking\b"))
    open_menu(authed_page)
    expect(authed_page.locator(READ_ITEM)).to_have_text("Stop reading")
    expect(authed_page.locator(SUMMARIZE_ITEM)).to_have_count(0)
    authed_page.locator(READ_ITEM).click()
    expect(authed_page.locator("#terminalMenu")).to_have_attribute("data-speaking", "false")
