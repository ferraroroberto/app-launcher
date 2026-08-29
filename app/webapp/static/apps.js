/* Apps tab: the registry list, launching, and the running-apps panel — plus
 * the shared orchestration both tab surfaces drive (renderApps, launchApp,
 * fetchApps).
 *
 * renderApps also feeds the Coding tab's project list (the `claude-code`
 * rows); those tiles, their git-status annotation and the agent-visibility
 * toggles live in apps-coding.js, the rename/scan dialogs in
 * apps-dialogs.js, and the port-listeners panel in apps-listeners.js — all
 * split out in issue #723, following the shape jobs.js and board.js already
 * set.
 */

import { els, state } from './state.js';
import { apiFailToast, jsonApi, toast, logPollFailure } from './api.js';
import { renderHomeHead } from './home-head.js';
import { fmtAgo } from './sessions.js';
import { applyLaunchSizePayload, handleLaunchResponse } from './terminal.js';
import { icon } from './_vendored/icons/icons.js';
import { switchEl } from './_vendored/switch/switch.js';
import { renderAgentVisibility, renderCodingList, wireCoding } from './apps-coding.js';
import { openRename, wireRenameDialog, wireScanDialog } from './apps-dialogs.js';

// ----------------------------------------------------------- apps list
export function renderApps() {
  const codingApps = state.apps.filter(function (a) { return a.kind === 'claude-code'; });
  const trayApps = state.apps.filter(function (a) { return a.kind === 'tray'; });
  const otherApps = state.apps.filter(function (a) {
    return a.kind !== 'claude-code' && a.kind !== 'tray';
  });

  renderCodingList(els.claudeList, codingApps);
  renderList(els.registeredTraysList, trayApps);
  renderList(els.appsList, otherApps);

  els.claudeEmpty.hidden = codingApps.length !== 0;
  els.registeredTraysEmpty.hidden = trayApps.length !== 0;
  els.appsEmpty.hidden = otherApps.length !== 0;
}

// Flip a Registered Trays entry's autostart flag (issue #456 part 2/2) via
// the same PATCH /api/apps/{id} the rename dialog uses. Re-fetches
// /api/apps on success so the switch reflects the authoritative persisted
// state, not an optimistic local flip.
async function toggleTrayAutostart(a, next) {
  try {
    await jsonApi('/api/apps/' + encodeURIComponent(a.id), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ autostart: next }),
    });
    await fetchApps();
  } catch (exc) {
    apiFailToast('Could not update autostart', exc);
  }
}

// The per-row launch cluster (issue #790) — the only way to start a
// bat-based app now that the row body is inert. Two explicit modes rather
// than a remembered setting: ⚡ opens the CMD window (watch a Streamlit
// boot, read a traceback), 🚫👁 runs the same bat with no window at all
// (the phone-first case, PC unattended). Geometry is the Board backlog's
// ▶/⚡ pair verbatim — `.board-issue-btn.icon-only`, already thumb-sized.
const LAUNCH_MODES = [
  { stealth: false, glyph: 'zap', hint: 'in a visible window', how: '' },
  { stealth: true, glyph: 'eye-off', hint: 'with no window', how: ' in stealth' },
];

function launchActions(a) {
  const row = document.createElement('div');
  row.className = 'app-launch-actions';
  // Tunnel rows carry their URL as a 🔗 here rather than as link text on a
  // row of its own — a cloudflared URL with a `?token=…` on it wrapped to
  // three lines on the phone, and it is never read, only tapped. The
  // href still holds the whole URL, so tap/copy-link/open-in-new-tab all
  // behave; the row's health dot already says whether it is up.
  if (a.kind === 'tunnel') {
    if (a.tunnel_url) {
      const link = document.createElement('a');
      link.className = 'board-issue-btn icon-only app-launch-btn app-tunnel-link';
      link.href = a.tunnel_url;
      link.target = '_blank';
      link.rel = 'noopener';
      link.innerHTML = icon('link');
      link.title = a.tunnel_url;
      link.setAttribute('aria-label', 'Open ' + a.name + ' tunnel');
      row.appendChild(link);
    } else {
      const dead = document.createElement('button');
      dead.type = 'button';
      dead.className = 'board-issue-btn icon-only app-launch-btn';
      dead.innerHTML = icon('link');
      dead.disabled = true;
      dead.title = 'Tunnel not running';
      dead.setAttribute('aria-label', a.name + ' tunnel not running');
      row.appendChild(dead);
    }
  }
  // Tray rows put their autostart switch on this same line rather than in a
  // row of its own — a full-width strip below the card cost a whole line to
  // one 44px control. Unlabelled on purpose: the panel is called Trays and
  // the switch is the only toggle on the row, so the visible word earned
  // nothing. Screen readers still get the full "Autostart <name> at boot".
  if (a.kind === 'tray') {
    row.appendChild(switchEl(!!a.autostart, {
      label: 'Autostart ' + a.name + ' at boot',
      onToggle: function (next, btn) {
        btn.disabled = true;
        toggleTrayAutostart(a, next).finally(function () {
          btn.disabled = false;
        });
      },
    }));
  }
  LAUNCH_MODES.forEach(function (m) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'board-issue-btn icon-only app-launch-btn';
    btn.innerHTML = icon(m.glyph);
    btn.title = 'Launch ' + a.name + ' ' + m.hint;
    btn.setAttribute('aria-label', 'Launch ' + a.name + m.how);
    // The mode is on the element, not just in the closure, so a test (or a
    // future caller) can address a button by what it does rather than by
    // its position in the row.
    btn.dataset.stealth = m.stealth ? '1' : '0';
    btn.addEventListener('click', function () { launchApp(a, undefined, m.stealth); });
    row.appendChild(btn);
  });
  return row;
}

