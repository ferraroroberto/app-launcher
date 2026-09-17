/* Running Claude Code sessions panel: list, stop, refresh.
 *
 * One 🛑 "Stop and kill" button per row (issue #253), same for both kinds.
 * The session-host types the agent's own quit command (Claude's /quit,
 * Copilot's /exit, …), waits briefly for a clean exit so shutdown hooks
 * run, then force-terminates as a fallback — and the window always closes.
 * Detached (remote) rows have no PTY to type into, so the host force-kills
 * the console directly. One exception to the no-confirm design: the fleet
 * chief (#547) — see stopSession()'s isChiefSession() guard.
 */

import { els, state } from './state.js';
import { apiFailToast, authHeaders, isDesktopClient, jsonApi, logPollFailure, toast } from './api.js';
import { ensureTerminalToken } from './webauthn.js';
import { renderHomeHead } from './home-head.js';
// The session overlay's two modes (#982). Circular with this module by
// design (session-overlay.js → terminal.js / session-transcript.js → here
// for sessionTitle and the send helpers); nothing runs at import time.
import { closeSessionOverlay, openSessionOverlay } from './session-overlay.js';
import { CHIEF_KILL_CONFIRM, fmtDuration, iconUrl, isChiefSession, renderQuotaLines } from './dom-utils.js';
import { createRowMenu } from './row-menu.js';
import { icon } from './_vendored/icons/icons.js';
import { hasTranscriptReader } from './session-transcript.js';
// runChiefAction (and the ensureChief it wraps) lives in board-dispatch.js
// (split off board.js in #691; the shared helper landed in #828), exported
// for this cross-tab use (#547); board.js already imports
// stopSession/sessionTitle/openSessionRename from this module, so this
// mirrors the existing sessions.js<->terminal.js circular-import pattern
// rather than introducing a new risk.
import { runChiefAction } from './board-dispatch.js';

// Kept as a named re-export (apps.js, jobs.js, jobs-row.js, and this
// module's own row render all import it) over the shared formatter in
// dom-utils.js (#750) — same "3h 24m" elapsed-time granularity as before.
export function fmtAgo(epochSeconds) {
  return fmtDuration(epochSeconds, { fromEpoch: true, granular: true });
}

// Last path segment of a session's project dir, lowercased — the project
// folder name, used to spot a live title that merely echoes it.
function projectBasename(s) {
  const dir = String((s && s.project_dir) || '');
  const parts = dir.split(/[\\/]/).filter(Boolean);
  return (parts.length ? parts[parts.length - 1] : '').toLowerCase();
}

// Display title for a session, with smart precedence (issue #266, extended
// #396, #458). The single source of truth both the Coding tab and the Board
// tab call, so a live session shows an identical title on both — do not
// re-derive a title in board.js or anywhere else.
//
//   0. ``manual_title`` (issue #458) — a launcher-native rename always wins.
//      It is the one title channel that works identically across every
//      agent (including detached sessions) without depending on agent-native
//      OSC support, so it overrides every auto-derived source below.
//   1. ``shared_name`` (fleet-config#302, joined agent-aware server-side into
//      every session dict as shared_name/shared_name_source) wins outright
//      when it's a real Claude-assigned title (name_source !== 'derived') —
//      the one cross-tab authoritative source: Claude's own /resume picker
//      title, kept fresh on every UserPromptSubmit/Stop hook fire.
//   2. else the OSC-parsed ``live_title`` — kept as a same-poll-cycle-faster
//      supplement: it updates sub-second inside an open terminal (parsed
//      straight off the PTY), while the shared source only refreshes on the
//      next hook fire + sessions-state.json poll. Only some agents self-name
//      per conversation this way: Claude emits a real summary, Codex emits
//      "<folder> | <model>", Pi emits "π - <folder>", Antigravity/Copilot
//      emit nothing — a short title that's just the folder name is a
//      project echo, no more distinctive than the launch name, so it's
//      skipped here in favor of prompt_title below.
//   3. else the first-prompt-derived title (prompt_title) — covers agents
//      that don't self-name, and de-genericizes the folder-only echoes.
//   4. else the shared name even when it's the generic derived
//      "<project>-N" fallback — still better than a bare project echo since
//      it distinguishes sibling sessions in the same directory.
//   5. else the folder-echo live title, then the launch name.
// Coding agents prefix their live title with a brand glyph (Claude's green ✳);
// the per-session agent icon already identifies the agent, so strip any
// leading run of non-alphanumeric characters.
export function sessionTitle(s) {
  const manual = String((s && s.manual_title) || '').trim();
  if (manual) return manual;
  const shared = String((s && s.shared_name) || '').trim();
  const sharedDerived = !!(s && s.shared_name_source === 'derived');
  const live = String((s && s.live_title) || '')
    .replace(/^[^\p{L}\p{N}]+/u, '')
    .trim();
  const prompt = String((s && s.prompt_title) || '').trim();
  const base = projectBasename(s);
  // A short live title containing the folder name is a project echo (Codex /
  // Pi), no more distinctive than the launch name. A real summary is longer
  // and not folder-dominated, so the word-count guard lets it through.
  const projectEcho = !!live && !!base &&
    live.toLowerCase().includes(base) && live.split(/\s+/).length <= 4;
  if (shared && !sharedDerived) return shared;
  if (live && !projectEcho) return live;
  if (prompt) return prompt;
  if (shared) return shared;
  return live || (s && s.name) || (s && s.project) || 'session';
}

