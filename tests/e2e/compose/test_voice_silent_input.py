"""Dictation warns while recording when the mic input carries no sound.

iOS routes the mic to a connected Bluetooth headset that can carry nothing,
and a take used to run for minutes before the finish-time "silent recording"
toast said so. The recorder now reads the live stream's level: a silent input
raises a banner within seconds and the recorder names the input; a real signal
never raises it.

The input is a real ``MediaStream`` built from Web Audio (a silent node, or an
oscillator), so the page's own ``AnalyserNode`` path runs for real; only
``getUserMedia`` and ``MediaRecorder`` are stubbed, as in
``test_voice_dictation.py``. The sessions routes are mocked so no
voice-transcriber is needed.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._audio_mock import RECORDER_BASE_JS
from tests.e2e.compose.test_voice_dictation import (
    _open_compose_with_record,
    _open_terminal,
)

pytestmark = pytest.mark.smoke

_LABEL = "Test Headset"

# A stream with a real audio graph behind it. `__tone` picks an oscillator
# over silence; closing a context the page opened is counted, so a test can
# wait on the page having finished listening instead of sleeping.
_INPUT_MOCK = ("""
(() => {
  window.__watchClosed = 0;
  const realClose = AudioContext.prototype.close;
  AudioContext.prototype.close = function () {
    window.__watchClosed += 1;
    return realClose.call(this);
  };
  navigator.mediaDevices = navigator.mediaDevices || {};
  navigator.mediaDevices.getUserMedia = async () => {
    const ctx = new AudioContext();
    await ctx.resume();
    const dest = ctx.createMediaStreamDestination();
    if (window.__tone) {
      const osc = ctx.createOscillator();
      osc.frequency.value = 440;
      osc.connect(dest);
      osc.start();
    }
    const track = dest.stream.getAudioTracks()[0];
    Object.defineProperty(track, 'label', { value: '__LABEL__' });
    return dest.stream;
  };
""" + RECORDER_BASE_JS + """  FakeRecorder.isTypeSupported = () => true;
  window.MediaRecorder = FakeRecorder;
})()
""").replace("__LABEL__", _LABEL)


def _mock_sessions(page: Page) -> None:
    page.route(
        "**/api/transcribe/sessions",
        lambda route: route.fulfill(status=503, body="nope"),
    )
    page.route(
        "**/api/transcribe",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"transcript": "", "silent": true}',
        ),
    )


def _start_take(page: Page, base_url: str, sid: str, tone: bool) -> None:
    page.add_init_script(f"window.__tone = {str(tone).lower()};")
    page.add_init_script(_INPUT_MOCK)
    _mock_sessions(page)
    _open_terminal(page, base_url, sid)
    _open_compose_with_record(page)
    page.locator("#terminalComposeBar .composer-mic").click()


def test_silent_input_shows_banner_and_names_the_mic(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """A silent input raises the no-sound banner within seconds, with the
    input's name above it; stopping the take clears both."""
    _start_take(authed_page, base_url, launched_pty_session, tone=False)

    notice = authed_page.locator("#terminalComposeBar .composer-mic-notice")
    expect(notice).to_contain_text(f"Mic: {_LABEL}")
    expect(notice.locator(".mic-notice-silent")).to_be_hidden()
    # The level strip is up for the whole take, flat until the verdict.
    expect(notice.locator(".rec-wave")).to_be_visible()
    # The verdict lands 3 s into the take; the expect polls for it.
    expect(notice.locator(".mic-notice-silent")).to_be_visible(timeout=6_000)
    expect(notice.locator(".mic-notice-silent")).to_contain_text("No sound")
    expect(notice.locator(".rec-wave")).to_have_attribute("data-state", "silent")

    authed_page.locator("#terminalComposeBar .composer-mic").click()
    expect(notice).to_be_hidden()
    expect(notice.locator(".rec-wave")).to_have_count(0)


def test_real_signal_moves_the_wave_and_never_shows_the_banner(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """A tone is heard at once: the strip goes live and no banner ever shows.
    Stopping the take removes the strip and closes the one listening context."""
    _start_take(authed_page, base_url, launched_pty_session, tone=True)

    notice = authed_page.locator("#terminalComposeBar .composer-mic-notice")
    expect(notice).to_contain_text(f"Mic: {_LABEL}")
    wave = notice.locator(".rec-wave")
    expect(wave).to_have_attribute("data-state", "live")
    # A tone lifts the bars off their resting sliver.
    authed_page.wait_for_function(
        "[...document.querySelectorAll('.composer-mic-notice .rec-wave-bars > i')]"
        ".some(i => i.style.transform !== 'scaleY(0.08)')",
        timeout=6_000,
    )
    expect(notice.locator(".mic-notice-silent")).to_be_hidden()
    expect(notice).not_to_have_class("is-silent")

    authed_page.locator("#terminalComposeBar .composer-mic").click()
    expect(wave).to_have_count(0)
    # The page closes the context it opened for the strip when the take ends.
    authed_page.wait_for_function("window.__watchClosed >= 1", timeout=6_000)
