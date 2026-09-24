/* The live terminal's composer transport (issue #315 split off terminal.js;
 * #980 moved the composer itself into composer.js).
 *
 * composer.js owns the surface — textarea, mic, keys, image menu, OCR tray,
 * send — and knows nothing about sessions. This module is the terminal's
 * binding of it: ➤ Send forwards the buffered text over the PTY WebSocket,
 * then a submitting \r as a SEPARATE WS frame (sendSubmit / #166); attach
 * uploads through the session-host's inline image route and hands the
 * composer the stored path; the ⌨ D-pad writes escape bytes down the same
 * `input` channel. The compose bar was born here (#37) as a normal
 * <textarea> with default predictive/autocorrect so iOS/Android keyboards
 * offer suggestions — which they can't inside xterm's per-keystroke-wiped
 * helper textarea — and since #980 it is always docked: no ✏️ toggle, the
 * PC mirror window is the only place it hides (terminal.js).
 */

import { els, state } from './state.js';
import { apiFailToast, apiRaw } from './api.js';
import { readTerminalToken } from './webauthn.js';
import { mountComposer } from './composer.js';
import { stopReading } from './terminal-readaloud.js';

// Attachment settle (issue #1211, replacing #450's fixed 350 ms defer): a
// CR that reaches Claude Code while it is still turning a pasted image path
// into an attachment is absorbed, and the prompt sits unsent in its composer
// until someone presses Enter again. The conversion is not a fixed cost: it
// paints "Pasting…" at once and the "[Image #N]" chip only when done,
// measured at ~200 ms for a 1.5 MB PNG, ~340 ms for 7.7 MB and ~400 ms for
// 11 MB on an idle box, so a full-resolution photo pasted from the phone's
// clipboard outran the fixed defer. The CR now waits on the bulk watch below
// and, while a "Pasting…" has no chip after it, keeps waiting however quiet
// the stream is (terminal-connection.js stamps both). An agent that never
// paints either marker echoes the path as text and settles on the plain
// echo-then-quiet rule. The cap is longer than the bulk one because the
// conversion scales with the image and with machine load; a CR sent at the
// cap is no worse than the old fixed defer.
const _ATTACH_CAP_MS = 10000;

// Bulk-text CR settle watch (issue #499): a dictation-sized paste can outrun
// Claude Code's bracketed-paste ingest when the machine is under load
// (concurrent PTY sessions, gates, browsers — #493 measured 5× latency
// spikes), so the immediately-following CR lands mid-ingest and becomes a
// newline into the still-settling composer instead of Submit. For payloads
// past the threshold, the CR is held until the session's output stream shows
// the paste was ingested AND settled: some output arrived after the send
// (Claude echoes the paste / collapses it into a "[Pasted text #N]" chip)
// and that output has been quiet for _BULK_QUIET_MS — with a floor (never
// sooner than _BULK_FLOOR_MS) and a cap (send anyway at _BULK_CAP_MS).
// Calibrated with a real-Claude ConPTY probe under synthetic load (#499):
// immediate CR submitted 1/20, fixed 350 ms 19/20, fixed 1000 ms 19/20, a
// bare quiet-window 13/20 (under load the echo itself is deferred, so it
// fires early) — this echo-then-quiet protocol was the only one to run
// clean 20/20. Short sends stay instant. Threshold: dictations that
// reproduced the swallow are ~1–2 KB; typed prompts stay well under this.
const _BULK_SUBMIT_THRESHOLD_CHARS = 500;
const _BULK_FLOOR_MS = 350;
const _BULK_QUIET_MS = 350;
const _BULK_CAP_MS = 3000;

// The terminal's mounted composer handle (composer.js), set once by
// mountTerminalComposer() from wireTerminal(). Live binding: importers read
// it after wiring.
export let terminalComposer = null;

// Wrap a clipboard / compose payload in bracketed-paste markers (DECSET
// 2004) when the agent's TUI has them enabled, so it buffers the whole
// block as one atomic paste instead of absorbing a per-keystroke burst —
// which the Windows console input queue silently drops spans of under a
// multi-KB load (#64). This is exactly what xterm already does for its own
// native paste (term.onData); compose ➤ Send bypasses xterm, so it has to
// replicate it. Only bracket when the app actually asked for it
// (`term.modes.bracketedPasteMode`) — otherwise the literal `\x1b[200~`
// would land as garbage in an agent that doesn't grok it.
//
// Framing only — this never appends the submitting carriage return. A
// submit goes through `sendSubmit`, which delivers the CR as its OWN WS
// frame after this block (see #166).
export function framePaste(t, text) {
  const bracketed = !!(t.term && t.term.modes && t.term.modes.bracketedPasteMode);
  if (!bracketed) return text;
  return '\x1b[200~' + text + '\x1b[201~';
}

