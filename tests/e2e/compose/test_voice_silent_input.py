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


def _start_take(
    page: Page, base_url: str, sid: str, tone: bool, label: str = _LABEL
) -> None:
    page.add_init_script(f"window.__tone = {str(tone).lower()};")
    page.add_init_script(_INPUT_MOCK.replace(_LABEL, label))
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
    expect(notice.locator(".rec-wave")).to_have_attribute("data-signal", "silent")

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
    expect(wave).to_have_attribute("data-signal", "live")
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


def test_silent_remote_audio_input_names_the_remote_desktop_setting(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """A silent "Remote Audio" input (Windows' redirected Remote Desktop mic)
    raises the Remote Desktop wording, not the Bluetooth one."""
    _start_take(
        authed_page, base_url, launched_pty_session, tone=False, label="Remote Audio"
    )

    notice = authed_page.locator("#terminalComposeBar .composer-mic-notice")
    expect(notice).to_contain_text("Mic: Remote Audio")
    warning = notice.locator(".mic-notice-silent")
    expect(warning).to_be_visible(timeout=6_000)
    expect(warning).to_contain_text("check the Remote Desktop audio recording setting")
    expect(warning).not_to_contain_text("Bluetooth")


_RECTS_JS = """
() => {
  const r = (el) => {
    const b = el.getBoundingClientRect();
    return { left: b.left, top: b.top, right: b.right, bottom: b.bottom };
  };
  const q = (s) => document.querySelector('#terminalComposeBar ' + s);
  const notice = q('.composer-mic-notice');
  const visible = notice && !notice.hidden;
  const part = (s) => (visible && notice.querySelector(s))
    ? r(notice.querySelector(s)) : null;
  return {
    bar: r(q('.compose-bar')),
    buttons: [...document.querySelectorAll(
      '#terminalComposeBar .compose-tools button')].map(r),
    notice: visible ? r(notice) : null,
    name: part('.mic-notice-input'),
    nameText: (() => {
      const el = visible && notice.querySelector('.mic-notice-input');
      if (!el) return null;
      const range = document.createRange();
      range.selectNodeContents(el);
      return r(range);
    })(),
    wave: part('.rec-wave'),
    warning: part('.mic-notice-silent:not([hidden])'),
    viewportBottom: window.innerHeight,
  };
}
"""


def _same(a: dict, b: dict) -> bool:
    return all(abs(a[k] - b[k]) < 0.5 for k in a)


# A phone's rounded corner cuts about this much off a row's outer ends.
_CORNER_CLEARANCE = 12


def _assert_group_centred(rects: dict) -> None:
    """Name, strip and timer sit as one group in the middle of the row: equal
    room either side, neither end near the row's edge, a small gap between.
    The name is measured by its text, not its box: a box stretched over the free
    space would hide text pinned to the row's left edge."""
    notice, name, wave = rects["notice"], rects["nameText"], rects["wave"]
    left_room = name["left"] - notice["left"]
    right_room = notice["right"] - wave["right"]
    assert abs(left_room - right_room) < 1.5, (left_room, right_room)
    assert min(left_room, right_room) >= _CORNER_CLEARANCE, (left_room, right_room)
    assert wave["left"] - name["right"] < 16, (name, wave)


def test_notice_is_one_line_below_the_compose_box_and_moves_nothing(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """The notice is one row under the compose box (name, level strip and
    timer side by side), inside the screen, and the box and its buttons keep
    their place while it shows and after it hides. The silent-mic warning then
    takes a line of its own under that row."""
    page = authed_page
    page.add_init_script("window.__tone = false;")
    page.add_init_script(_INPUT_MOCK)
    _mock_sessions(page)
    _open_terminal(page, base_url, launched_pty_session)
    _open_compose_with_record(page)

    before = page.evaluate(_RECTS_JS)
    assert before["notice"] is None

    page.locator("#terminalComposeBar .composer-mic").click()
    notice = page.locator("#terminalComposeBar .composer-mic-notice")
    expect(notice).to_contain_text(f"Mic: {_LABEL}")
    expect(notice.locator(".rec-wave")).to_be_visible()
    live = page.evaluate(_RECTS_JS)

    # Nothing above the notice moved: not the box, not any button.
    assert _same(before["bar"], live["bar"]), (before["bar"], live["bar"])
    for was, now in zip(before["buttons"], live["buttons"]):
        assert _same(was, now), (was, now)
    # Below the box, on screen, so it covers none of the conversation.
    assert live["notice"]["top"] >= live["bar"]["bottom"] - 0.5
    assert live["notice"]["bottom"] <= live["viewportBottom"] + 0.5
    # One line: the name and the strip share a row, the name on the left.
    name, wave = live["name"], live["wave"]
    assert name["right"] <= wave["left"] + 0.5
    assert abs((name["top"] + name["bottom"]) / 2
               - (wave["top"] + wave["bottom"]) / 2) < 6
    _assert_group_centred(live)

    # The warning takes its own line under that row.
    expect(notice.locator(".mic-notice-silent")).to_be_visible(timeout=6_000)
    warned = page.evaluate(_RECTS_JS)
    assert warned["warning"]["top"] >= warned["wave"]["bottom"] - 0.5
    assert warned["notice"]["bottom"] <= warned["viewportBottom"] + 0.5

    page.locator("#terminalComposeBar .composer-mic").click()
    expect(notice).to_be_hidden()
    after = page.evaluate(_RECTS_JS)
    assert _same(before["bar"], after["bar"]), (before["bar"], after["bar"])


# WebKit cannot build the fake input above, so the phone engine checks the
# layout against the component's own markup, filled in by hand.
_FILL_NOTICE_JS = """
([withWarning, name]) => {
  const n = document.querySelector('#terminalComposeBar .composer-mic-notice');
  n.classList.add('mic-notice');
  n.innerHTML =
    '<div class="mic-notice-input">Mic: ' + name +
    '</div><div class="mic-notice-wave"><div class="rec-wave">' +
    '<div class="rec-wave-bars" style="--rec-wave-height:20px"></div>' +
    '<span class="rec-wave-time">0:07</span></div></div>' +
    '<div class="mic-notice-silent" role="status"' +
    (withWarning ? '' : ' hidden') + '>No sound from the microphone</div>';
  n.hidden = false;
}
"""


_LONG_NAME = "A very long input name " * 6


@pytest.mark.iphone
def test_notice_row_layout_holds_on_the_phone_engine(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """One row under the box, on screen, a long input name ellipsized rather
    than wrapped, the box unmoved; the warning on its own line below."""
    page = authed_page
    _open_terminal(page, base_url, launched_pty_session)
    _open_compose_with_record(page)
    before = page.evaluate(_RECTS_JS)

    page.evaluate(_FILL_NOTICE_JS, [False, _LONG_NAME])
    row = page.evaluate(_RECTS_JS)
    assert _same(before["bar"], row["bar"]), (before["bar"], row["bar"])
    assert row["notice"]["top"] >= row["bar"]["bottom"] - 0.5
    assert row["notice"]["bottom"] <= row["viewportBottom"] + 0.5
    assert row["name"]["right"] <= row["wave"]["left"] + 0.5
    assert row["notice"]["bottom"] - row["notice"]["top"] < 48
    # A long name still fills the row between the two clearances, ellipsized
    # on one line, with the strip and timer kept inside the edge.
    assert row["wave"]["right"] <= row["notice"]["right"] - _CORNER_CLEARANCE + 0.5
    assert row["name"]["left"] >= row["notice"]["left"] + _CORNER_CLEARANCE - 0.5
    assert page.evaluate(
        "(() => { const n = document.querySelector("
        "'#terminalComposeBar .mic-notice-input');"
        " return n.scrollWidth > n.clientWidth"
        " && getComputedStyle(n).textOverflow === 'ellipsis'; })()"
    )
    assert row["name"]["bottom"] - row["name"]["top"] < 28

    # A short name is the centred group, however wide the screen.
    page.evaluate(_FILL_NOTICE_JS, [False, "iPhone Microphone"])
    short = page.evaluate(_RECTS_JS)
    _assert_group_centred(short)

    page.evaluate(_FILL_NOTICE_JS, [True, _LONG_NAME])
    warned = page.evaluate(_RECTS_JS)
    assert warned["warning"]["top"] >= warned["wave"]["bottom"] - 0.5
    assert warned["notice"]["bottom"] <= warned["viewportBottom"] + 0.5