function renderList(host, items) {
  host.innerHTML = '';
  items.forEach(function (a) {
    const li = document.createElement('li');
    li.className = 'app-item';
    li.dataset.id = a.id;

    const main = document.createElement('div');
    main.className = 'app-main';

    // Inert info block (issue #790) — the row body no longer launches;
    // the ⚡ / 🚫👁 pair beside it is the only launch affordance. Same
    // `.launch-btn … inert` shape renderRunningApps already uses, so the
    // typography stays identical to every other list row.
    const launch = document.createElement('div');
    launch.className = 'launch-btn inert';

    // Kind pill and name each get their own line. Sharing one line made a
    // long name wrap *around* the pill, so the title arrived as a ragged
    // two-line block indented under a badge.
    const top = document.createElement('div');
    top.className = 'app-row-kind';
    const dot = document.createElement('span');
    dot.className = 'health-dot';
    // Health is only known for tunnel apps (probed server-side).
    if (a.health === 'up') dot.classList.add('up');
    else if (a.health === 'down') dot.classList.add('down');
    top.appendChild(dot);

    const pill = document.createElement('span');
    pill.className = 'kind-pill';
    pill.textContent = a.kind;
    top.appendChild(pill);
    launch.appendChild(top);

    const name = document.createElement('span');
    name.className = 'app-row-name';
    name.textContent = a.name;
    launch.appendChild(name);

    // The full bat path is long enough to wrap to two or three lines on a
    // phone and is never what you're scanning for — it only matters when
    // you're about to rename or remove the row, so it rides Edit mode with
    // the ✏️/🗑️ rail rather than costing every row the height.
    if (state.editMode) {
      const meta = document.createElement('span');
      meta.className = 'meta';
      meta.textContent = a.bat_path || a.project_dir || '';
      launch.appendChild(meta);
    }

    main.appendChild(launch);
    li.appendChild(main);
    li.appendChild(launchActions(a));

    // Rename + remove are gated behind Jobs tab → Edit mode, so the
    // lists stay icon-free in normal use (no per-row icon inflation).
    // Only the Apps tab's bat-based rows reach renderList — Coding-tab
    // rows render via renderCodingList instead.
    if (state.editMode) {
      const actions = document.createElement('div');
      actions.className = 'row-actions';

      const renameBtn = document.createElement('button');
      renameBtn.type = 'button';
      renameBtn.className = 'icon-btn';
      renameBtn.innerHTML = icon('pencil');
      renameBtn.title = 'Rename';
      renameBtn.setAttribute('aria-label', 'Rename');
      renameBtn.addEventListener('click', function () { openRename(a); });
      actions.appendChild(renameBtn);

      const removeBtn = document.createElement('button');
      removeBtn.type = 'button';
      removeBtn.className = 'icon-btn danger';
      removeBtn.innerHTML = icon('trash-2');
      removeBtn.title = 'Remove';
      removeBtn.setAttribute('aria-label', 'Remove');
      removeBtn.addEventListener('click', function () { removeApp(a); });
      actions.appendChild(removeBtn);

      li.appendChild(actions);
    }

    host.appendChild(li);
  });
}

