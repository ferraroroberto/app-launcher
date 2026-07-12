/* Running Claude Code sessions panel: list, stop, refresh.
 *
 * One 🛑 "Stop and kill" button per row (issue #253), same for both kinds.
 * The session-host types the agent's own quit command (Claude's /quit,
 * Copilot's /exit, …), waits briefly for a clean exit so shutdown hooks
 * run, then force-terminates as a fallback — and the window always closes.
 * Detached (remote) rows have no PTY to type into, so the host force-kills
 * the console directly.
 */

import { els, state } from './state.js';
import { apiFailToast, isDesktopClient, jsonApi, logPollFailure, toast } from './api.js';
import { hideTerminal, openTerminal } from './terminal.js';
import { iconUrl, renderUsageBadgeRow } from './dom-utils.js';
import { icon } from './_vendored/icons/icons.js';

export function fmtAgo(epochSeconds) {
  if (!epochSeconds) return '';
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - epochSeconds));
  if (secs < 60) return secs + 's';
  const mins = Math.floor(secs / 60);
  if (mins < 60) return mins + 'm';
  const hrs = Math.floor(mins / 60);
  return hrs + 'h ' + (mins % 60) + 'm';
}

// Last path segment of a session's project dir, lowercased — the project
// folder name, used to spot a live title that merely echoes it.
function projectBasename(s) {
  const dir = String((s && s.project_dir) || '');
  const parts = dir.split(/[\\/]/).filter(Boolean);
  return (parts.length ? parts[parts.length - 1] : '').toLowerCase();
}

// Display title for a session, with smart precedence (issue #266, extended
// #396). The single source of truth both the Coding tab and the Board tab
// call, so a live session shows an identical title on both — do not
// re-derive a title in board.js or anywhere else.
//
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

export function renderSessions() {
  const host = els.sessionsList;
  host.innerHTML = '';
  els.sessionsEmpty.hidden = state.sessions.length !== 0;

  state.sessions.forEach(function (s) {
    const li = document.createElement('li');
    li.className = 'app-item session-item';
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
    name.textContent = sessionTitle(s);
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

    // Single Stop-and-kill button per row, both kinds (issue #253). The
    // session-host quits gracefully then force-falls-back; the window
    // always closes. A plain ✕ glyph (not a loud 🛑 emoji) inherits the
    // theme — muted by default via `action-stop-close`, danger-red on press.
    const stopBtn = document.createElement('button');
    stopBtn.type = 'button';
    stopBtn.className = 'icon-btn action-stop-close';
    stopBtn.innerHTML = icon('x');
    stopBtn.title = 'Stop and kill';
    stopBtn.setAttribute('aria-label', 'Stop and kill session');
    stopBtn.addEventListener('click', function () { stopSession(s); });
    actions.appendChild(stopBtn);

    li.appendChild(actions);

    host.appendChild(li);
  });
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
          (r.action === 'focused' ? '🖥️ Focused ' : '🖥️ Opened ') +
            sessionTitle(s) + ' window',
          'good'
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
  // so a confirmation dialog is just friction.
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
    toast('🛑 Stopping ' + s.name + '…', 'good');
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

// Claude 5h/7d usage badges (issue #326) in the Running-sessions header —
// same data + rendering as the Board tab's badges (dom-utils.js), but on
// its own endpoint so this tab never depends on the Board tab ever having
// been opened (GET /api/board's own rate-limits read only happens as a
// side effect of fetchBoard(), which self-gates to "Board tab visible").
export async function fetchRateLimits() {
  try {
    const body = await jsonApi('/api/rate-limits');
    renderUsageBadgeRow(els.codingUsage, els.codingUsageSession, els.codingUsageWeekly, body);
  } catch (exc) {
    logPollFailure('rate-limits fetch failed', exc);
  }
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
}
