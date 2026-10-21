"""Regression pin for the read-aloud "summarize & read" dropdown (issue #210).

The 🔊 read-aloud button becomes a small action menu: "Read aloud" (verbatim,
#190/#203/#206) and "Summarize & read" — the reply is condensed by the hub's
``claude-haiku-4-5`` (``POST /api/tts/summarize``) before it is spoken, for
hands-free / driving listening. The menu only appears when the hub is reachable;
otherwise the button keeps its original single-tap behaviour.

The summary fetch arms the hub AudioContext in the click gesture (``prepareHub``)
*before* awaiting the LLM, then reads into it (``speakHubInto``) so iOS still
lets the audio sound after the round-trip. The hub endpoints and Web Audio are
stubbed; the JS surface is driven through the ``window.__readback`` seam,
mirroring ``test_hub_readback.py``.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._audio_mock import AUDIO_MOCK
from tests.e2e._surface import surface
from tests.e2e.conftest import OVERLAY_OPEN_MS

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
               summarize_status: int = 200) -> None:
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
            status=summarize_status, content_type="application/json",
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


def test_summarize_reply_returns_summary(
    authed_page: Page, base_url: str
) -> None:
    """summarizeReply() POSTs the reply to /api/tts/summarize and resolves the
    hub's condensed summary string."""
    _route_hub(authed_page, summary="Tests pass. Decide whether to merge.")
    _load_readback(authed_page, base_url)
    got = authed_page.evaluate(
        "async () => window.__readback.summarizeReply('a long reply', "
        "{ token: 'bt', terminalToken: 'tt' })"
    )
    assert got == "Tests pass. Decide whether to merge."


def test_summarize_reply_rejects_on_error(
    authed_page: Page, base_url: str
) -> None:
    """A failed summarize POST makes summarizeReply() reject, so the click
    handler can surface an error and skip the read."""
    _route_hub(authed_page, summarize_status=502)
    _load_readback(authed_page, base_url)
    rejected = authed_page.evaluate(
        "async () => { try { await window.__readback.summarizeReply('x', {});"
        " return false; } catch (_) { return true; } }"
    )
    assert rejected is True


def test_summarize_then_play_composes(
    authed_page: Page, base_url: str
) -> None:
    """The summarize action's composition: arm the hub audio in the gesture
    (prepareHub), await the summary (summarizeReply), then read it into the
    armed context (speakHubInto) — the audio still plays after the round-trip."""
    authed_page.add_init_script(AUDIO_MOCK)
    _route_hub(authed_page, summary="Shipped. Your call: deploy now?")
    _load_readback(authed_page, base_url)
    got = authed_page.evaluate(
        "async () => {"
        "  const handle = window.__readback.prepareHub();"
        "  const summary = await window.__readback.summarizeReply('long', {});"
        "  const ok = await window.__readback.speakHubInto(handle, summary, {});"
        "  return Object.assign({ ok, summary }, window.__audioLog); }"
    )
    assert got["ok"] is True
    assert got["summary"] == "Shipped. Your call: deploy now?"
    assert got["contexts"] >= 1      # AudioContext armed in the gesture
    assert got["resumed"] >= 1       # resumed for iOS autoplay
    assert got["started"] >= 1       # the summary PCM was scheduled


def test_menu_offers_both_actions_when_hub_available(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """When the hub is reachable, tapping 🔊 opens a dropdown with both
    "Read aloud" and "Summarize & read"."""
    _route_hub(authed_page, available=True)
    _open_terminal(authed_page, base_url, launched_pty_session)
    # The probe clears the summarize action's `hidden` attribute once the hub
    # answers available (the action lives inside the still-closed popover, so it
    # isn't *rendered* yet — wait on the attribute, not visibility).
    authed_page.wait_for_selector(
        '#terminalSpeakPopover [data-action="summarize"]:not([hidden])',
        state="attached", timeout=10_000,
    )
    authed_page.locator("#terminalSpeak").click()
    expect(authed_page.locator("#terminalSpeakPopover")).to_be_visible()
    expect(authed_page.locator('#terminalSpeakPopover [data-action="read"]')).to_be_visible()
    expect(
        authed_page.locator('#terminalSpeakPopover [data-action="summarize"]')
    ).to_be_visible()


def test_summary_modal_autocloses_when_reading_ends(
    authed_page: Page, base_url: str
) -> None:
    """The summary modal is shown while the summary is read aloud and must
    auto-close the moment reading stops — wired through the speaking-state
    machine, so a natural finish, a stop, or a tab-leave all close it."""
    _load_readback(authed_page, base_url)
    # Open the modal, then drive the speaking state to idle (cancelHub flips it).
    authed_page.evaluate("() => { document.getElementById('summaryModal').hidden = false; }")
    expect(authed_page.locator("#summaryModal")).to_be_visible()
    authed_page.evaluate("() => window.__readback.cancelHub()")
    expect(authed_page.locator("#summaryModal")).to_be_hidden()


def test_summary_modal_tap_dismisses(
    authed_page: Page, base_url: str
) -> None:
    """Tapping the modal dismisses it (and stops the read)."""
    _load_readback(authed_page, base_url)
    authed_page.evaluate("() => { document.getElementById('summaryModal').hidden = false; }")
    expect(authed_page.locator("#summaryModal")).to_be_visible()
    surface(authed_page, "readback.summary-modal")
    authed_page.locator("#summaryModal").click()
    expect(authed_page.locator("#summaryModal")).to_be_hidden()


def test_menu_suppressed_when_hub_unavailable(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """With the hub down, the summarize action stays hidden and 🔊 keeps its
    original single-tap behaviour — no dropdown opens."""
    _route_hub(authed_page, available=False)
    _open_terminal(authed_page, base_url, launched_pty_session)
    # The summarize action keeps its `hidden` attribute (the probe never cleared
    # it). Give the probe a beat to resolve, then confirm the attribute remains.
    authed_page.wait_for_selector(
        '#terminalSpeakPopover [data-action="summarize"][hidden]',
        state="attached", timeout=10_000,
    )
    authed_page.locator("#terminalSpeak").click()
    # Single-tap read path — the popover never opens (no reply yet → a toast).
    expect(authed_page.locator("#terminalSpeakPopover")).to_be_hidden()
