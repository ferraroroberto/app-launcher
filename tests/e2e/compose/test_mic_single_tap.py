"""Regression pin for #1581: the composer mic starts dictation on ONE tap.

The report (iPhone): the mic "sometimes needs two taps" -- the first only
seems to focus/highlight it, the second records. Keys, attach and Send start on
the first tap. This drives the mic with real touch taps (``tap()`` dispatches
touch events, not a bare ``click()``), from an idle composer, with the textarea
both focused and unfocused. ``getUserMedia`` / ``MediaRecorder`` are stubbed;
no real microphone is ever touched.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._audio_mock import RECORDER_BASE_JS
from tests.e2e.fixtures.waits import OVERLAY_OPEN_MS, flush_requests, wait_until

pytestmark = [pytest.mark.smoke, pytest.mark.iphone]

_MEDIA_MOCK = """
(() => {
  window.__gumCalls = 0;
  navigator.mediaDevices = navigator.mediaDevices || {};
  navigator.mediaDevices.getUserMedia = () => {
    window.__gumCalls++;
    const stream = { getTracks: () => [{ stop: () => {} }] };
    if (!window.__holdGum) return Promise.resolve(stream);
    return new Promise((resolve) => { window.__releaseGum = () => resolve(stream); });
  };
""" + RECORDER_BASE_JS + """  FakeRecorder.isTypeSupported = () => true;
  window.MediaRecorder = FakeRecorder;
})()
"""


def _open_composer(page: Page, base_url: str, sid: str, hold_gum: bool = False) -> None:
    page.add_init_script(_MEDIA_MOCK)
    if hold_gum:
        page.add_init_script("window.__holdGum = true")
    # No streamed session: the buffered fallback keeps the stub minimal.
    page.route(
        "**/api/transcribe/sessions",
        lambda route: route.fulfill(status=503, body="nope"),
    )
    page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")
    page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)
    page.evaluate("document.getElementById('terminalComposeBar').hidden = false")
    page.evaluate(
        "document.querySelector('#terminalComposeBar .composer-mic').disabled = false"
    )
    expect(page.locator("#terminalComposeBar")).to_be_visible()


@pytest.mark.parametrize("focused", [False, True], ids=["unfocused", "focused"])
def test_one_tap_starts_dictation(
    authed_page: Page, base_url: str, launched_pty_session: str, focused: bool
) -> None:
    if not authed_page.evaluate("'ontouchstart' in window"):
        pytest.skip("needs a touch-emulating projection (WebKit/iPhone)")
    _open_composer(authed_page, base_url, launched_pty_session)
    textarea = authed_page.locator("#terminalComposeBar .composer-input")
    if focused:
        textarea.tap()
        expect(textarea).to_be_focused()
    mic = authed_page.locator("#terminalComposeBar .composer-mic")
    mic.tap()
    expect(mic).to_have_class(re.compile(r"\brecording\b"))
    assert authed_page.evaluate("window.__gumCalls") == 1


def _info_lines(page: Page) -> list[str]:
    lines: list[str] = []
    page.on(
        "console",
        lambda m: lines.append(m.text) if m.type == "info" else None,
    )
    return lines


def _usage_capture(page: Page) -> list[dict]:
    """Stub the usage-log endpoint (#1524) and collect every event it is sent."""
    events: list[dict] = []

    def record(route) -> None:
        body = json.loads(route.request.post_data or "{}")
        events.extend(body.get("events", []))
        route.fulfill(status=200, content_type="application/json",
                      body='{"accepted":0,"rejected":0}')

    page.route(re.compile(r".*/api/usage/events$"), record)
    return events


def _mic_taps(page: Page, events: list[dict]) -> list[str]:
    """Hide the page (the client's flush) and return the mic control ids sent."""
    page.evaluate(
        "Object.defineProperty(document, 'visibilityState',"
        " { value: 'hidden', configurable: true });"
        " document.dispatchEvent(new Event('visibilitychange'));"
    )
    wait_until(page, lambda: any(e.get("kind") == "session_end" for e in events),
               "the page-hidden usage flush")
    flush_requests(page)
    return [e["id"] for e in events if e.get("kind") == "tap" and e["id"].startswith("mic.")]


def test_tap_swallowed_while_arming_leaves_a_breadcrumb(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """A tap the state machine ignores is logged, so the next report is readable."""
    lines = _info_lines(authed_page)
    usage = _usage_capture(authed_page)
    _open_composer(authed_page, base_url, launched_pty_session, hold_gum=True)
    mic = authed_page.locator("#terminalComposeBar .composer-mic")
    mic.click()
    expect(mic).to_have_class(re.compile(r"\barming\b"))
    mic.click()  # swallowed: getUserMedia is still pending
    authed_page.wait_for_function("true")
    assert any("tap: start recording" in t for t in lines), lines
    assert any("tap ignored" in t and "arming" in t for t in lines), lines
    assert authed_page.evaluate("window.__gumCalls") == 1
    # The server-side usage log gets the same two outcomes: a phone's console
    # is out of reach, so this is what the next report is read from.
    assert _mic_taps(authed_page, usage) == ["mic.start", "mic.ignored-arming"]


def test_tap_cancelling_a_finalizing_take_leaves_a_breadcrumb(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    lines = _info_lines(authed_page)
    usage = _usage_capture(authed_page)
    # The single-shot transcribe never answers: the take stays finalizing.
    authed_page.route("**/api/transcribe", lambda route: None)
    _open_composer(authed_page, base_url, launched_pty_session)
    mic = authed_page.locator("#terminalComposeBar .composer-mic")
    mic.click()
    expect(mic).to_have_class(re.compile(r"\brecording\b"))
    mic.click()  # stop -> finalizing
    expect(mic).to_have_class(re.compile(r"\bworking\b"))
    mic.click()  # spent cancelling, not starting
    expect(mic).not_to_have_class(re.compile(r"\bworking\b"))
    assert any("tap: stop recording" in t for t in lines), lines
    assert any("cancelled" in t and "finalizing" in t for t in lines), lines
    assert _mic_taps(authed_page, usage) == [
        "mic.start", "mic.stop", "mic.cancel-finalizing",
    ]
