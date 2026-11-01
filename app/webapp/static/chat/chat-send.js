/* Sending from the Chat pane's composer: the message or file out to the
 * agent, the watch that confirms the agent took it, and the overlay pinned
 * above the phone keyboard while Chat owns it.
 */

import { els, state } from '../state.js';
import { apiFailToast, toast } from '../api.js';
import { NOT_PICKED_UP, sendOutcome, sendSessionMessage } from '../sessions.js';
import { CONFIRM_WINDOW_MS, alreadyPending, judgeWatch, startWatch } from './send-confirm.js';
import { visiblePending } from './chat-pending.js';
import { confirmDialog } from '../confirm-dialog.js';
import { keyboardOverlayHeight } from '../terminal.js';
import { uploadSessionFile } from '../terminal/terminal-compose.js';
import { openResumeCard } from './chat-resume.js';
import { pickerAllowed } from './transcript-decisions.js';
import {
  SENT_REFRESH_MS, chatComposer, forwardRead, hasTranscriptReader, liveAllowed, scheduleLive, stopLiveTimer, view,
} from './session-transcript.js';

// iOS shrinks the visual viewport for the software keyboard but not the
// layout viewport, so the fixed inset:0 overlay would keep the composer
// under the keyboard (as in the terminal). Same fix as the terminal pane:
// pin the overlay to the visual viewport while the keyboard is up, and
// release it to the CSS when it hides or the pane closes. Only while the
// chat pane owns the overlay — in Terminal mode terminal.js's applySize
// does the pinning, and the two must never fight over the same styles.
export function pinChatToKeyboard() {
  const o = els.terminalOverlay;
  if (!o) return;
  const chatShowing = !!view && o.dataset.mode === 'chat';
  const vp = window.visualViewport;
  const kbH = (chatShowing && vp) ? keyboardOverlayHeight(window.innerHeight, vp.height) : null;
  if (kbH != null) {
    o.style.height = kbH + 'px';
    o.style.bottom = 'auto';
    o.style.top = Math.round(vp.offsetTop || 0) + 'px';
  } else if (chatShowing || !view) {
    // Release only what this pane may own: with Terminal showing, the
    // terminal's own pin (if any) stands.
    o.style.height = '';
    o.style.bottom = '';
    o.style.top = '';
  }
}

// ➤ Send from Chat: the /input route for either kind. Resolves true to
// clear the draft; a failed send (502 not ingested / console failed, 409
// exited, 501 stale host) toasts and resolves false so the text stays to
// retry. A send that lands after the pane moved to another session toasts
// but leaves that session's composer and list alone.
export async function sendFromChat(text) {
  if (!view) return false;
  // `/resume` opens the searchable resume card instead of Claude
  // Code's scrolling picker, where Chat can read the terminal's screen.
  if (text.trim() === '/resume' && pickerAllowed(view.session)) {
    openResumeCard('composer');
    return true;
  }
  const target = view;
  // The same text already waiting is the double send the queued chip exists
  // to prevent: ask before it goes twice. A "no" keeps the draft.
  if (alreadyPending(visiblePending(), text)) {
    const again = await confirmDialog({
      title: 'Already queued',
      message: 'This message is already waiting for the agent. Send it again?',
      action: 'Send again',
      tone: 'primary',
    });
    if (!again || view !== target) return false;
  }
  // Counted before the request: an agent quick enough to log the turn while
  // the answer is still on the wire must not be mistaken for an older one.
  const watch = startWatch(
    text, target.entries, target.activity && target.activity.working, Date.now()
  );
  let verdict;
  try {
    verdict = await sendSessionMessage(target.session.session_id, text);
  } catch (exc) {
    apiFailToast('Send failed', exc);
    return false;
  }
  const outcome = sendOutcome(verdict, { watch: hasTranscriptReader(target.session) });
  toast(outcome.text, outcome.kind, { icon: 'send-horizontal' });
  if (view !== target) return false;
  if (outcome.watch) armSendWatch(target, watch);
  window.clearTimeout(target.refreshTimer);
  target.refreshTimer = window.setTimeout(function () {
    // One extra tick, not a reload: the sent turn arrives as an append like
    // any other, so the pane keeps its scroll position and open cards
    // Brought forward rather than waiting out the current interval,
    // and still subject to the same gate — a send followed by a switch to
    // Terminal fetches nothing.
    if (view === target) scheduleLive(0);
  }, SENT_REFRESH_MS);
  return true;
}

// Look for evidence the agent took an unconfirmed send. The look is
// the live tick's own read: `checkSendWatch` judges after each one, and a
// timer at the deadline forces a read so a quiet session still gets its
// verdict. A pane that cannot read then (Terminal showing, phone locked) keeps
// the watch for the first read that can, which is judged against the full
// window, not skipped.
function armSendWatch(target, watch) {
  window.clearTimeout(target.watchTimer);
  watch.deadline = Date.now() + CONFIRM_WINDOW_MS;
  target.sendWatch = watch;
  target.watchTimer = window.setTimeout(function () { closeSendWatch(target); }, CONFIRM_WINDOW_MS);
}

async function closeSendWatch(target) {
  if (view !== target || !target.sendWatch) return;
  if (target.ticking && target.inflight) await target.inflight;
  if (view !== target || !target.sendWatch || !liveAllowed()) return;
  stopLiveTimer();
  await forwardRead(target, false);
}

export function checkSendWatch(target) {
  const watch = target.sendWatch;
  if (!watch) return;
  const verdict = judgeWatch(
    watch, target.entries || [], target.activity && target.activity.working, Date.now()
  );
  if (verdict === 'waiting') return;
  target.sendWatch = null;
  window.clearTimeout(target.watchTimer);
  if (verdict !== 'not-picked-up') return;
  toast(NOT_PICKED_UP.text, NOT_PICKED_UP.kind, { icon: 'send-horizontal' });
  if (chatComposer) chatComposer.restoreDraft(watch.sent);
}

export function uploadFromChat(file, signal, kind) {
  return uploadSessionFile(view ? view.session.session_id : null, file, signal, kind);
}

// The live terminal socket of the session Chat is showing, if one is open.
// Opening straight into Chat connects nothing, so the keys have no
// socket until Terminal has been shown once.
export function liveTerminalFor(s) {
  const t = state.terminal;
  if (!t || t.sid !== s.session_id || !t.ws || t.ws.readyState !== WebSocket.OPEN) {
    return null;
  }
  return t;
}
