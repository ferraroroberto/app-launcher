/* Read-aloud UI orchestration for the live terminal (issue #315 — finishes
 * the split terminal-readback.js started): the 🔊 button, its action
 * popover, the summary modal, and the two user-facing actions ("Read
 * aloud" verbatim / "Summarize & read"). terminal-readback.js stays the
 * low-level engine (buffer extraction, Web Speech, hub streaming); this
 * module is the DOM-facing layer wired onto it.
 *
 * The 🔊 button opens a small dropdown with two actions: "Read aloud" (the
 * verbatim path, #190/#203/#206) and "Summarize & read" — condense the reply
 * via the hub's claude-haiku-4-5 first, for hands-free / driving listening. The
 * menu only appears when the summarize action is available (hub reachable);
 * otherwise the button keeps its original single-tap "read aloud" behaviour.
 */

import { els, state } from './state.js';
import { apiFailToast, readToken, showLogin, toast } from './api.js';
import { bindOutsideClickToClose } from './dom-utils.js';
import { readTerminalToken } from './webauthn.js';
import { icon } from './_vendored/icons/icons.js';
import {
  cancelHub,
  cancelSpeech,
  extractLastReply,
  isHubAvailable,
  isSpeaking,
  isSpeechSupported,
  onSpeakingChange,
  onSpeechEnd,
  prepareHub,
  probeHub,
  speak,
  speakHub,
  speakHubInto,
  summarizeReply,
} from './terminal-readback.js';

// Stop read-aloud whichever voice is active — the hub <audio> and the Web
// Speech queue are independent engines sharing one speaking-state flag, so a
// stop (re-press, tab-leave, new dictation) must silence both.
export function stopReading() {
  cancelHub();
  cancelSpeech();
}

// Reflect speaking state on the 🔊 button: ⏹ + pulse while reading, 🔊 idle.
function setSpeakingUI(on) {
  // Auto-close the summary modal (issue #210) the moment reading stops —
  // whether it finished naturally, was stopped, or the tab was left. This is
  // the single sink for every speaking→idle transition, so the modal can't
  // outlive the read.
  if (!on) closeSummaryModal();
  const btn = els.terminalSpeak;
  if (!btn) return;
  btn.classList.toggle('speaking', on);
  btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  btn.innerHTML = on ? icon('square') : icon('volume-2');
  btn.title = on ? 'Stop reading' : 'Read the last reply aloud';
}

// ── Summary modal (issue #210) ──────────────────────────────────────────────
// Shows the hub summary on screen while it's read aloud — and readable on its
// own when audio is muted. Auto-closes when the read ends (via setSpeakingUI);
// a tap anywhere (or the ✕) dismisses it and stops the read.
function showSummaryModal(text) {
  if (!els.summaryModal) return;
  els.summaryModalText.textContent = text;
  els.summaryModal.hidden = false;
}

function closeSummaryModal() {
  if (!els.summaryModal || els.summaryModal.hidden) return;
  els.summaryModal.hidden = true;
}

function wireSummaryModal() {
  if (!els.summaryModal) return;
  // Tap the backdrop (or ✕) → dismiss + stop reading. stopReading() flips the
  // speaking state, which closeSummaryModal()s via setSpeakingUI as a backstop.
  els.summaryModal.addEventListener('click', function () {
    stopReading();
    closeSummaryModal();
  });
}

// ── Read-aloud action menu (issue #210) ─────────────────────────────────────
let _disposeSpeakOutsideClick = null;

export function closeSpeakPopover() {
  if (!els.terminalSpeakPopover) return;
  els.terminalSpeakPopover.hidden = true;
  if (_disposeSpeakOutsideClick) {
    _disposeSpeakOutsideClick();
    _disposeSpeakOutsideClick = null;
  }
}

function openSpeakPopover() {
  if (!els.terminalSpeakPopover) return;
  els.terminalSpeakPopover.hidden = false;
  if (!_disposeSpeakOutsideClick) {
    _disposeSpeakOutsideClick = bindOutsideClickToClose(
      els.terminalSpeakPopover, els.terminalSpeak, closeSpeakPopover
    );
  }
}

// True when the "Summarize & read" action is currently offered (hub reachable).
function summarizeAvailable() {
  const a = els.terminalSpeakPopover &&
    els.terminalSpeakPopover.querySelector('[data-action="summarize"]');
  return !!(a && !a.hidden);
}