// Fleet chief status (#547) — Coding-tab parity with the Board's chat-mode
// row. Derived straight from state.sessions (already the Coding tab's own
// poll of every launcher-owned session, chief included), so no extra fetch.
function renderCodingChiefStatus() {
  if (!els.codingChiefStatus) return;
  const chief = state.sessions.find(isChiefSession);
  const alive = !!(chief && chief.alive !== false);
  els.codingChiefStatus.hidden = false;
  els.codingChiefStart.hidden = alive;
  if (els.codingChiefResume) els.codingChiefResume.hidden = alive;
  els.codingChiefStatusText.textContent = alive ? 'chief: running' : 'chief: not running';
}

// ------------------------------------------------- row action menu (#953)
//
// One gear per row opens a floating menu of the row's actions (transcript ·
// rename · stop) — the rail had no room for a third icon on the phone
// without crowding the title. The menu machinery (open/close, outside tap,
// Escape, surviving the poll re-render) is the shared row-menu.js helper
// since #977, when the Coding tile grew the same shape.
const sessionMenu = createRowMenu('session-menu');

export function closeSessionMenu() {
  sessionMenu.close();
}

export function renderSessions() {
  const host = els.sessionsList;
  host.innerHTML = '';
  els.sessionsEmpty.hidden = state.sessions.length !== 0;
  renderHomeHead();
  renderCodingChiefStatus();

  state.sessions.forEach(function (s) {
    const li = document.createElement('li');
    li.className = 'app-item session-item';
    // Same accent tint + crown as the Board tab's chief card (#547) — the
    // standing orchestrator reads distinct here too, not just on the Board.
    if (isChiefSession(s)) li.classList.add('session-item-chief');
    // Stable hook so a test (or any consumer) can target a specific
    // session's row by id rather than position — e.g. the kill regression
    // must act on the session it launched, never ".first" (issue #260).
    li.dataset.sessionId = s.session_id;

    const main = document.createElement('div');
    main.className = 'app-main';

    const remote = s.kind === 'remote';
    const reader = hasTranscriptReader(s);
    // A row tap opens the session overlay in the mode it was last viewed
    // in (#982): a full-control row has a terminal, a detached row of an
    // agent with a transcript reader opens in Chat. A detached row of an
    // agent with no reader has nothing to show, so it stays inert — still
    // killable from its gear menu.
    const tappable = !remote || reader;
    const open = document.createElement(tappable ? 'button' : 'div');
    open.className = 'launch-btn session-open' + (tappable ? '' : ' inert');
    if (tappable) open.type = 'button';

    // Title on its own full-width line at the top of the card, so a long
    // project title wraps across the whole card instead of being squeezed
    // into the narrow space beside the badges (issue #113).
    const name = document.createElement('span');
    name.className = 'name';
    if (isChiefSession(s)) {
      const crown = document.createElement('span');
      crown.className = 'board-chief-crown';
      crown.innerHTML = icon('crown');
      name.appendChild(crown);
    }
    name.appendChild(document.createTextNode(sessionTitle(s)));
    open.appendChild(name);

    const head = document.createElement('div');
    head.className = 'session-head';
    const dot = document.createElement('span');
    dot.className = 'health-dot ' + (s.alive === false ? 'down' : 'up');
    head.appendChild(dot);
    // Which coding agent this session is running (issue #45). Resolved
    // against the agent registry (state.agents) so a new agent's icon +
    // label flow through without touching this file; falls back to
    // Claude Code for an unrecognised id.
    const known = state.agents.find(function (a) { return a.id === s.agent; });
    const agentId = known ? known.id : 'claude';
    const agentIcon = document.createElement('img');
    agentIcon.className = 'session-agent-icon';
    agentIcon.src = iconUrl(agentId);
    agentIcon.alt = known ? known.label : 'Claude Code';
    agentIcon.title = agentIcon.alt;
    head.appendChild(agentIcon);
    const kindTag = document.createElement('span');
    kindTag.className = 'session-kind ' + (remote ? 'remote' : 'pty');
    kindTag.innerHTML = remote ? icon('cloud') + ' detached' : icon('zap') + ' full control';
    head.appendChild(kindTag);
    if (tappable) {
      const chev = document.createElement('span');
      chev.className = 'session-chevron';
      chev.textContent = '›';
      head.appendChild(chev);
    }
    open.appendChild(head);

    const meta = document.createElement('span');
    meta.className = 'meta';
    const ago = fmtAgo(s.started_at);
    meta.textContent = (ago ? 'up ' + ago + ' · ' : '') + s.project_dir;
    open.appendChild(meta);
    if (tappable) {
      open.addEventListener('click', function () { openSession(s); });
    }
    main.appendChild(open);
    li.appendChild(main);

    const actions = document.createElement('div');
    actions.className = 'row-actions session-actions';

    // One gear, vertically centred, opens the row's floating action menu
    // (#953) — a vertical icon + label list (#967). The menu holds, in order:
    //   · Terminal (#982) — full-control rows only: the session overlay in
    //     Terminal mode (a desktop browser gets the PC mirror window, as a
    //     row tap does).
    //   · Chat (#982) — every row whose agent has a transcript reader
    //     (Claude and Codex, #966; hasTranscriptReader mirrors the
    //     endpoint's flavour map): the same overlay in Chat mode. The reader
    //     uses the agent's native history, never the PTY capture a detached
    //     row lacks.
    //   · Rename (issue #458) — a launcher-native override that always wins
    //     in sessionTitle()'s precedence, for both kinds. Submitting a blank
    //     title clears it, reverting to the automatic precedence.
    //   · Stop-and-kill (issue #253), both kinds: the session-host quits
    //     gracefully then force-falls-back; the window always closes. Keeps
    //     the `action-stop-close` class (muted by default, danger-red on
    //     press). Two taps from the list now — a deliberate trade for the
    //     other actions fitting the phone.
    const gear = document.createElement('button');
    gear.type = 'button';
    gear.className = 'icon-btn session-gear';
    gear.innerHTML = icon('settings');
    gear.title = 'Session actions';
    gear.setAttribute('aria-label', 'Session actions');
    const menu = sessionMenu.attach(s.session_id, gear, [
      {
        className: 'session-terminal-btn', glyph: 'terminal',
        label: 'Open terminal', text: 'Terminal',
        hidden: remote,
        onTap: function () { openSession(s, 'terminal'); },
      },
      {
        className: 'session-chat-btn', glyph: 'messages-square',
        label: 'Open chat', text: 'Chat',
        hidden: !reader,
        onTap: function () { openSession(s, 'chat'); },
      },
      {
        glyph: 'pencil', label: 'Rename session', text: 'Rename',
        onTap: function () { openSessionRename(s); },
      },
      {
        className: 'action-stop-close', glyph: 'x',
        label: 'Stop and kill session', text: 'Stop',
        onTap: function () { stopSession(s); },
      },
    ]);
    actions.appendChild(gear);
    actions.appendChild(menu);

    li.appendChild(actions);

    host.appendChild(li);
  });
  // The open menu's row is gone (session ended) — drop the stale state.
  sessionMenu.endRender();
}

