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
import { isDesktopClient, jsonApi, toast } from './api.js';
import { hideTerminal, openTerminal } from './terminal.js';

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

// Display title for a session, with smart precedence (issue #266). Only some
// agents self-name per conversation: Claude emits a real summary, but Codex
// emits "<folder> | <model>", Pi emits "π - <folder>", and Antigravity /
// Copilot emit no title at all. So:
//   1. a genuine live title (not just the project folder) wins — Claude's;
//   2. else the first-prompt-derived title (prompt_title) — covers the agents
//      that don't self-name, and de-genericizes the folder-only ones;
//   3. else fall back to the folder-echo title, then the launch name.
// Coding agents prefix their live title with a brand glyph (Claude's green ✳);
// the per-session agent icon already identifies the agent, so strip any
// leading run of non-alphanumeric characters.
export function sessionTitle(s) {
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
  if (live && !projectEcho) return live;
  if (prompt) return prompt;
  return live || (s && s.name) || 'session';
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
    agentIcon.src = '/static/icons/' + agentId + '.svg';
    agentIcon.alt = known ? known.label : 'Claude Code';
    agentIcon.title = agentIcon.alt;
    head.appendChild(agentIcon);
    const kindTag = document.createElement('span');
    kindTag.className = 'session-kind ' + (remote ? 'remote' : 'pty');
    kindTag.textContent = remote ? '☁️ detached' : '⚡ full control';
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
    stopBtn.textContent = '✕';
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
      toast('Open window failed: ' + (exc.message || exc), 'error');
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
    toast('Stop failed: ' + (exc.message || exc), 'error');
  }
}

export async function fetchSessions() {
  try {
    const body = await jsonApi('/api/claude-code/sessions');
    state.sessions = body.sessions || [];
    renderSessions();
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      // Sessions polling is best-effort — don't spam toasts.
      console.warn('sessions fetch failed', exc);
    }
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
