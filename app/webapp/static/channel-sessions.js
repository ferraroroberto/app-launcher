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
 * (GET /api/claude-code/sessions/{sid}/context). Request cadence (#1402):
 *   - list closed, summary line showing: one read per running Claude session
 *     the moment the line first has it (and for any session that appears
 *     later), then every CONTEXT_SLOW_POLL_MS (60 s). That is the whole cost
 *     of the alert icon; there is no fast poll behind a closed popup.
 *   - list open: every CONTEXT_POLL_MS (10 s), so a figure on screen is live.
 *   - setting off, no running session, or the page hidden: no reads.
 * At >= CONTEXT_ALERT_PCT (dom-utils.js) any session raises the alert icon on
 * both summary lines, and its popup row's Compact button is highlighted.
 *
 * The popup stays read-only apart from Compact: a borderless icon-only button
 * at the right end of each running row (#1402) that sends /compact through the
 * same verified /input route as the terminal ⋮ menu (sessions.js::
 * sendSessionMessage) and reports the real outcome. There is still no Stop and
 * no delete here.
 */

import { els, state } from './state.js';
import { apiFailToast, jsonApi, toast } from './api.js';
import {
  channelSessionName, contextAlert, fmtDuration, isChannelSession, usageTier,
} from './dom-utils.js';
import { icon } from './_vendored/icons/icons.js';
import { chip } from './glance.js';
import { openSessionOverlay } from './session-overlay.js';
import { canCompact, sendOutcome, sendSessionMessage } from './sessions.js';

const CONTEXT_POLL_MS = 10000;
const CONTEXT_SLOW_POLL_MS = 60000;
// How long a Compact outcome stays on its row. The toast says it too, but a
// modal <dialog> sits in the top layer above the toast, so the row carries it.
const COMPACT_NOTE_MS = 6000;
// The terminal ⋮ menu's Compact (terminal-bar.js): same glyph. The glyph is an
// app-local one that lives in index.html's inline sprite only, so it travels as
// data, not as a literal icon() call.
const COMPACT_ACTION = { glyph: 'chevrons-down-up' };

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

// session_id → percent (number) or null for "not known". Survives a row
// re-render so a poll tick never blanks a figure that was already read.
const contextBySid = new Map();
let timer = null;
let reading = false;

// The highest context figure among running sessions at or over the alert
// threshold, or null when none is.
function contextAlertPct() {
  let worst = null;
  channelSessions().forEach(function (s) {
    const pct = contextBySid.get(s.session_id);
    if (s.alive !== false && contextAlert(pct) && (worst == null || pct > worst)) worst = pct;
  });
  return worst;
}

function paintSummary(btn, count, alerting) {
  if (!btn) return;
  btn.hidden = !(channelSessionsHidden() && count > 0);
  if (btn.hidden) return;
  btn.querySelector('.channel-summary-text').textContent =
    count + ' Telegram ' + (count === 1 ? 'session' : 'sessions') + ' running';
  const flag = btn.querySelector('.channel-summary-alert');
  if (flag) flag.hidden = !alerting;
}

// The Code tab's flat row (#1434): a send-glyph avatar, "Telegram", then
// "N running" and an attention "context N%" chip only while a session is at
// the alert threshold. The chip is rebuilt only when its text changes, so a
// poll tick never replaces it for nothing.
function paintCodingRow(btn, count, alertPct) {
  if (!btn) return;
  btn.hidden = !(channelSessionsHidden() && count > 0);
  if (btn.hidden) return;
  btn.querySelector('.channel-summary-text').textContent = count + ' running';
  const meta = btn.querySelector('.srow-meta');
  const want = alertPct == null ? '' : 'context ' + alertPct + '%';
  const current = meta.querySelector('.channel-context-chip');
  if (current && current.textContent === want) return;
  if (current) current.remove();
  if (want) meta.appendChild(chip(want, 'attention', 'channel-context-chip'));
}

function paintSummaries() {
  const count = runningCount();
  const alertPct = contextAlertPct();
  paintCodingRow(els.sessionsChannelSummary, count, alertPct);
  paintSummary(els.boardChannelSummary, count, alertPct != null);
  if (dialogOpen()) renderRows();
}

// Repaint both summary lines (Board, Coding tab) and, when the list is open,
// its rows. Called from every render that filters a list, so a session that
// starts or stops moves the count on the next poll — and a session the alert
// has not read yet gets its first context read here.
export function renderChannelSummaries() {
  paintSummaries();
  ensureReading();
}

// -------------------------------------------------------------- read-only list

function dialogOpen() {
  return !!(els.channelListDialog && els.channelListDialog.open);
}

// Compact outcome per session, kept for COMPACT_NOTE_MS: { text, kind }.
const compactNotes = new Map();
// Sessions with a /compact in flight, so a second tap cannot queue another.
const compacting = new Set();

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
    const note = compactNotes.get(s.session_id);
    open.appendChild(note
      ? textEl(note.text, 'channel-list-status channel-list-note' + (note.kind ? ' is-' + note.kind : ''))
      : textEl(running ? 'Running' : 'Stopped',
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
    if (running && canCompact(s)) li.appendChild(compactButton(s, pct));
    host.appendChild(li);
  });
}

// The row's one action (#1402): Compact, a borderless icon at the row's right
// end after Running / Context. A sibling of the open button, never inside it —
// a tap here acts and must not also open the look. The icon takes the attention
// colour (data-high) at >= the alert threshold; there is no border and no text.
function compactButton(s, pct) {
  const sid = s.session_id;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'icon-button channel-list-compact';
  if (contextAlert(pct)) btn.dataset.high = 'true';
  btn.disabled = compacting.has(sid);
  btn.title = 'Send /compact to this session';
  btn.setAttribute('aria-label', 'Compact ' + channelSessionName(s));
  btn.innerHTML = icon(COMPACT_ACTION.glyph);
  btn.addEventListener('click', function () { compactSession(s); });
  return btn;
}

// /compact through the verified /input route, the toast saying what actually
// happened (the terminal ⋮ menu's Compact, #1218). A modal <dialog> paints over
// the toast, so the outcome's first clause also replaces the row's status word
// for COMPACT_NOTE_MS — in the same cell, so the row never grows.
function compactSession(s) {
  const sid = s.session_id;
  if (!canCompact(s) || compacting.has(sid)) return;
  compacting.add(sid);
  renderRows();
  function note(text, kind) {
    compactNotes.set(sid, { text: text.split(':')[0], kind: kind });
    window.setTimeout(function () {
      compactNotes.delete(sid);
      if (dialogOpen()) renderRows();
    }, COMPACT_NOTE_MS);
  }
  sendSessionMessage(sid, '/compact').then(
    function (verdict) {
      const outcome = sendOutcome(verdict);
      toast('Compact: ' + outcome.text, outcome.kind, { icon: COMPACT_ACTION.glyph });
      note(outcome.text, outcome.kind);
    },
    function (exc) {
      apiFailToast('Compact failed', exc);
      note('Compact failed', 'error');
    }
  ).finally(function () {
    compacting.delete(sid);
    if (dialogOpen()) renderRows();
  });
}

// ------------------------------------------------------------ context reads

// The sessions a context read applies to: running Claude PTY sessions.
function contextTargets() {
  return channelSessions().filter(function (s) {
    return s.alive !== false && s.kind !== 'remote' &&
      String(s.agent || 'claude').toLowerCase() === 'claude';
  });
}

// Reads happen only while a summary line is showing something to alert on.
function wantsPolling() {
  return channelSessionsHidden() && contextTargets().length > 0;
}

// One context read per target; any failure or an answer without a number is
// "not known", never 0%. Reschedules itself at the cadence the list's state
// asks for (module header).
async function pollContext() {
  timer = null;
  if (reading || !wantsPolling()) return;
  if (document.visibilityState === 'visible') {
    reading = true;
    try {
      await Promise.all(contextTargets().map(async function (s) {
        let body = null;
        try {
          body = await jsonApi('/api/claude-code/sessions/' + encodeURIComponent(s.session_id) + '/context');
        } catch (_) {
          body = null;
        }
        contextBySid.set(s.session_id,
          body && typeof body.percent === 'number' ? body.percent : null);
      }));
    } finally {
      reading = false;
    }
    paintSummaries();
  }
  if (wantsPolling()) scheduleContext();
}

function scheduleContext() {
  stopPolling();
  timer = window.setTimeout(pollContext, dialogOpen() ? CONTEXT_POLL_MS : CONTEXT_SLOW_POLL_MS);
}

function stopPolling() {
  if (timer) window.clearTimeout(timer);
  timer = null;
}

// Start (or catch up) the reads: nothing running yet, or a session that has
// no figure yet, reads now; otherwise the timer already in place stands.
function ensureReading() {
  if (reading || !wantsPolling()) return;
  const fresh = contextTargets().some(function (s) { return !contextBySid.has(s.session_id); });
  if (!timer || fresh) {
    stopPolling();
    pollContext();
  }
}

export function openChannelList() {
  renderRows();
  if (!els.channelListDialog.open) els.channelListDialog.showModal();
  stopPolling();
  pollContext();
}

export function closeChannelList() {
  if (els.channelListDialog.open) els.channelListDialog.close();
}

export function wireChannelSessions() {
  [els.sessionsChannelSummary, els.boardChannelSummary].forEach(function (btn) {
    if (btn) btn.addEventListener('click', openChannelList);
  });
  if (!els.channelListDialog) return;
  els.channelListClose.addEventListener('click', closeChannelList);
  els.channelListDone.addEventListener('click', closeChannelList);
  // Every way of closing the <dialog> (Esc and a backdrop dismissal included)
  // ends in this event: the fast poll stops and the slow one takes over.
  els.channelListDialog.addEventListener('close', function () {
    stopPolling();
    if (wantsPolling()) scheduleContext();
  });
}