// Open a session when its row (or its gear's Terminal / Chat item) is
// tapped. `mode` forces 'terminal' or 'chat'; without it the session opens
// in the mode it was last viewed in (#982, session-overlay.js), a detached
// session always in Chat. On a desktop browser a full-control session's
// terminal is a dedicated PC Edge --app window (issue #282) — the same
// window a new-session launch opens — instead of rendering the terminal
// inside the user's own browser, so it can be closed without fear while
// the session keeps running headless. A second tap focuses that window
// rather than spawning a duplicate. Chat is in-page everywhere; the phone
// (and a desktop with mirroring disabled) streams the terminal in-page.
export async function openSession(s, mode) {
  if (isDesktopClient() && s.kind !== 'remote' && mode !== 'chat') {
    try {
      const r = await jsonApi(
        '/api/claude-code/sessions/' + encodeURIComponent(s.session_id) +
          '/mirror',
        { method: 'POST' }
      );
      if (r && r.mirrored) {
        toast(
          (r.action === 'focused' ? 'Focused ' : 'Opened ') +
            sessionTitle(s) + ' window',
          'good',
          { icon: 'monitor' }
        );
        return;
      }
      // Mirroring disabled server-side — fall through to the in-page terminal.
    } catch (exc) {
      apiFailToast('Open window failed', exc);
      return;
    }
  }
  openSessionOverlay(s, mode);
}

