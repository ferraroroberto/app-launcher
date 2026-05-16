/* Running Claude Code sessions panel: list, stop, refresh.
 *
 * Stop modes:
 *   PTY ("full control"):  ⏹ Stop (leave window open)  + ⏏ Stop & Close
 *   Remote ("detached"):                                 only ⏏ Stop & Close
 *     (graceful Ctrl+C requires stdin access — only the PTY owns that.)
 */

import { els, state } from './state.js';
import { jsonApi, toast } from './api.js';
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

export function renderSessions() {
  const host = els.sessionsList;
  host.innerHTML = '';
  els.sessionsEmpty.hidden = state.sessions.length !== 0;

  state.sessions.forEach(function (s) {
    const li = document.createElement('li');
    li.className = 'app-item session-item';

    const main = document.createElement('div');
    main.className = 'app-main';

    const remote = s.kind === 'remote';
    // Full-control rows open the live terminal on tap. Detached rows
    // can't be streamed, so the row is inert — it's still killable
    // from the ⏹️ button.
    const open = document.createElement(remote ? 'div' : 'button');
    open.className = 'launch-btn session-open' + (remote ? ' inert' : '');
    if (!remote) open.type = 'button';

    const head = document.createElement('div');
    head.className = 'session-head';
    const dot = document.createElement('span');
    dot.className = 'health-dot ' + (s.alive === false ? 'down' : 'up');
    head.appendChild(dot);
    const kindTag = document.createElement('span');
    kindTag.className = 'session-kind ' + (remote ? 'remote' : 'pty');
    kindTag.textContent = remote ? '☁️ detached' : '⚡ full control';
    head.appendChild(kindTag);
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = (s.live_title && s.live_title.trim()) ? s.live_title : s.name;
    head.appendChild(name);
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
      open.addEventListener('click', function () { openTerminal(s); });
    }
    main.appendChild(open);
    li.appendChild(main);

    const actions = document.createElement('div');
    actions.className = 'row-actions session-actions';

    // For attached (PTY) sessions: show both Stop and Stop & Close.
    // For detached (remote) sessions: show only Stop & Close (graceful stop requires stdin access).
    if (!remote) {
      const stopBtn = document.createElement('button');
      stopBtn.type = 'button';
      stopBtn.className = 'icon-btn action-stop';
      stopBtn.textContent = '⏹️';
      stopBtn.title = 'Stop (leave window open)';
      stopBtn.setAttribute('aria-label', 'Stop session');
      stopBtn.addEventListener('click', function () { stopSession(s, false); });
      actions.appendChild(stopBtn);
    }

    const stopCloseBtn = document.createElement('button');
    stopCloseBtn.type = 'button';
    stopCloseBtn.className = 'icon-btn action-stop-close';
    stopCloseBtn.textContent = '⏏️';
    stopCloseBtn.title = remote ? 'Stop session' : 'Stop and close window';
    stopCloseBtn.setAttribute('aria-label', 'Stop and close session');
    stopCloseBtn.addEventListener('click', function () { stopSession(s, true); });
    actions.appendChild(stopCloseBtn);

    li.appendChild(actions);

    host.appendChild(li);
  });
}

export async function stopSession(s, closeWindow) {
  const remote = s.kind === 'remote';
  let msg;
  if (closeWindow) {
    // Stop & Close
    msg = remote
      ? 'Stop and close the detached session "' + s.name + '"?\n\n' +
        'Its console window will be closed.'
      : 'Stop and close the Claude Code session "' + s.name + '"?\n\n' +
        'The terminal window will close.';
  } else {
    // Just Stop (PtySession only)
    msg = 'Stop the Claude Code session "' + s.name + '"?\n\n' +
      'The terminal window will stay open, and Claude will exit cleanly.';
  }
  if (!confirm(msg)) return;
  try {
    await jsonApi(
      '/api/claude-code/sessions/' + encodeURIComponent(s.session_id) +
        '/stop',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode: 'quit', close_window: closeWindow }),
      }
    );
    const action = closeWindow ? '🛑 Stopping & closing ' : '🛑 Stopping ';
    toast(action + s.name + '…', 'good');
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
  els.refreshSessions.addEventListener('click', fetchSessions);
}