// Send a composed prompt to the PTY and submit it. The submitting carriage
// return is sent as its OWN WS frame *after* the (possibly bracketed) text
// block — never concatenated onto it.
//
// Why split: the webapp proxies each WS `input` frame to the session-host
// as a distinct `pty.write()`, so two frames become two PTY writes. That
// guarantees the `\x1b[201~` paste-end marker is written — and the TUI has
// finished exiting bracketed-paste mode — before the bare CR arrives. When
// the CR rode in the same frame as the end marker, the TUI intermittently
// absorbed it into paste finalization instead of running the prompt: the
// "➤ Send sometimes does nothing" race of #166. A CR *inside* the markers
// is literal pasted text by design, so the split is the only ordering that
// reliably submits. With bracketed mode off there is no paste state machine
// to race, but the two-frame path is harmless there, so it stays uniform.
// `opts.bulkSettle` (issue #499): hold the CR until the session's output
// stream shows the paste was ingested and settled (echo seen after the send,
// then quiet — floor/quiet/cap constants above). Used for dictation-sized
// text payloads, whose ingest under machine load outlives any fixed delay.
// `opts.attachSettle` (issue #1211): the same watch for a payload carrying
// an attached-file path, which also waits out an image conversion still in
// flight, up to _ATTACH_CAP_MS (see that constant).
// Short plain-text sends pass no options and stay instant (the CR still
// goes as its own frame, preserving #166's ordering fix).
export function sendSubmit(t, text, opts) {
  if (!t || !t.ws || t.ws.readyState !== WebSocket.OPEN) return;
  const sentAt = Date.now();
  t.ws.send(JSON.stringify({ type: 'input', data: framePaste(t, text) }));
  const submit = function () {
    if (t.ws && t.ws.readyState === WebSocket.OPEN) {
      t.ws.send(JSON.stringify({ type: 'input', data: '\r' }));
    }
  };
  if (!opts || !(opts.bulkSettle || opts.attachSettle)) {
    submit();
    return;
  }
  const cap = opts.attachSettle ? _ATTACH_CAP_MS : _BULK_CAP_MS;
  const watch = setInterval(function () {
    const now = Date.now();
    const converting = t.attachStartAt > sentAt
      && !(t.attachChipAt >= t.attachStartAt);
    const settled = now - sentAt >= _BULK_FLOOR_MS
      && t.lastOutputAt > sentAt
      && now - t.lastOutputAt >= _BULK_QUIET_MS
      && !converting;
    if (settled || now - sentAt >= cap) {
      clearInterval(watch);
      submit();
    }
  }, 50);
}

// Store one attachment on the session-host for session `sid` and resolve its
// path. `inline=1` asks the host to skip its paste-into-PTY step and just
// return the stored path (#41), so the composer can append it for
// review-before-send. A terminal can't hold an image: the agent is handed
// the *file path* and reads the image from there. Always inline, so it
// serves a detached session too (#983) — the host saves under the session's
// project_dir and never writes to it. Errors toast here and resolve null so
// the composer's batch loop counts only the files that landed (#448).
export async function uploadSessionFile(sid, file) {
  if (!sid || !file) return null;
  const fd = new FormData();
  fd.append('file', file, file.name || 'image.png');
  try {
    const res = await apiRaw(
      '/api/claude-code/sessions/' + encodeURIComponent(sid) + '/image?inline=1',
      { method: 'POST', terminalToken: readTerminalToken(), body: fd }
    );
    if (!res.ok) {
      const b = await res.json().catch(function () { return null; });
      throw new Error((b && b.detail) || ('HTTP ' + res.status));
    }
    const body = await res.json().catch(function () { return null; });
    return (body && body.path) || null;
  } catch (exc) {
    apiFailToast('Image failed', exc);
    return null;
  }
}

function uploadTerminalImage(file) {
  const t = state.terminal;
  return t ? uploadSessionFile(t.sid, file) : Promise.resolve(null);
}

// sendSubmit's options for one composed send. #499: bulk text (a long
// dictation) holds the CR until the paste's ingest visibly settles — under
// machine load a fixed defer still lands mid-ingest and the CR becomes a
// newline instead of Submit. #1211: an attached-file path holds it until the
// agent has finished converting the image, however short the buffer.
export function submitOptions(text, meta) {
  if (meta && meta.hasImage) return { attachSettle: true };
  if (text.length >= _BULK_SUBMIT_THRESHOLD_CHARS) return { bulkSettle: true };
  return undefined;
}

function sendComposed(text, meta) {
  const t = state.terminal;
  if (!t || !t.ws || t.ws.readyState !== WebSocket.OPEN) return false;
  sendSubmit(t, text, submitOptions(text, meta));
  return true;
}

function sendKeyBytes(bytes) {
  const t = state.terminal;
  if (t && t.ws && t.ws.readyState === WebSocket.OPEN) {
    t.ws.send(JSON.stringify({ type: 'input', data: bytes }));
  }
  if (t && t.term) t.term.focus();
}

function snapToTail() {
  // Opening the D-pad means the user is about to drive a prompt, which
  // lives at the tail — snap to the bottom like the ↓ button.
  const t = state.terminal;
  if (t && t.term) { try { t.term.scrollToBottom(); } catch (_) {} }
}

// Mount the shared composer into the terminal overlay, bound to the live
// PTY session. Called once from wireTerminal(); the mirror rule and the
// per-open availability sync (dictation / OCR) live in openTerminal.
export function mountTerminalComposer() {
  terminalComposer = mountComposer(els.terminalComposeBar, {
    placeholder: 'Type a prompt — predictive on.',
    send: sendComposed,
    upload: uploadTerminalImage,
    keys: { send: sendKeyBytes, onOpen: snapToTail },
    // Starting to talk silences any in-flight read-aloud (issue #190):
    // you're answering, not still listening.
    onDictationStart: stopReading,
  });
  return terminalComposer;
}

// Leave-terminal teardown (stashActiveTerminal / disposeTerminal in
// terminal.js): drop the draft, staged screenshots and any in-flight
// recording (#755) so a re-open never shows a stale bar.
export function resetComposeBar() {
  if (terminalComposer) terminalComposer.reset();
}