// Coding-tab launch mode is the ☁️ Detached toggle in the options
// card: checked → 'remote' (detached console window, listed + killable
// here but no phone terminal); unchecked → full-control PTY streamed to
// the phone. The ↺ Resume toggle (issue #151) reopens the agent's own
// session picker; it is orthogonal to Detached (issue #157) — Detached +
// Resume opens the picker in the detached console, Resume alone streams it
// to the phone over a PTY. `agentId` (claude | codex | antigravity |
// copilot) is set by the Coding tile's per-agent button; undefined for
// Apps-tab bat launches.
//
// `stealth` (issue #790) is the Apps/Trays 🚫👁 button: the bat runs with
// no console window on screen. Bat kinds only — a coding session has no
// console window of its own to hide.
export async function launchApp(a, agentId, stealth) {
  const resume = !!(a.kind === 'claude-code' && els.claudeResume &&
    els.claudeResume.getAttribute('aria-checked') === 'true');
  // Detached → 'remote', independent of Resume. The two combine: a
  // Detached+Resume launch renders the agent's picker in the console.
  const mode = (a.kind === 'claude-code' && els.claudeDetached &&
    els.claudeDetached.getAttribute('aria-checked') === 'true') ? 'remote' : null;
  try {
    const opts = { method: 'POST' };
    const payload = {};
    if (mode) payload.mode = mode;
    if (resume) payload.resume = true;
    if (a.kind === 'claude-code') payload.agent = agentId || 'claude';
    else if (stealth) payload.stealth = true;
    // Streamed (pty) coding launches need a starting PTY size. Detached
    // (remote) launches have no PTY, so skip it.
    if (a.kind === 'claude-code' && !mode) {
      // A desktop browser gets a dedicated PC Edge --app window, not an
      // in-page terminal (issue #241); a phone carries its real terminal
      // size so the PTY's first frame is the right width for a ratatui
      // TUI (issue #126) — see applyLaunchSizePayload.
      applyLaunchSizePayload(payload);
    }
    if (Object.keys(payload).length) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify(payload);
    }
    const body = await jsonApi(
      '/api/apps/' + encodeURIComponent(a.id) + '/launch', opts
    );
    // Tag the toast with the agent's label for any non-default agent;
    // resolved against the registry so a new agent needs no change here.
    let agentTag = '';
    if (a.kind === 'claude-code' && body.agent && body.agent !== 'claude') {
      const known = state.agents.find(function (ag) { return ag.id === body.agent; });
      agentTag = ' (' + (known ? known.label : body.agent) + ')';
    }
    // A stealth launch leaves nothing on screen to confirm it worked, so
    // the toast is the only feedback — say the mode out loud (issue #790).
    toast(
      (resume ? 'Resumed ' : 'Launched ') + a.name + agentTag +
        (mode === 'remote' ? ' (detached)' : '') +
        (stealth ? ' (stealth)' : ''),
      'good',
      { icon: resume ? 'rotate-ccw' : (stealth ? 'eye-off' : 'rocket') }
    );
    if (a.kind === 'claude-code' && body.session) {
      // Full-control sessions drop straight into the terminal; detached
      // ones only appear in the running-sessions list. A desktop browser
      // gets its terminal in a dedicated PC Edge window instead of in-page,
      // so it stays on the launcher SPA (issue #241).
      handleLaunchResponse(body.session);
    } else if (a.kind !== 'claude-code') {
      // Non-claude-code: a bat was spawned and is now tracked. Port
      // discovery is racy (Streamlit takes 1-3 s to bind) so poll the
      // running-apps list a few times after the launch.
      fetchRunningApps().catch(function () {});
      setTimeout(function () { fetchRunningApps().catch(function () {}); }, 1500);
      setTimeout(function () { fetchRunningApps().catch(function () {}); }, 4000);
      if (a.kind === 'tunnel') {
        // The tunnel URL takes a few seconds to appear — schedule a refresh.
        setTimeout(fetchApps, 5000);
      }
    }
  } catch (exc) {
    apiFailToast('Launch failed', exc);
  }
}

async function removeApp(a) {
  if (!confirm('Remove ' + a.name + ' from the registry?')) return;
  try {
    await jsonApi('/api/apps/' + encodeURIComponent(a.id), { method: 'DELETE' });
    toast('Removed ' + a.name, 'good');
    await fetchApps();
  } catch (exc) {
    apiFailToast('Remove failed', exc);
  }
}

export async function fetchApps() {
  const body = await jsonApi('/api/apps');
  state.apps = body.apps || [];
  renderApps();
}

// Coding-agent detection — which CLIs are installed. Drives the
// enabled/disabled state of the Coding tab's per-tile launch buttons.
// Best-effort: on failure state.agents keeps its conservative fallback.
export async function fetchAgents() {
  try {
    const body = await jsonApi('/api/agents');
    if (Array.isArray(body.agents) && body.agents.length) {
      state.agents = body.agents;
    }
    // Rides on the same payload (#802) — the VS Code button isn't an agent,
    // but it's detected the same way. Guarded on the type so an older server
    // that omits the field leaves the conservative fallback in place rather
    // than coercing `undefined` to a hard `false`.
    if (typeof body.vscode_available === 'boolean') {
      state.vscodeAvailable = body.vscode_available;
    }
  } catch (exc) {
    logPollFailure('agents fetch failed', exc);
  }
  // The visibility list is keyed off the registry, so it renders once the
  // agents are known (boot order: fetchConfig → fetchAgents). Rendering from
  // the conservative fallback on a failed fetch is fine — same ids, same
  // labels.
  renderAgentVisibility();
}

