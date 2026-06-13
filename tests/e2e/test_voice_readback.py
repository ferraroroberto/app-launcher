"""Regression pin for issue #190 (read the last AI reply aloud).

The feature: a ``🔊`` button in the Coding-tab compose bar speaks the
agent's last reply for eyes-free / driving use. The reply is extracted
client-side from the xterm scrollback — strip the trailing input composer +
UI chrome, then keep the last contiguous block of assistant prose, stopping
at the first tool/user boundary above it (so tool output and spinners are
never read). Speaking goes through the Web Speech API; starting a new
dictation cancels any in-flight read-aloud.

``speechSynthesis`` isn't meaningfully available in headless WebKit, so it
is stubbed via an init script that records spoken/cancelled calls. The
extraction heuristic is pure, so it is driven directly through the
``window.__readback`` test seam with synthetic transcripts — no live PTY
buffer to synthesize.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

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

# A faithful idle Claude Code rendering (built from real captured output at the
# phone's 51-col width): the reply, the "Worked for" timing line, a recap block,
# the composer box, then the status footer (folder/branch, permission mode,
# token count). The reply is column-wrapped with the TUI's blank-line gutter.
# Read-aloud must return ONLY the reply — de-wrapped to one paragraph, with the
# recap, "Worked for", box and footer all stripped.
_IDLE_LINES = [
    "                    > ship it",
    "",
    "  Done — issue #190 is built, verified, and",
    "",
    "  live on your phone. The pre-ship gate is",
    "",
    "  green and the webapp is live on :8445.",
    "",
    "  Worked for 21m 17s",
    "",
    "  recap: Goal: add an eyes-free read-aloud",
    "  button to the Coding tab (issue #190).",
    "  (disable recaps in /config)",
    "",
    "          ─────────────────────────────────",
    "          ──────── voice drive from mobile ──",
    "          > ",
    "          ─────────────────────────────────",
    "  app-launcher (feat/190-read-last-reply-aloud) …",
    "  ⏵⏵ bypass permissions on (shift+tab to cycle)",
    "                              187754 tokens",
]

_EXPECTED = (
    "Done — issue #190 is built, verified, and live on your phone. "
    "The pre-ship gate is green and the webapp is live on :8445."
)

# The exact shape that regressed in the field (20:00 screenshot): a *titled*
# composer border on one line (the title text dilutes the whole-line rule
# ratio), a draft prompt with typed text inside the box, a spinner-prefixed
# "✻ Worked for" line, and the context%/auto-mode/tokens footer. The titled
# border must still be recognised so the box + draft `>` are cut — otherwise
# the draft prompt is mistaken for a user turn and nothing is read.
_TITLED_BOX_LINES = [
    "                    > read the last reply",
    "",
    "  The toast was rendering behind the terminal.",
    "",
    "  Reload and tap the speaker again.",
    "",
    "✻ Crunched for 5s · 1 shell still running",
    "─────────────── voice drive from mobile ───────",
    "> see the Reading toast now",
    "─────────────────────────────────────────────",
    "28% | app-launcher (feat/190-read-last-reply-a…",
    "▶▶ auto mode                          agents",
    "                              30251 tokens",
]
_TITLED_BOX_EXPECTED = (
    "The toast was rendering behind the terminal. "
    "Reload and tap the speaker again."
)

# Mid-work: a spinner + pending tool below the box, no settled reply above it.
# Nothing should be read (better an honest "nothing yet" than the status line).
_RUNNING_LINES = [
    "                    > do the thing",
    "",
    "  ⎿  Waiting…",
    "",
    "  Ruminating… (2m 3s · ↓ 7.2k tokens)",
    "",
    "          ────────────────────────",
    "          > ",
    "          ────────────────────────",
    "  app-launcher (main) …",
    "  Ruminating…  ✶  187754 tokens",
]

# Issue #193 (20:46 screenshot): the live thinking spinner "✻ Cogitating…
# (4m 39s · thinking)" — no token count, so STATUS_RE doesn't catch it — sits
# above a Read tool boundary, with the *previous* turn's real prose further up.
# The spinner must be skipped by shape; the walk then hits the ⎿ boundary and
# returns nothing (mid-work → silent), NOT the spinner text.
_THINKING_SPINNER_LINES = [
    "  Key finding: there's a maintained chatterbox-",
    "",
    "  tts PyPI package with the exact API I need.",
    "",
    "  Read(E:\\automation\\local-llm-hub\\src\\whisper_",
    "  translate_proxy.py)",
    "  ⎿  Read 386 lines",
    "",
    "✻ Cogitating… (4m 39s · thinking)",
    "",
    "          ─────────────────────────────────",
    "          > ",
    "          ─────────────────────────────────",
    "  12% | local-llm-hub (feat/98-tts-backend-audio…",
    "  ▶▶ auto mode on (shift+tab to cycle)",
    "                              127496 tokens",
]


def _open_terminal(page: Page, base_url: str, sid: str) -> None:
    page.goto(f"{base_url}/?terminal={sid}", wait_until="domcontentloaded")
    page.wait_for_selector("#terminalOverlay:not([hidden])", timeout=10_000)
    page.wait_for_function(
        "() => document.getElementById('terminalStatus') "
        "&& document.getElementById('terminalStatus').hidden === true",
        timeout=10_000,
    )


def test_speak_button_in_toolbar(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """The 🔊 button is a top-bar control, between ↓ Jump and 📋 Paste — NOT in
    the compose bar (which is for editing)."""
    _open_terminal(authed_page, base_url, launched_pty_session)
    expect(
        authed_page.locator(".terminal-bar-actions #terminalSpeak")
    ).to_have_count(1)
    expect(authed_page.locator("#terminalComposeBar #terminalSpeak")).to_have_count(0)
    # Document order: ↓ Jump → 🔊 Speak → 📋 Paste.
    order = authed_page.eval_on_selector_all(
        ".terminal-bar-actions .term-btn", "els => els.map(e => e.id)"
    )
    assert order.index("terminalSpeak") == order.index("terminalJumpEnd") + 1
    assert order.index("terminalPaste") == order.index("terminalSpeak") + 1


def test_toast_sits_above_terminal_overlay(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """Read-aloud feedback (and every terminal toast) must out-rank the
    full-screen terminal overlay — otherwise it renders behind it and the
    user sees nothing on the phone."""
    _open_terminal(authed_page, base_url, launched_pty_session)
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


def test_extraction_reads_reply_not_footer_or_recap(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """The heuristic returns the de-wrapped reply, skipping the composer box,
    the status footer (folder/permission/tokens), the recap and "Worked for"."""
    _open_terminal(authed_page, base_url, launched_pty_session)
    got = authed_page.evaluate(
        "(lines) => window.__readback.extractLastReplyFromLines(lines)",
        _IDLE_LINES,
    )
    assert got == _EXPECTED
    # Mid-work (spinner, no settled reply) → nothing to read.
    running = authed_page.evaluate(
        "(lines) => window.__readback.extractLastReplyFromLines(lines)",
        _RUNNING_LINES,
    )
    assert running == ""
    # The live "· thinking" spinner with NO token count (#193) must also be
    # skipped by shape — not read aloud as the reply.
    thinking = authed_page.evaluate(
        "(lines) => window.__readback.extractLastReplyFromLines(lines)",
        _THINKING_SPINNER_LINES,
    )
    assert thinking == ""
    # Titled composer border + a draft prompt inside the box (the 20:00 field
    # regression): the box must be cut so the draft `>` isn't read as a turn.
    titled = authed_page.evaluate(
        "(lines) => window.__readback.extractLastReplyFromLines(lines)",
        _TITLED_BOX_LINES,
    )
    assert titled == _TITLED_BOX_EXPECTED


def test_speak_synthesizes_then_cancels(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """speak() queues per-sentence utterances; cancelSpeech() stops them."""
    authed_page.add_init_script(_SPEECH_MOCK)
    _open_terminal(authed_page, base_url, launched_pty_session)
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
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """When the reply finishes reading on its own, the button resets to idle
    and a 'Finished reading' toast confirms it (the iOS onend-unreliability
    fix — the watchdog/onend must finalize, not leave the button stuck)."""
    authed_page.add_init_script(_SPEECH_MOCK)
    _open_terminal(authed_page, base_url, launched_pty_session)
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
    authed_page.evaluate("document.getElementById('terminalCompose').hidden = false")
    authed_page.locator("#terminalCompose").click()
    expect(authed_page.locator("#terminalComposeBar")).to_be_visible()
    authed_page.evaluate("document.getElementById('terminalRecord').hidden = false")

    authed_page.evaluate("() => window.__readback.speak('Reading this aloud now.')")
    before = authed_page.evaluate("() => window.speechSynthesis._cancels")
    authed_page.locator("#terminalRecord").click()
    after = authed_page.evaluate("() => window.speechSynthesis._cancels")
    assert after > before
