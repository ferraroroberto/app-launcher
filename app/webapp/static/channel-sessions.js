/* Telegram channel sessions — hidden from the lists, one line instead (#1384).
 *
 * A Telegram channel session (label `telegram:<profile>`, #1366) is long-lived
 * and serves a household member through their own chat; nobody types into it
 * here, and its Stop is one mis-tap from cutting that chat off. With the
 * `hide_channel_sessions` setting on (the default) the Board's columns and the
 * Coding tab's session list leave those sessions out and show a single summary
 * line in their place — "2 Telegram sessions running" — that opens a read-only
 * list: status, last activity and context use per session, with no Stop and no
 * delete. Off, everything renders exactly as before.
 *
 * This is display filtering only. `state.sessions` and `state.board` still hold
 * every session, so an open overlay, a deep link and the title poll resolve
 * theirs as ever; only the render sites ask `isHiddenChannel()` and the one
 * kill path (`stopSession`) refuses a hidden channel session.
 *
 * The context figure is the existing per-session route the overlay's ring uses
 * (GET /api/claude-code/sessions/{sid}/context), polled for the sessions in
 * the list only while the list is open — closing it stops the timer — so the
 * summary line itself costs no requests.
 */

import { els, state } from './state.js';
import { jsonApi } from './api.js';
import { channelSessionName, fmtDuration, isChannelSession, usageTier } from './dom-utils.js';
import { openSessionOverlay } from './session-overlay.js';

const CONTEXT_POLL_MS = 10000;

// Default on: before the first config lands, or if it is unreadable, the safe
// reading is "hidden" — never a flash of the cards the user chose to hide.
export function channelSessionsHidden() {
  const cfg = state.config;
  return !cfg || cfg.hide_channel_sessions !== false;
}

// True for a session the lists must leave out right now.
export function isHiddenChannel(item) {
  return channelSessionsHidden() && isChannelSession(item);
}

function channelSessions() {
  return (state.sessions || []).filter(isChannelSession);
}

function runningCount() {
  return channelSessions().filter(function (s) { return s.alive !== false; }).length;
}

// ------------------------------------------------------------- summary line

function paintSummary(btn, count) {
  if (!btn) return;
  btn.hidden = !(channelSessionsHidden() && count > 0);
  if (btn.hidden) return;
  btn.querySelector('.channel-summary-text').textContent =
    count + ' Telegram ' + (count === 1 ? 'session' : 'sessions') + ' running';
}

// Repaint both summary lines (Board, Coding tab) and, when the list is open,
// its rows. Called from every render that filters a list, so a session that
// starts or stops moves the count on the next poll.
export function renderChannelSummaries() {
  const count = runningCount();
  paintSummary(els.sessionsChannelSummary, count);
  paintSummary(els.boardChannelSummary, count);
  if (els.channelListDialog && els.channelListDialog.open) renderRows();
}

// -------------------------------------------------------------- read-only list

// session_id → percent (number) or null for "not known". Survives a row
// re-render so a poll tick never blanks a figure that was already read.
const contextBySid = new Map();
let timer = null;

function contextText(sid) {
  const pct = contextBySid.get(sid);
  return typeof pct === 'number' ? pct + '%' : '—';
}

function meta(label, valueEl) {
  const wrap = document.createElement('span');
  wrap.className = 'channel-list-meta';
  const l = document.createElement('span');
  l.className = 'muted small';
  l.textContent = label;
  wrap.appendChild(l);
  wrap.appendChild(valueEl);
  return wrap;
}

function textEl(text, cls) {
  const el = document.createElement('span');
  el.className = cls || '';
  el.textContent = text;
  return el;
}

function renderRows() {
  const host = els.channelListRows;
  const rows = channelSessions();
  host.replaceChildren();
  els.channelListEmpty.hidden = rows.length !== 0;
  rows.forEach(function (s) {
    const li = document.createElement('li');
    li.className = 'channel-list-row';
    li.dataset.sessionId = s.session_id;
    const open = document.createElement('button');
    open.type = 'button';
    open.className = 'channel-list-open';
    open.title = 'Open a read-only look at this session';
    open.appendChild(textEl(channelSessionName(s), 'channel-list-name'));
    const running = s.alive !== false;
    open.appendChild(textEl(running ? 'Running' : 'Stopped',
      'channel-list-status ' + (running ? 'is-up' : 'is-down')));
    const last = fmtDuration(s.last_output_at, { fromEpoch: true });
    open.appendChild(meta('Last activity', textEl(last ? (last === 'now' ? 'just now' : last + ' ago') : '—', 'channel-list-last')));
    const ctx = textEl(contextText(s.session_id), 'channel-list-context');
    const pct = contextBySid.get(s.session_id);
    if (typeof pct === 'number') ctx.dataset.tier = usageTier(pct);
    open.appendChild(meta('Context', ctx));
    open.addEventListener('click', function () {
      closeChannelList();
      // Chat, not Terminal: the look is read-only, and nothing here types.
      openSessionOverlay(s, 'chat');
    });
    li.appendChild(open);
    host.appendChild(li);
  });
}

// One context read per Claude PTY session in the list; any failure or an
// answer without a number is "not known", never 0%.
async function pollContext() {
  timer = null;
  if (!els.channelListDialog.open) return;
  if (document.visibilityState === 'visible') {
    const targets = channelSessions().filter(function (s) {
      return s.alive !== false && s.kind !== 'remote' &&
        String(s.agent || 'claude').toLowerCase() === 'claude';
    });
    await Promise.all(targets.map(async function (s) {
      let body = null;
      try {
        body = await jsonApi('/api/claude-code/sessions/' + encodeURIComponent(s.session_id) + '/context');
      } catch (_) {
        body = null;
      }
      contextBySid.set(s.session_id,
        body && typeof body.percent === 'number' ? body.percent : null);
    }));
    if (!els.channelListDialog.open) return;
    renderRows();
  }
  timer = window.setTimeout(pollContext, CONTEXT_POLL_MS);
}

function stopPolling() {
  if (timer) window.clearTimeout(timer);
  timer = null;
}

export function openChannelList() {
  renderRows();
  if (!els.channelListDialog.open) els.channelListDialog.showModal();
  stopPolling();
  pollContext();
}

export function closeChannelList() {
  stopPolling();
  if (els.channelListDialog.open) els.channelListDialog.close();
}

export function wireChannelSessions() {
  [els.sessionsChannelSummary, els.boardChannelSummary].forEach(function (btn) {
    if (btn) btn.addEventListener('click', openChannelList);
  });
  if (!els.channelListDialog) return;
  els.channelListClose.addEventListener('click', closeChannelList);
  els.channelListDone.addEventListener('click', closeChannelList);
  // Esc and a backdrop dismissal close the <dialog> without our handlers.
  els.channelListDialog.addEventListener('close', stopPolling);
}
