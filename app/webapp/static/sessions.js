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
import { apiFailToast, isDesktopClient, jsonApi, logPollFailure, toast } from './api.js';
import { renderHomeHead } from './home-head.js';
import { hideTerminal, openTerminal } from './terminal.js';
import { CHIEF_KILL_CONFIRM, bindOutsideClickToClose, fmtDuration, iconUrl, isChiefSession, renderQuotaLines } from './dom-utils.js';
import { icon } from './_vendored/icons/icons.js';
// Same circular-import shape as terminal.js above: session-transcript.js
// imports sessionTitle from here for its overlay title (#953).
import { openTranscript } from './session-transcript.js';
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
// without crowding the title. The list re-renders on every sessions poll,
// which would tear an open menu down: the open row's id is remembered here
// and renderSessions() reopens it on the rebuilt row.
let openMenuSid = null;
let disposeMenuOutside = null;

function showSessionMenu(sid, gear, menu) {
  openMenuSid = sid;
  menu.hidden = false;
  gear.setAttribute('aria-expanded', 'true');
  if (disposeMenuOutside) disposeMenuOutside();
  disposeMenuOutside = bindOutsideClickToClose(menu, gear, closeSessionMenu);
}

export function closeSessionMenu() {
  openMenuSid = null;
  if (disposeMenuOutside) {
    disposeMenuOutside();
    disposeMenuOutside = null;
  }
  if (!els.sessionsList) return;
  els.sessionsList.querySelectorAll('.session-menu').forEach(function (m) { m.hidden = true; });
  els.sessionsList.querySelectorAll('.session-gear[aria-expanded="true"]').forEach(function (g) {
    g.setAttribute('aria-expanded', 'false');
  });
}

function menuButton(className, glyph, label, onTap) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'icon-btn session-menu-btn ' + className;
  btn.innerHTML = icon(glyph);
  btn.title = label;
  btn.setAttribute('aria-label', label);
  btn.setAttribute('role', 'menuitem');
  btn.addEventListener('click', function () {
    closeSessionMenu();
    onTap();
  });
  return btn;
}

export function renderSessions() {
  const host = els.sessionsList;
  host.innerHTML = '';
  els.sessionsEmpty.hidden = state.sessions.length !== 0;
  renderHomeHead();
  renderCodingChiefStatus();
  let menuReopened = false;

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
    // Full-control rows open the live terminal on tap. Detached rows
    // can't be streamed, so the row is inert — it's still killable
    // from the ⏹️ button.
    const open = document.createElement(remote ? 'div' : 'button');
    open.className = 'launch-btn session-open' + (remote ? ' inert' : '');
    if (!remote) open.type = 'button';

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
    if (!remote) {
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
    if (!remote) {
      open.addEventListener('click', function () { openSession(s); });
    }
    main.appendChild(open);
    li.appendChild(main);

    const actions = document.createElement('div');
    actions.className = 'row-actions session-actions';

    // One gear, vertically centred, opens the row's floating action menu
    // (#953). The menu holds, in order:
    //   · Transcript — full-control rows only (a detached row has no
    //     launcher capture and no structured history to show).
    //   · Rename (issue #458) — a launcher-native override that always wins
    //     in sessionTitle()'s precedence, for both kinds. Submitting a blank
    //     title clears it, reverting to the automatic precedence.
    //   · Stop-and-kill (issue #253), both kinds: the session-host quits
    //     gracefully then force-falls-back; the window always closes. Keeps
    //     the `action-stop-close` class (muted by default, danger-red on
    //     press). Two taps from the list now — the in-terminal ✕ is still
    //     one — a deliberate trade for the third action fitting the phone.
    const gear = document.createElement('button');
    gear.type = 'button';
    gear.className = 'icon-btn session-gear';
    gear.innerHTML = icon('settings');
    gear.title = 'Session actions';
    gear.setAttribute('aria-label', 'Session actions');
    gear.setAttribute('aria-haspopup', 'menu');
    gear.setAttribute('aria-expanded', 'false');
    const menu = document.createElement('div');
    menu.className = 'session-menu';
    menu.setAttribute('role', 'menu');
    menu.hidden = true;
    if (!remote) {
      menu.appendChild(menuButton('session-transcript-btn', 'messages-square', 'Session transcript',
        function () { openTranscript(s); }));
    }
    menu.appendChild(menuButton('', 'pencil', 'Rename session',
      function () { openSessionRename(s); }));
    menu.appendChild(menuButton('action-stop-close', 'x', 'Stop and kill session',
      function () { stopSession(s); }));
    gear.addEventListener('click', function () {
      if (openMenuSid === s.session_id) closeSessionMenu();
      else showSessionMenu(s.session_id, gear, menu);
    });
    actions.appendChild(gear);
    actions.appendChild(menu);
    if (openMenuSid === s.session_id) {
      showSessionMenu(s.session_id, gear, menu);
      menuReopened = true;
    }

    li.appendChild(actions);

    host.appendChild(li);
  });
  // The open menu's row is gone (session ended) — drop the stale state.
  if (openMenuSid && !menuReopened) closeSessionMenu();
}

// Open a full-control session when its row is tapped. On a desktop browser
// this opens a dedicated PC Edge --app window (issue #282) — the same window
// a new-session launch opens — instead of rendering the terminal inside the
// user's own browser, so it can be closed without fear while the session
// keeps running headless. A second tap focuses that window rather than
// spawning a duplicate. The phone (and a desktop with mirroring disabled)
// streams the terminal in-page as before.
export async function openSession(s) {
  if (isDesktopClient()) {
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
  openTerminal(s);
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
    if (state.terminal && state.terminal.sid === s.session_id) {
      hideTerminal();
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