export async function stopSession(s) {
  // No confirm — one tap stops (issue #253 follow-up). The stop is graceful
  // (the agent's own quit, then force-fallback) and a mis-tap is resumable,
  // so a confirmation dialog is just friction. The one exception is the
  // fleet chief (#547 — parity with the Board tab's own drawer guard, the
  // isChiefCard() check on the drawer's stop button in board.js::buildDrawer):
  // the chief is the one session a mis-tap shouldn't take down, from either
  // the Coding tab's row or the terminal overlay's kill button (which calls
  // this same function). The Board drawer's own call passes a stripped
  // {session_id, name} object with no kind/label, so isChiefSession() is a
  // safe no-op there and it never double-confirms.
  if (isChiefSession(s) && !confirm(CHIEF_KILL_CONFIRM)) return;
  try {
    await jsonApi(
      '/api/claude-code/sessions/' + encodeURIComponent(s.session_id) +
        '/stop',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: 'quit' }),
      }
    );
    toast('Stopping ' + s.name + '…', 'good', { icon: 'octagon-x' });
    // The overlay showing this session closes whichever pane is up (#982) —
    // a detached session viewed in Chat has no state.terminal to key on.
    if (state.sessionView &&
        state.sessionView.session.session_id === s.session_id) {
      closeSessionOverlay();
    }
    setTimeout(fetchSessions, 1500);
  } catch (exc) {
    apiFailToast('Stop failed', exc);
  }
}

export async function fetchSessions() {
  try {
    const body = await jsonApi('/api/claude-code/sessions');
    state.sessions = body.sessions || [];
    renderSessions();
  } catch (exc) {
    // Sessions polling is best-effort — don't spam toasts.
    logPollFailure('sessions fetch failed', exc);
  }
}

// Two fixed quota rows (issues #326/#847/#860) on a standalone endpoint, so
// the Coding tab never depends on the Board tab having been opened. The rows
// no longer follow the model picker — both heavy agents are always listed,
// in the same order — so there is no selection to race and no sequence guard
// to keep.
const QUOTA_ERROR_LINES = [
  { harness: 'claude', label: 'Claude Code', state: 'error' },
  { harness: 'codex', label: 'Codex', state: 'error' },
];

