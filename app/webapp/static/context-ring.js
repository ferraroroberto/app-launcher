/* Context ring in the session overlay's bar (#1223).
 *
 * How full the open session's context window is, as a ring whose border
 * fills with the percentage, just before the Terminal ⇄ Chat toggle. A tap
 * toasts the number. The value is the one Claude Code itself reports: the
 * server reads the fleet statusline's `NN%c` off the session's terminal
 * screen (GET /api/claude-code/sessions/{sid}/context, the same screen read
 * as Chat's plan picker). Nothing on disk names a context-window size, so
 * there is no second source to fall back on.
 *
 * Shown only while the number is known. A detached or non-Claude session
 * has no screen to read and is never asked; an answer without a number
 * (no statusline on screen, Claude Code's null early on and right after
 * /compact) or a failed request hides the ring rather than drawing 0%.
 *
 * Polled on its own route, never the transcript, so a window sitting on
 * Terminal still fetches no chat (#1050): once on open and on every mode
 * switch (session-overlay.js calls syncContextRing), then every
 * CONTEXT_POLL_MS while the overlay is open and the page is visible.
 */

import { els, state } from './state.js';
import { jsonApi, toast } from './api.js';
import { usageTier } from './dom-utils.js';
import { bindLongPressHint } from './long-press-hint.js';

const CONTEXT_POLL_MS = 10000;
// Matches the <circle r="9"> in index.html. The dash length is computed
// rather than set through pathLength, which older iOS WebKit ignores for
// stroke-dasharray.
const RING_RADIUS = 9;
const RING_LENGTH = 2 * Math.PI * RING_RADIUS;

let timer = null;
let inflightSid = null;
// The session the ring currently speaks for, so a number read for one
// session is never left showing over another.
let ringSid = null;

// The open session as the list knows it (kind / agent), falling back to the
// object the overlay was opened with — the same resolution as the ⋮ menu.
function viewSession() {
  const v = state.sessionView;
  if (!v) return null;
  const sid = v.session.session_id;
  return (state.sessions || []).find(function (x) { return x.session_id === sid; }) || v.session;
}

// Only a full-control Claude session has a statusline on a screen the
// server can read (the plan picker's rule, session-transcript.js).
function ringAllowed(s) {
  return !!s && String(s.agent || 'claude').toLowerCase() === 'claude' && s.kind !== 'remote';
}

function pollAllowed() {
  return ringAllowed(viewSession()) && document.visibilityState === 'visible';
}

function render(percent) {
  const btn = els.contextRing;
  if (!btn) return;
  if (typeof percent !== 'number' || !isFinite(percent)) {
    btn.hidden = true;
    delete btn.dataset.percent;
    return;
  }
  const filled = Math.max(0, Math.min(100, percent));
  const dash = (RING_LENGTH * filled / 100).toFixed(2);
  btn.querySelector('.context-ring-fill')
    .setAttribute('stroke-dasharray', dash + ' ' + RING_LENGTH.toFixed(2));
  // Same 60/80 tiers as every other usage figure in the app.
  const tier = usageTier(percent);
  btn.dataset.tier = tier === 'warn' || tier === 'danger' ? tier : 'normal';
  btn.dataset.percent = String(percent);
  const label = 'Context window ' + percent + '% used';
  btn.title = label;
  btn.setAttribute('aria-label', label);
  btn.hidden = false;
}

function stop() {
  if (timer) {
    window.clearTimeout(timer);
    timer = null;
  }
}

function schedule() {
  stop();
  if (pollAllowed()) timer = window.setTimeout(tick, CONTEXT_POLL_MS);
}

async function tick() {
  timer = null;
  const s = viewSession();
  if (!pollAllowed()) return;
  const sid = s.session_id;
  // A mode switch or visibility change landing mid-request: the request
  // already in flight answers it, and reschedules when it lands.
  if (inflightSid === sid) return;
  inflightSid = sid;
  let body = null;
  try {
    body = await jsonApi('/api/claude-code/sessions/' + encodeURIComponent(sid) + '/context');
  } catch (_) {
    body = null;   // unknown is not 0%: the ring hides, the next tick asks again
  }
  if (inflightSid === sid) inflightSid = null;
  const now = viewSession();
  if (!now || now.session_id !== sid) return;   // the overlay moved on meanwhile
  render(body && typeof body.percent === 'number' ? body.percent : null);
  // A session that is gone won't come back: stop asking until it is reopened.
  if (body && body.reason === 'session_not_found') return;
  schedule();
}

// Called whenever the answer to "should the ring be polling, and for which
// session?" may have changed: open, mode switch, close, tab visibility.
export function syncContextRing() {
  stop();
  const s = viewSession();
  if (!ringAllowed(s)) {
    ringSid = null;
    render(null);
    return;
  }
  if (s.session_id !== ringSid) {
    ringSid = s.session_id;
    render(null);
  }
  if (pollAllowed()) tick();
}

export function wireContextRing() {
  if (!els.contextRing) return;
  bindLongPressHint(els.contextRing);
  els.contextRing.addEventListener('click', function () {
    const pct = els.contextRing.dataset.percent;
    if (pct == null) return;
    toast('Context window ' + pct + '% used');
  });
  document.addEventListener('visibilitychange', syncContextRing);
}