// Speak `text` aloud, preferring the hub's Orpheus voice and falling back to
// on-device Web Speech. `handle` is an optional pre-armed hub context from
// prepareHub() — the summarize path arms it inside the click gesture, then
// awaits the summary, then reads into it (so iOS still lets the audio sound
// after the network round-trip). The verbatim path passes none; speakHub then
// arms its own context inside this call's synchronous prologue.
async function readTextAloud(text, handle, quiet) {
  const peek = text.length > 60 ? text.slice(0, 60) + '…' : text;
  const opts = { token: readToken(), terminalToken: readTerminalToken() };
  // `quiet` suppresses the peek toast — the summarize path already shows the
  // full text in the modal, so the toast would be redundant noise.
  if (handle || isHubAvailable()) {
    if (!quiet) toast('🔊 Reading: ' + peek, 'good');
    try {
      if (handle) await speakHubInto(handle, text, opts);
      else await speakHub(text, opts);
      return true;
    } catch (exc) {
      // A stale session (issue #333): reopen the login overlay, but still
      // fall through to Web Speech below — losing the nicer hub voice is a
      // fair trade for not breaking read-aloud outright on an expired token.
      if (exc && exc.status === 401) showLogin();
    }
  }
  if (!speak(text)) {
    toast('Speech not supported on this browser', 'error');
    return false;
  }
  if (!quiet) toast('🔊 Reading: ' + peek, 'good');
  return true;
}

// "Read aloud": extract the last reply and speak it verbatim. Must be called
// directly from the button-click gesture (speakHub arms audio synchronously).
function readLastReplyAloud() {
  const t = state.terminal;
  if (!t || !t.term) return;
  const text = extractLastReply(t.term);
  if (!text) { toast('🔊 No reply to read yet.'); return; }
  readTextAloud(text, null);
}

// "Summarize & read": condense the last reply via the hub, then speak the
// summary. Arms the hub audio context in the gesture tick (before the awaited
// summary) so iOS lets the synthesized summary sound. Must be called directly
// from the button-click gesture.
async function summarizeAndReadLastReply() {
  const t = state.terminal;
  if (!t || !t.term) return;
  const text = extractLastReply(t.term);
  if (!text) { toast('🔊 No reply to read yet.'); return; }
  const opts = { token: readToken(), terminalToken: readTerminalToken() };
  // Arm hub audio now, in the gesture, so the summary can sound after the LLM
  // round-trip; null when Web Audio is unavailable → Web Speech fallback.
  let handle = null;
  if (isHubAvailable()) {
    try { handle = prepareHub(); } catch (_) { handle = null; }
  }
  toast('📝 Summarizing…');
  let summary;
  try {
    summary = await summarizeReply(text, opts);
  } catch (exc) {
    if (handle) cancelHub();   // release the armed context + reset the button
    // Routes a stale-session 401 to showLogin() instead of a generic toast
    // (issue #333) — apiFailToast() already treats err.status === 401 that
    // way; any other failure still gets this friendly prefixed message.
    apiFailToast('Could not summarize the reply', exc);
    return;
  }
  // Show the summary on screen (readable on its own when audio is muted); it
  // auto-closes when the read finishes — or stays until dismissed if silent.
  showSummaryModal(summary);
  await readTextAloud(summary, handle, true);
}

function wireSpeakMenu() {
  // Idle press: open the action menu when summarize is available, else read
  // aloud directly (preserve the original single-tap behaviour). Press while
  // reading: stop. The tap is the user gesture iOS speech needs, so the read
  // helpers run synchronously off this handler.
  els.terminalSpeak.addEventListener('click', function () {
    if (isSpeaking()) { stopReading(); closeSpeakPopover(); return; }
    if (!summarizeAvailable()) { readLastReplyAloud(); return; }
    if (els.terminalSpeakPopover.hidden) openSpeakPopover();
    else closeSpeakPopover();
  });
  els.terminalSpeakPopover.addEventListener('click', function (ev) {
    const btn = ev.target.closest('.speak-action');
    if (!btn) return;
    const action = btn.getAttribute('data-action');
    closeSpeakPopover();
    if (action === 'summarize') summarizeAndReadLastReply();
    else readLastReplyAloud();
  });
}

// Reveal/hide the 🔊 button + "Summarize" menu action for the session just
// opened (called from openTerminal). `state.status.tts` is the cheap
// config-presence flag; the live /api/tts/health probe then refines which
// path the click takes — summarize only ever appears once the hub is
// confirmed reachable.
export function revealReadAloudButton() {
  const ttsConfigured = !!(state.status && state.status.tts);
  els.terminalSpeak.hidden = !(isSpeechSupported() || ttsConfigured);
  const summarizeAction = els.terminalSpeakPopover &&
    els.terminalSpeakPopover.querySelector('[data-action="summarize"]');
  if (summarizeAction) summarizeAction.hidden = true;
  probeHub({
    token: readToken(),
    terminalToken: readTerminalToken(),
  }).then(function (ok) {
    // A reachable hub means the button is useful even where Web Speech
    // isn't supported (e.g. some embedded WebViews), and unlocks summarize.
    if (ok) els.terminalSpeak.hidden = false;
    if (summarizeAction) summarizeAction.hidden = !ok;
  });
}

// Single wiring entrypoint for terminal.js's wireTerminal(): the 🔊 control
// (issues #190, #210), its speaking-state sync, and the summary modal.
export function wireReadAloud() {
  onSpeakingChange(setSpeakingUI);
  // When the reply finishes reading on its own, reset the button (done by
  // setSpeakingUI(false) via onSpeakingChange) and confirm with a toast.
  onSpeechEnd(function () { toast('🔊 Finished reading.', 'good'); });
  wireSpeakMenu();
  wireSummaryModal();
}
