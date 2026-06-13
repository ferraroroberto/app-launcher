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
segmenter ``extractReplyBlocksFromRows`` is driven through the
``window.__readback`` seam with synthetic ``{text, marker}`` rows; a separate
test writes real ANSI into an xterm ``Terminal`` to exercise the live
cell-colour classifier (``extractReplyBlocks``/``extractLastReply``).
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

# The extraction core (#197) is the pure segmenter `extractReplyBlocksFromRows`,
# which takes `{text, marker}` rows — `marker` is what the live cell-colour
# reader derives per line: 'assistant' (default/white ● bullet), 'tool'
# (coloured ● bullet) or 'none'. `_rows` builds them tersely from (marker, text)
# tuples; 'a'/'t'/'n' abbreviate the three markers.
_M = {"a": "assistant", "t": "tool", "n": "none"}


def _rows(*pairs):
    return [{"text": text, "marker": _M[m]} for (m, text) in pairs]


# A faithful idle Claude Code rendering (real captured output at the phone's
# 51-col width): the assistant reply (one ● block), the "Worked for" timing
# line, a recap block, the composer box, then the status footer. Read-aloud
# must return ONLY the reply — de-wrapped to one paragraph, with the leading ●
# stripped and the recap, "Worked for", box and footer all dropped.
_IDLE_LINES = _rows(
    ("n", "                    > ship it"),
    ("n", ""),
    ("a", "● Done — issue #190 is built, verified, and"),
    ("n", "  live on your phone. The pre-ship gate is"),
    ("n", "  green and the webapp is live on :8445."),
    ("n", ""),
    ("n", "  Worked for 21m 17s"),
    ("n", ""),
    ("n", "  recap: Goal: add an eyes-free read-aloud"),
    ("n", "  button to the Coding tab (issue #190)."),
    ("n", "  (disable recaps in /config)"),
    ("n", ""),
    ("n", "          ─────────────────────────────────"),
    ("n", "          ──────── voice drive from mobile ──"),
    ("n", "          > "),
    ("n", "          ─────────────────────────────────"),
    ("n", "  app-launcher (feat/190-read-last-reply-aloud) …"),
    ("n", "  ⏵⏵ bypass permissions on (shift+tab to cycle)"),
    ("n", "                              187754 tokens"),
)

_EXPECTED = (
    "Done — issue #190 is built, verified, and live on your phone. "
    "The pre-ship gate is green and the webapp is live on :8445."
)

# The exact shape that regressed in the field (20:00 screenshot): a *titled*
# composer border on one line (the title text dilutes the whole-line rule
# ratio), a draft prompt with typed text inside the box, a spinner-prefixed
# "✻ Worked for" line, and the context%/auto-mode/tokens footer. The titled
# border must still be recognised so the box + draft `>` are cut, and the
# timing line truncates the block — otherwise the draft prompt or "Crunched"
# leaks into the read.
_TITLED_BOX_LINES = _rows(
    ("n", "                    > read the last reply"),
    ("a", "● The toast was rendering behind the terminal."),
    ("n", "  Reload and tap the speaker again."),
    ("n", ""),
    ("n", "✻ Crunched for 5s · 1 shell still running"),
    ("n", "─────────────── voice drive from mobile ───────"),
    ("n", "> see the Reading toast now"),
    ("n", "─────────────────────────────────────────────"),
    ("n", "28% | app-launcher (feat/190-read-last-reply-a…"),
    ("n", "▶▶ auto mode                          agents"),
    ("n", "                              30251 tokens"),
)
_TITLED_BOX_EXPECTED = (
    "The toast was rendering behind the terminal. "
    "Reload and tap the speaker again."
)

# Mid-work with NO prior reply: a spinner + pending tool, no ● assistant block
# anywhere. Nothing should be read (better an honest "nothing yet").
_RUNNING_LINES = _rows(
    ("n", "                    > do the thing"),
    ("n", ""),
    ("n", "  ⎿  Waiting…"),
    ("n", ""),
    ("n", "  Ruminating… (2m 3s · ↓ 7.2k tokens)"),
    ("n", ""),
    ("n", "          ────────────────────────"),
    ("n", "          > "),
    ("n", "          ────────────────────────"),
    ("n", "  app-launcher (main) …"),
    ("n", "  Ruminating…  ✶  187754 tokens"),
)

# Issue #193 (20:46 screenshot): the live thinking spinner "✻ Cogitating…
# (4m 39s · thinking)" sits below a green ● Read tool, with the *previous*
# turn's real ● reply above. The spinner carries no bullet, so it never opens a
# block and is never read. Under the colour-block model the last assistant block
# IS that prior reply — so read-aloud now speaks the last completed reply rather
# than going silent mid-work (the #193 guarantee — never the spinner — holds).
_THINKING_SPINNER_LINES = _rows(
    ("a", "● Key finding: there's a maintained chatterbox-"),
    ("n", "  tts PyPI package with the exact API I need."),
    ("n", ""),
    ("t", "● Read(E:\\automation\\local-llm-hub\\src\\whisper_"),
    ("n", "  translate_proxy.py)"),
    ("n", "  ⎿  Read 386 lines"),
    ("n", ""),
    ("n", "✻ Cogitating… (4m 39s · thinking)"),
    ("n", ""),
    ("n", "          ─────────────────────────────────"),
    ("n", "          > "),
    ("n", "          ─────────────────────────────────"),
    ("n", "  12% | local-llm-hub (feat/98-tts-backend-audio…"),
    ("n", "  ▶▶ auto mode on (shift+tab to cycle)"),
    ("n", "                              127496 tokens"),
)
# The column wrap split "chatterbox-tts" as "chatterbox-" / "tts"; de-wrapping
# joins lines with a space (a raw TUI can't tell a soft wrap-hyphen from a real
# one), so the spoken text carries the seam — an accepted de-wrap limitation.
_THINKING_SPINNER_EXPECTED = (
    "Key finding: there's a maintained chatterbox- tts PyPI package with the "
    "exact API I need."
)


