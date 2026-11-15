"""Dictation stops by itself after a pause in speech when Settings turns it on.

With ``dictation_auto_stop_seconds`` set, a take that has heard speech and then
stays quiet for that long stops as if the mic were tapped, and the transcript
lands in the composer for review, so Send is the only tap left. Quiet before
any speech never stops a take: that is the no-sound warning's case.

The input is a real ``MediaStream`` from Web Audio (an oscillator behind a gain
node the page can open and shut), so the page's own level reader runs for real;
``getUserMedia`` and ``MediaRecorder`` are stubbed and the transcribe routes
mocked, as in ``test_voice_silent_input.py``. ``/api/config`` is answered with
the setting on rather than saved, so the shared disposable config is untouched.
"""

from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e._audio_mock import RECORDER_BASE_JS
from tests.e2e.compose.test_voice_dictation import (
    _open_compose_with_record,
    _open_terminal,
)

pytestmark = pytest.mark.smoke

_TRANSCRIPT = "dictated by voice"

# `__speakFor(ms)` opens the tone for ms then shuts it, timed in the page.
_INPUT_MOCK = """
(() => {
  navigator.mediaDevices = navigator.mediaDevices || {};
  navigator.mediaDevices.getUserMedia = async () => {
    const ctx = new AudioContext();
    await ctx.resume();
    const dest = ctx.createMediaStreamDestination();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    gain.gain.value = 0;
    osc.connect(gain);
    gain.connect(dest);
    osc.start();
    window.__speakFor = (ms) => {
      gain.gain.value = 1;
      setTimeout(() => { gain.gain.value = 0; }, ms);
    };
    return dest.stream;
  };
""" + RECORDER_BASE_JS + """  FakeRecorder.isTypeSupported = () => true;
  window.MediaRecorder = FakeRecorder;
})()
"""


def _config_with_auto_stop(route: Route) -> None:
    resp = route.fetch()
    body = resp.json()
    body["dictation_auto_stop_seconds"] = 2
    route.fulfill(response=resp, body=json.dumps(body))


def test_a_pause_after_speech_stops_the_take_and_lands_the_transcript(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    page = authed_page
    page.add_init_script(_INPUT_MOCK)
    page.route(re.compile(r".*/api/config$"), _config_with_auto_stop)
    page.route(
        "**/api/transcribe/sessions",
        lambda route: route.fulfill(status=503, body="nope"),
    )
    page.route(
        "**/api/transcribe",
        lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"transcript": _TRANSCRIPT}),
        ),
    )
    _open_terminal(page, base_url, launched_pty_session)
    _open_compose_with_record(page)

    mic = page.locator("#terminalComposeBar .composer-mic")
    mic.click()
    expect(mic).to_have_class(re.compile(r"\brecording\b"))

    # Three quiet seconds with no speech yet: the no-sound warning shows and
    # the take, past its 2 s pause, is still recording.
    notice = page.locator("#terminalComposeBar .composer-mic-notice")
    expect(notice.locator(".mic-notice-silent")).to_be_visible(timeout=6_000)
    expect(mic).to_have_class(re.compile(r"\brecording\b"))

    # Speech, then quiet: the take stops by itself and the text lands.
    page.evaluate("window.__speakFor(800)")
    expect(mic).not_to_have_class(re.compile(r"\brecording\b"), timeout=8_000)
    expect(page.locator("#terminalComposeBar .composer-input")).to_have_value(
        _TRANSCRIPT, timeout=10_000
    )