// -------------------------------------------------- running apps panel
// Apps spawned from the launcher (bats). Mirrors the Claude Code tab's
// Running sessions panel: list, tap-to-open over Tailscale, per-app stop.
export function renderRunningApps() {
  const host = els.runningAppsList;
  host.innerHTML = '';
  els.runningAppsEmpty.hidden = state.runningApps.length !== 0;
  renderHomeHead();

  state.runningApps.forEach(function (r) {
    const li = document.createElement('li');
    li.className = 'app-item session-item';
    li.dataset.pid = r.pid;

    const main = document.createElement('div');
    main.className = 'app-main';

    // Inert info block — the row itself isn't tappable; actions are
    // the two buttons. Reuses .launch-btn styling minus the click.
    const info = document.createElement('div');
    info.className = 'launch-btn session-open inert';

    const head = document.createElement('div');
    head.className = 'session-head';
    const dot = document.createElement('span');
    dot.className = 'health-dot ' + ((r.alive && r.port) ? 'up' : 'down');
    head.appendChild(dot);
    const pill = document.createElement('span');
    pill.className = 'kind-pill';
    pill.textContent = r.kind;
    head.appendChild(pill);
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = r.name;
    head.appendChild(name);
    info.appendChild(head);

    const meta = document.createElement('span');
    meta.className = 'meta';
    const ago = fmtAgo(r.started_at);
    const parts = [];
    if (ago) parts.push('up ' + ago);
    parts.push(r.port ? ':' + r.port : 'binding…');
    parts.push('pid ' + r.pid);
    meta.textContent = parts.join(' · ');
    info.appendChild(meta);
    main.appendChild(info);
    li.appendChild(main);

    const actions = document.createElement('div');
    actions.className = 'row-actions session-actions';

    const openBtn = document.createElement('button');
    openBtn.type = 'button';
    openBtn.className = 'icon-btn action-open';
    openBtn.innerHTML = icon('globe');
    openBtn.setAttribute('aria-label', 'Open app');
    if (r.url) {
      openBtn.title = 'Open ' + r.url;
      openBtn.addEventListener('click', function () {
        window.open(r.url, '_blank', 'noopener,noreferrer');
      });
    } else {
      openBtn.disabled = true;
      openBtn.title = r.port
        ? 'Set tailnet_host in config/config.json to enable Open'
        : 'Waiting for the app to bind a port…';
    }
    actions.appendChild(openBtn);

    const stopBtn = document.createElement('button');
    stopBtn.type = 'button';
    stopBtn.className = 'icon-btn action-stop-close';
    stopBtn.innerHTML = icon('square');
    stopBtn.title = 'Stop ' + r.name;
    stopBtn.setAttribute('aria-label', 'Stop app');
    stopBtn.addEventListener('click', function () { stopAppInstance(r); });
    actions.appendChild(stopBtn);

    li.appendChild(actions);
    host.appendChild(li);
  });
}

async function stopAppInstance(r) {
  if (!confirm('Stop ' + r.name + ' (pid ' + r.pid + ')?')) return;
  try {
    await jsonApi(
      '/api/apps/' + encodeURIComponent(r.app_id) +
        '/instances/' + r.pid + '/stop',
      { method: 'POST' }
    );
    toast('Stopped ' + r.name + '.', 'good', { icon: 'octagon-x' });
    // Optimistic removal — the next poll confirms it's gone.
    state.runningApps = state.runningApps.filter(function (x) {
      return !(x.app_id === r.app_id && x.pid === r.pid);
    });
    renderRunningApps();
  } catch (exc) {
    apiFailToast('Stop failed', exc);
  }
}

export async function fetchRunningApps() {
  // Apps-tab-only poll: pause while another tab is showing so the
  // background interval doesn't hit the API for an invisible panel.
  if (state.tab !== 'apps') return;
  try {
    const body = await jsonApi('/api/apps/running');
    state.runningApps = body.running || [];
    renderRunningApps();
  } catch (exc) {
    // Best-effort poll — don't spam toasts.
    logPollFailure('running apps fetch failed', exc);
  }
}

export function wireApps() {
  // Refresh the running-apps panel the moment the Apps tab is opened —
  // the background poll pauses while the tab is hidden.
  els.tabApps.addEventListener('click', function () {
    fetchRunningApps().catch(function () {});
  });
  wireCoding();
  wireRenameDialog();
  wireScanDialog();
}