export async function fetchRateLimits() {
  try {
    const body = await jsonApi('/api/rate-limits');
    renderQuotaLines(els.codingUsage, body.quota_lines);
  } catch (exc) {
    logPollFailure('rate-limits fetch failed', exc);
    renderQuotaLines(els.codingUsage, QUOTA_ERROR_LINES);
  }
}

// --------------------------------------------------- rename dialog (#458)
//
// One dialog shared by the Coding tab's row rename button and the Board
// tab's drawer rename button (board.js imports openSessionRename), the same
// way #renameDialog is shared across the Apps tab's rename affordances.
// ``onDone`` lets a caller optimistically patch its own view of the session
// instead of always re-fetching (the Board drawer stays open across a
// rename, and patching shows the new title without waiting on a poll).
let renameSessionTarget = null;
let renameSessionOnDone = null;
let sessionLinkFeedbackTimer = null;

export function openSessionRename(s, onDone) {
  renameSessionTarget = s;
  renameSessionOnDone = onDone || null;
  els.sessionRenameInput.value = sessionTitle(s);
  const supportsLinkRow = s.kind !== 'remote' && (s.agent === 'claude' || s.agent === 'codex');
  const webUrl = s.agent === 'claude' ? String(s.web_url || '') : '';
  els.sessionRenameHeading.textContent = supportsLinkRow ? 'Rename / link' : 'Rename session';
  els.sessionLinkRow.hidden = !supportsLinkRow;
  els.sessionLinkInput.value = webUrl || 'Not available yet';
  els.sessionLinkCopy.disabled = !webUrl;
  els.sessionLinkCopy.title = webUrl ? 'Copy link' : 'Web link not available yet';
  els.sessionLinkCopy.setAttribute(
    'aria-label', webUrl ? 'Copy session link' : 'Session web link not available yet'
  );
  window.clearTimeout(sessionLinkFeedbackTimer);
  els.sessionLinkCopy.classList.remove('is-copied');
  if (els.sessionRenameDialog.showModal) els.sessionRenameDialog.showModal();
}

// Send from a session's Chat mode (issue #967 → #983). The chat pane's
// composer and the Board drawer's (#984) post to the same kind-agnostic /input
// route: a PTY session submits with the host's framing, settle
// and ingest verification (#611/#760/#763); a detached session is typed into
// its PC console by PID and can only ever answer ``delivered: "unconfirmed"``.
// The gear menu's Send message dialog is gone — the composer covers both
// kinds — so the gate, the request and the outcome wording live here once.

// A detached session whose agent the registry says was never probed for
// console input (``console_input: false`` from /api/agents). Only an explicit
// false refuses: the boot fallback list carries no flag until that fetch
// lands, and "not known yet" must not grey Send out for the whole open — the
// session-host still refuses an unprobed agent with a 502 console_failed.
export function detachedSendRefused(s) {
  if (!s || s.kind !== 'remote') return false;
  const known = (state.agents || []).find(function (a) { return a.id === s.agent; });
  return !!known && known.console_input === false;
}

// /input is passkey-gated (middleware `_TERMINAL_GUARD_RULES`), so the
// request carries the terminal token — without it a phone behind a configured
// passkey gate gets a 401, which api() turns into the login overlay. Loopback
// and an unconfigured gate resolve '' and the header is simply left off.
export async function sendSessionMessage(sid, text) {
  const tt = await ensureTerminalToken();
  return jsonApi(
    '/api/claude-code/sessions/' + encodeURIComponent(sid) + '/input',
    {
      method: 'POST',
      headers: authHeaders({ terminalToken: tt, contentType: 'application/json' }),
      body: JSON.stringify({ data: text, submit: true }),
    }
  );
}

