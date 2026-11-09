"""Regression pin for read-aloud (issues #190 + #197 colour-block overhaul).

The feature: a ``🔊`` top-bar button in the Coding tab speaks the agent's last
reply for eyes-free / driving use. Detection (#197) keys off the bullet the
agent prints to open each block, classified by its xterm cell foreground
COLOUR: a default/white ``●`` is an assistant reply, a coloured (green/red/…)
``●`` is a tool call. The buffer segments into an ordered list of reply blocks;
``🔊`` reads the last by default (a future depth-selector slices the list).
Speaking goes through the Web Speech API; starting a new dictation cancels any
in-flight read-aloud.

``speechSynthesis`` isn't meaningfully available in headless WebKit, so it is
stubbed via an init script that records spoken/cancelled calls. The pure
segmenter ``extractReplyBlocksFromRows`` is pinned under plain Node in
``tests/js/readback_extraction.test.mjs``; a test here writes real ANSI into an
xterm ``Terminal`` to exercise the live cell-colour classifier (``extractReplyBlocks``/``extractLastReply``).
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.fixtures.waits import OVERLAY_OPEN_MS

pytestmark = pytest.mark.smoke

# Stub the Web Speech API before the SPA loads. Records every utterance text
# in `_spoken` and every cancel() in `_cancels` so the test can assert what
# was read and that a re-dictation silenced it. `speak()` flips `speaking`
# and fires onend asynchronously so the drain-complete path runs.
#
# `window.speechSynthesis` is a non-writable accessor on the Window prototype
# (Chromium), so a plain assignment silently no-ops and the native object
# rejects our fake utterance — both globals must be shadowed with
# Object.defineProperty to take effect.
_SPEECH_MOCK = """
(() => {
  const synth = {
    _spoken: [], _cancels: 0, _resumes: 0, speaking: false, pending: false,
    getVoices: () => [],
    speak(u) {
      this._spoken.push(u.text);
      this.speaking = true; this.pending = false;
      if (u.onend) setTimeout(() => { synth.speaking = false; u.onend(); }, 0);
    },
    cancel() { this._cancels += 1; this.speaking = false; this.pending = false; },
    pause() {},
    resume() { this._resumes += 1; },
  };
  Object.defineProperty(window, 'speechSynthesis', {
    value: synth, configurable: true,
  });
  Object.defineProperty(window, 'SpeechSynthesisUtterance', {
    configurable: true, writable: true,
    value: function (text) {
      this.text = text; this.rate = 1; this.lang = ''; this.voice = null;
      this.onend = null; this.onerror = null;
    },
  });
})()
"""

def _open_terminal(page: Page, base_url: str, sid: str) -> None:
    page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")
    page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=OVERLAY_OPEN_MS)
    page.wait_for_function(
        "() => document.getElementById('terminalStatus') "
        "&& document.getElementById('terminalStatus').hidden === true",
        timeout=OVERLAY_OPEN_MS,
    )


@pytest.mark.iphone
def test_toast_sits_above_terminal_overlay(
    authed_page: Page, base_url: str
) -> None:
    """Read-aloud feedback (and every terminal toast) must out-rank the
    full-screen terminal overlay — otherwise it renders behind it and the
    user sees nothing on the phone. A stylesheet fact, so the overlay need not
    be open (and no session launched) to read the two z-indexes (#1515)."""
    authed_page.goto(f"{base_url}/", wait_until="domcontentloaded")
    z = authed_page.evaluate(
        """() => {
          const zi = (el) => parseInt(
            getComputedStyle(el).zIndex || '0', 10) || 0;
          return {
            toast: zi(document.getElementById('toast')),
            overlay: zi(document.getElementById('terminalOverlay')),
          };
        }"""
    )
    assert z["toast"] > z["overlay"], z


_LOAD_XTERM = (
    "async () => (await import('/static/terminal/xterm-loader.js')).loadXterm()"
)


def _load_readback(page: Page, base_url: str) -> None:
    """Land on the SPA until the ``window.__readback`` seam and xterm's global
    ``Terminal`` exist. The extraction tests below feed synthetic rows or write
    into their own Terminal, so they need the page's modules, not a live PTY
    session or the terminal overlay (#954). xterm loads on first terminal
    open, so the page's own loader is called for it here."""
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_function("() => window.__readback")
    page.evaluate(_LOAD_XTERM)


def test_color_path_classifies_bullets_from_real_buffer(
    authed_page: Page, base_url: str
) -> None:
    """The live cell-colour reader, end-to-end: a default/white ● opens an
    assistant block; a green ● opens a tool block. Drives a real xterm Terminal
    so bufferToRows + the colour classifier run, not just the pure segmenter."""
    _load_readback(authed_page, base_url)
    got = authed_page.evaluate(
        r"""() => {
          const term = new window.Terminal({ cols: 80, rows: 30 });
          const host = document.createElement('div');
          document.body.appendChild(host);
          term.open(host);
          const W = (s) => new Promise((r) => term.write(s, r));
          const BULLET = '●';   // ●
          return (async () => {
            await W('\x1b[0m\x1b[39m' + BULLET + ' First reply here.\r\n');  // default ●
            await W('\x1b[0m\x1b[32m' + BULLET + ' Bash(ls -la)\x1b[0m\r\n');// green ●
            await W('  output line\r\n');                                    // tool result
            await W('\x1b[0m\x1b[97m' + BULLET + ' Second reply here.\r\n'); // white ●
            const blocks = window.__readback.extractReplyBlocks(term);
            const last = window.__readback.extractLastReply(term);
            term.dispose();
            host.remove();
            return { blocks, last };
          })();
        }"""
    )
    assert got["blocks"] == ["First reply here.", "Second reply here."]
    assert got["last"] == "Second reply here."


def test_speak_synthesizes_then_cancels(
    authed_page: Page, base_url: str
) -> None:
    """speak() queues per-sentence utterances; cancelSpeech() stops them."""
    authed_page.add_init_script(_SPEECH_MOCK)
    _load_readback(authed_page, base_url)
    result = authed_page.evaluate(
        "() => { window.__readback.speak('Build is green. Merge now.');"
        " return { spoken: window.speechSynthesis._spoken,"
        "          resumes: window.speechSynthesis._resumes }; }"
    )
    # One utterance per sentence.
    assert result["spoken"] == ["Build is green.", "Merge now."]
    # iOS can start the queue paused — speak() must kick it with resume().
    assert result["resumes"] >= 1
    # speak() must NOT pre-cancel when idle (the iOS cancel→speak silence bug);
    # only the explicit cancelSpeech() cancels.
    cancels = authed_page.evaluate(
        "() => { window.__readback.cancelSpeech();"
        " return window.speechSynthesis._cancels; }"
    )
    assert cancels == 1


def test_speech_finish_resets_and_toasts(
    authed_page: Page, base_url: str
) -> None:
    """When the reply finishes reading on its own, the button resets to idle
    and a 'Finished reading' toast confirms it (the iOS onend-unreliability
    fix — the watchdog/onend must finalize, not leave the button stuck)."""
    authed_page.add_init_script(_SPEECH_MOCK)
    _load_readback(authed_page, base_url)
    authed_page.evaluate("() => window.__readback.speak('All done now.')")
    expect(authed_page.locator("#toast")).to_contain_text(
        "Finished reading", timeout=5000
    )
    expect(authed_page.locator("#terminalSpeak")).to_have_attribute(
        "aria-pressed", "false"
    )


def test_new_dictation_cancels_speech(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """Starting a recording silences any in-flight read-aloud (AC #190)."""
    authed_page.add_init_script(_SPEECH_MOCK)
    # Minimal media stub so startRecording() proceeds to the cancel point.
    authed_page.add_init_script(
        """
        (() => {
          navigator.mediaDevices = navigator.mediaDevices || {};
          navigator.mediaDevices.getUserMedia = async () => ({
            getTracks: () => [{ stop: () => {} }],
          });
          class FakeRecorder {
            constructor() { this.state = 'inactive'; this._l = {}; }
            addEventListener(e, c) { this._l[e] = c; }
            start() { this.state = 'recording'; }
            stop() { this.state = 'inactive'; }
          }
          FakeRecorder.isTypeSupported = () => true;
          window.MediaRecorder = FakeRecorder;
        })()
        """
    )
    # Streamed-session create fails fast so the handler doesn't hang on it;
    # the speech cancel happens before any network anyway.
    authed_page.route(
        "**/api/transcribe/sessions",
        lambda route: route.fulfill(status=503, body="nope"),
    )
    _open_terminal(authed_page, base_url, launched_pty_session)
    authed_page.evaluate("document.getElementById('terminalComposeBar').hidden = false")
    expect(authed_page.locator("#terminalComposeBar")).to_be_visible()
    authed_page.evaluate(
        "document.querySelector('#terminalComposeBar .composer-mic').disabled = false"
    )

    authed_page.evaluate("() => window.__readback.speak('Reading this aloud now.')")
    before = authed_page.evaluate("() => window.speechSynthesis._cancels")
    authed_page.locator("#terminalComposeBar .composer-mic").click()
    after = authed_page.evaluate("() => window.speechSynthesis._cancels")
    assert after > before