# Issue #195 (21:12 screenshot): the agent is mid-work and the live spinner
# "· Processing… (15m 11s · ↓ 64.6k tokens)" renders a randomised help line as a
# tool-result child — "⎿  Tip: Running multiple Claude sessions? Use /color and
# /rename …". The tip carries no bullet, so it is part of the last ● block's
# trailing continuation; the block must truncate at the spinner so the REAL
# reply ("● Everything's wired. … update the README.") is read, not the tip —
# with its leading "●" turn-marker stripped.
_TIP_SPINNER_LINES = _rows(
    ("n", "  ⎿  Allowed by auto mode classifier"),
    ("n", ""),
    ("a", "● Everything's wired. Now docs — the README is"),
    ("n", "  explicitly part of what you asked for (\"put into"),
    ("n", "  the readme how to connect and consume from other"),
    ("n", "  apps\"). Let me write docs/add-tts.md and update"),
    ("n", "  the README."),
    ("n", ""),
    ("n", "· Processing… (15m 11s · ↓ 64.6k tokens)"),
    ("n", "  ⎿  Tip: Running multiple Claude sessions? Use"),
    ("n", "     /color and /rename to tell them apart at a"),
    ("n", "     glance."),
    ("n", ""),
    ("n", "          ─────────────────────────────────"),
    ("n", "          > "),
    ("n", "          ─────────────────────────────────"),
    ("n", "  23% | local-llm-hub (feat/98-tts-backend-audio…"),
    ("n", "  ▶▶ auto mode on (shift+tab to cycle)"),
    ("n", "                              225377 tokens"),
)
_TIP_SPINNER_EXPECTED = (
    "Everything's wired. Now docs — the README is explicitly part of what you "
    "asked for (\"put into the readme how to connect and consume from other "
    "apps\"). Let me write docs/add-tts.md and update the README."
)


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


def _last_reply(page: Page, rows) -> str:
    """Last reply block from the pure segmenter (== extractLastReply's value)."""
    return page.evaluate(
        "(rows) => { const b = window.__readback.extractReplyBlocksFromRows(rows);"
        " return b.length ? b[b.length - 1] : ''; }",
        rows,
    )


def test_extraction_reads_reply_not_footer_or_recap(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """The segmenter returns the de-wrapped last ● block, dropping the composer
    box, the status footer (folder/permission/tokens), the recap, "Worked for",
    the live spinner and its "Tip:" hint."""
    _open_terminal(authed_page, base_url, launched_pty_session)
    assert _last_reply(authed_page, _IDLE_LINES) == _EXPECTED
    # No ● assistant block anywhere (only a spinner / pending tool) → nothing.
    assert _last_reply(authed_page, _RUNNING_LINES) == ""
    # The live "· thinking" spinner (#193) carries no bullet — it is never a
    # block; the last completed reply above it is read instead.
    assert _last_reply(authed_page, _THINKING_SPINNER_LINES) == (
        _THINKING_SPINNER_EXPECTED
    )
    # The live spinner's "⎿ Tip:" hint (#195) is trailing continuation of the
    # last ● block — the block truncates at the spinner so the real reply is
    # read, with the leading "●" turn-marker stripped.
    assert _last_reply(authed_page, _TIP_SPINNER_LINES) == _TIP_SPINNER_EXPECTED
    # Titled composer border + a draft prompt inside the box (the 20:00 field
    # regression): the box must be cut and the "Crunched for" timing line must
    # truncate the block.
    assert _last_reply(authed_page, _TITLED_BOX_LINES) == _TITLED_BOX_EXPECTED


def test_extraction_returns_ordered_block_list(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """The segmenter exposes every ● reply in order (the seam the future
    "read last N" depth-selector slices, #197) — tool blocks excluded."""
    _open_terminal(authed_page, base_url, launched_pty_session)
    rows = _rows(
        ("a", "● First reply."),
        ("t", "● Bash(ls)"),
        ("n", "  ⎿  ok"),
        ("a", "● Second reply, wrapped"),
        ("n", "  onto two lines."),
        ("a", "● Third reply."),
    )
    blocks = authed_page.evaluate(
        "(rows) => window.__readback.extractReplyBlocksFromRows(rows)", rows
    )
    assert blocks == [
        "First reply.",
        "Second reply, wrapped onto two lines.",
        "Third reply.",
    ]


def test_color_path_classifies_bullets_from_real_buffer(
    authed_page: Page, base_url: str, launched_pty_session: str
) -> None:
    """The live cell-colour reader, end-to-end: a default/white ● opens an
    assistant block; a green ● opens a tool block. Drives a real xterm Terminal
    so bufferToRows + the colour classifier run, not just the pure segmenter."""
    _open_terminal(authed_page, base_url, launched_pty_session)
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