// The toast for one successful /input answer: `{ text, kind }`. Each verdict
// gets its own words, because folding them into "sent" is exactly what
// #760/#763/#929 were filed over. The failures never get here — a payload the
// terminal never echoed (502 not_ingested), a console that did not take the
// keystrokes (502 console_failed), an exited session (409) all throw, and
// the caller toasts them as "Send failed" with the text kept.
export function sendOutcome(verdict) {
  const v = verdict || {};
  // Detached (#967): a console has no output stream, so this is the best a
  // successful send can ever be — not a failure.
  if (v.delivered === 'unconfirmed') {
    return { text: 'Sent, not confirmed: typed into the PC console', kind: '' };
  }
  switch (v.submit_state) {
    case 'confirmed':
      return { text: 'Sent', kind: 'good' };
    case 'unconfirmed':
      // A short payload or a bare submit: Enter went in, nothing checked it.
      return { text: 'Sent, not confirmed: nothing verified the agent took it', kind: '' };
    case 'pending':
      // 202 (#763): in the agent's composer, Enter still with the watcher.
      return { text: 'Queued: the agent is busy, Enter goes in once it settles', kind: '' };
    case 'not_submitted':
      return { text: 'Not submitted: the text reached the agent but Enter was never sent', kind: 'error' };
    default:
      break;
  }
  // A session-host from before #929 carries no submit_state; only an
  // explicit `delivered: true` reads as a landing.
  if (v.delivered === true) return { text: 'Sent', kind: 'good' };
  return { text: 'Sent, not confirmed', kind: '' };
}

function wireSessionRenameDialog() {
  els.sessionRenameCancel.addEventListener('click', function () {
    if (els.sessionRenameDialog.close) els.sessionRenameDialog.close();
  });
  els.sessionLinkCopy.addEventListener('click', async function () {
    try {
      await navigator.clipboard.writeText(els.sessionLinkInput.value);
      els.sessionLinkCopy.classList.add('is-copied');
      window.clearTimeout(sessionLinkFeedbackTimer);
      sessionLinkFeedbackTimer = window.setTimeout(function () {
        els.sessionLinkCopy.classList.remove('is-copied');
      }, 650);
      toast('Session link copied', 'good', { icon: 'link' });
    } catch (exc) {
      apiFailToast('Copy link failed', exc);
    }
  });
  els.sessionRenameForm.addEventListener('submit', async function (ev) {
    ev.preventDefault();
    if (!renameSessionTarget) return;
    const title = els.sessionRenameInput.value.trim();
    try {
      await jsonApi(
        '/api/claude-code/sessions/' +
          encodeURIComponent(renameSessionTarget.session_id) + '/rename',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title }),
        }
      );
      if (els.sessionRenameDialog.close) els.sessionRenameDialog.close();
      if (renameSessionOnDone) {
        renameSessionOnDone(title);
      } else {
        await fetchSessions();
      }
    } catch (exc) {
      apiFailToast('Rename failed', exc);
    }
  });
}

export function wireSessions() {
  // The ⎇ status button (and the off-main popover) live in the Running-
  // sessions card's <summary>, so a click there would also toggle the
  // <details>. Stop the click at the actions container so it only drives
  // the buttons, never the collapse — same trick the Coding options card
  // uses for its Detached/Resume toggles.
  const headerActions = els.gitStatusBtn
    ? els.gitStatusBtn.closest('.sessions-header-actions')
    : null;
  if (headerActions) {
    headerActions.addEventListener('click', function (ev) { ev.stopPropagation(); });
  }
  // Manual Start-chief (#547) — same ensure endpoint as the Board's chat
  // mode, so a chief killed while the Coding tab was open (or via a
  // deliberate tray/session-host restart) can be brought back without
  // switching tabs.
  if (els.codingChiefStart) {
    els.codingChiefStart.addEventListener('click', function () {
      runChiefAction({
        button: els.codingChiefStart, label: 'Start', fresh: false, resume: false,
        onDone: fetchSessions,
      });
    });
  }
  // Manual Resume-chief (#633) — same ensure endpoint with resume=true, so
  // a chief killed by a host reboot / session-host restart can be reattached
  // (rather than started fresh) from the Coding tab too, not only Board chat
  // mode. Falls back server-side to a fresh spawn when nothing is resumable.
  if (els.codingChiefResume) {
    els.codingChiefResume.addEventListener('click', function () {
      runChiefAction({
        button: els.codingChiefResume, label: 'Resume', fresh: false, resume: true,
        onDone: fetchSessions,
      });
    });
  }
  wireSessionRenameDialog();
}
