/* Apps tab: registry list, launch, scan dialog, rename dialog,
 * running-listeners panel.
 *
 * All of this is on the Apps tab — except renderApps also feeds the
 * Coding tab's project list (the `claude-code` rows), which renders as
 * bare folder-name tiles with one launch button per coding agent.
 */

import { els, state } from './state.js';
import { jsonApi, toast } from './api.js';
import { fetchSessions, fmtAgo } from './sessions.js';
import { openTerminal } from './terminal.js';

// ----------------------------------------------------------- apps list
export function renderApps() {
  const codingApps = state.apps.filter(function (a) { return a.kind === 'claude-code'; });
  const otherApps = state.apps.filter(function (a) { return a.kind !== 'claude-code'; });

  renderCodingList(els.claudeList, codingApps);
  renderList(els.appsList, otherApps);

  els.claudeEmpty.hidden = codingApps.length !== 0;
  els.appsEmpty.hidden = otherApps.length !== 0;
}

// ------------------------------------------------------ Coding tab tiles
// A Coding tile shows only the bare on-disk folder name plus one icon
// button per coding agent (Claude Code, Antigravity, GitHub Copilot).
// An agent's button is disabled with a hover hint when its CLI isn't
// installed. Coding rows are disk-scanned, so they carry no rename/
// remove controls — Settings → Edit mode does not apply here.
function renderCodingList(host, items) {
  host.innerHTML = '';
  items.forEach(function (a) {
    const li = document.createElement('li');
    li.className = 'app-item coding-item';
    li.dataset.id = a.id;

    const main = document.createElement('div');
    main.className = 'app-main';
    const name = document.createElement('div');
    name.className = 'coding-name';
    name.textContent = a.name;   // raw folder name, exactly as on disk
    main.appendChild(name);
    li.appendChild(main);

    const actions = document.createElement('div');
    actions.className = 'row-actions agent-actions';
    state.agents.forEach(function (agent) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'icon-btn agent-btn';
      btn.dataset.agent = agent.id;
      const icon = document.createElement('img');
      icon.className = 'agent-icon';
      icon.src = '/static/icons/' + agent.id + '.svg';
      icon.alt = agent.label;
      btn.appendChild(icon);
      if (agent.available) {
        btn.title = 'Launch ' + agent.label;
        btn.setAttribute('aria-label', 'Launch ' + agent.label);
        btn.addEventListener('click', function () { launchApp(a, agent.id); });
      } else {
        btn.disabled = true;
        btn.title = agent.label + ' is not installed';
        btn.setAttribute('aria-label', agent.label + ' is not installed');
      }
      actions.appendChild(btn);
    });

    // GitHub repo icon — opens the project's repo in a new browser tab.
    // Spawns no process and creates no session. Disabled with a hover
    // hint when the project has no GitHub remote (a.repo_url is unset).
    const ghBtn = document.createElement('button');
    ghBtn.type = 'button';
    ghBtn.className = 'icon-btn agent-btn';
    const ghIcon = document.createElement('img');
    ghIcon.className = 'agent-icon';
    ghIcon.src = '/static/icons/github.svg';
    ghIcon.alt = 'GitHub';
    ghBtn.appendChild(ghIcon);
    if (a.repo_url) {
      ghBtn.title = 'Open GitHub repo';
      ghBtn.setAttribute('aria-label', 'Open GitHub repo');
      ghBtn.addEventListener('click', function () {
        window.open(a.repo_url, '_blank', 'noopener,noreferrer');
      });
    } else {
      ghBtn.disabled = true;
      ghBtn.title = 'No GitHub remote';
      ghBtn.setAttribute('aria-label', 'No GitHub remote');
    }
    actions.appendChild(ghBtn);

    li.appendChild(actions);
    host.appendChild(li);
  });
}

function renderList(host, items) {
  host.innerHTML = '';
  items.forEach(function (a) {
    const li = document.createElement('li');
    li.className = 'app-item';
    li.dataset.id = a.id;

    const main = document.createElement('div');
    main.className = 'app-main';

    const launch = document.createElement('button');
    launch.type = 'button';
    launch.className = 'launch-btn';

    const top = document.createElement('div');
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

    const name = document.createElement('span');
    name.textContent = a.name;
    top.appendChild(name);
    launch.appendChild(top);

    const meta = document.createElement('span');
    meta.className = 'meta';
    meta.textContent = a.bat_path || a.project_dir || '';
    launch.appendChild(meta);

    launch.addEventListener('click', function () { launchApp(a); });
    main.appendChild(launch);

    if (a.kind === 'tunnel') {
      const tr = document.createElement('div');
      tr.className = 'tunnel-row';
      if (a.tunnel_url) {
        const link = document.createElement('a');
        link.href = a.tunnel_url;
        link.target = '_blank';
        link.rel = 'noopener';
        link.textContent = '📡 ' + a.tunnel_url;
        tr.appendChild(link);
      } else {
        const span = document.createElement('span');
        span.textContent = '📡 Tunnel not running';
        tr.appendChild(span);
      }
      main.appendChild(tr);
    }

    li.appendChild(main);

    // Rename + remove are gated behind Settings → Edit mode, so the
    // lists stay icon-free in normal use (no per-row icon inflation).
    // Only the Apps tab's bat-based rows reach renderList — Coding-tab
    // rows render via renderCodingList instead.
    if (state.editMode) {
      const actions = document.createElement('div');
      actions.className = 'row-actions';

      const renameBtn = document.createElement('button');
      renameBtn.type = 'button';
      renameBtn.className = 'icon-btn';
      renameBtn.textContent = '✏️';
      renameBtn.title = 'Rename';
      renameBtn.setAttribute('aria-label', 'Rename');
      renameBtn.addEventListener('click', function () { openRename(a); });
      actions.appendChild(renameBtn);

      const removeBtn = document.createElement('button');
      removeBtn.type = 'button';
      removeBtn.className = 'icon-btn danger';
      removeBtn.textContent = '🗑️';
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
// the phone. `agentId` (claude | antigravity | copilot) is set by the
// Coding tile's per-agent button; undefined for Apps-tab bat launches.
async function launchApp(a, agentId) {
  const mode = (a.kind === 'claude-code' && els.claudeDetached &&
    els.claudeDetached.checked) ? 'remote' : null;
  try {
    const opts = { method: 'POST' };
    const payload = {};
    if (mode) payload.mode = mode;
    if (a.kind === 'claude-code') payload.agent = agentId || 'claude';
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
    toast(
      '🚀 Launched ' + a.name + agentTag +
        (mode === 'remote' ? ' (detached)' : ''),
      'good'
    );
    if (a.kind === 'claude-code' && body.session) {
      fetchSessions().catch(function () {});
      // Full-control sessions drop straight into the terminal; detached
      // ones only appear in the running-sessions list.
      if (body.session.kind !== 'remote') openTerminal(body.session);
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
    toast('Launch failed: ' + (exc.message || exc), 'error');
  }
}

async function removeApp(a) {
  if (!confirm('Remove ' + a.name + ' from the registry?')) return;
  try {
    await jsonApi('/api/apps/' + encodeURIComponent(a.id), { method: 'DELETE' });
    toast('Removed ' + a.name, 'good');
    await fetchApps();
  } catch (exc) {
    toast('Remove failed: ' + (exc.message || exc), 'error');
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
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      console.warn('agents fetch failed', exc);
    }
  }
}

// -------------------------------------------------- running apps panel
// Apps spawned from the launcher (bats). Mirrors the Claude Code tab's
// Running sessions panel: list, tap-to-open over Tailscale, per-app stop.
export function renderRunningApps() {
  const host = els.runningAppsList;
  host.innerHTML = '';
  els.runningAppsEmpty.hidden = state.runningApps.length !== 0;

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
    openBtn.textContent = '🌐';
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
    stopBtn.textContent = '⏹️';
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
    toast('🛑 Stopped ' + r.name + '.', 'good');
    // Optimistic removal — the next poll confirms it's gone.
    state.runningApps = state.runningApps.filter(function (x) {
      return !(x.app_id === r.app_id && x.pid === r.pid);
    });
    renderRunningApps();
  } catch (exc) {
    toast('Stop failed: ' + (exc.message || exc), 'error');
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
    if (String(exc.message) !== 'auth required') {
      // Best-effort poll — don't spam toasts.
      console.warn('running apps fetch failed', exc);
    }
  }
}

// ----------------------------------------------------------- rename dialog
let renameTargetId = null;

function openRename(a) {
  renameTargetId = a.id;
  els.renameInput.value = a.name;
  if (els.renameDialog.showModal) els.renameDialog.showModal();
}

function wireRenameDialog() {
  els.renameCancel.addEventListener('click', function () {
    if (els.renameDialog.close) els.renameDialog.close();
  });
  els.renameForm.addEventListener('submit', async function (ev) {
    ev.preventDefault();
    const name = els.renameInput.value.trim();
    if (!name || !renameTargetId) return;
    try {
      await jsonApi('/api/apps/' + encodeURIComponent(renameTargetId), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      if (els.renameDialog.close) els.renameDialog.close();
      await fetchApps();
    } catch (exc) {
      toast('Rename failed: ' + (exc.message || exc), 'error');
    }
  });
}

// ----------------------------------------------------------- scan dialog
async function runScan() {
  try {
    const body = await jsonApi('/api/apps/scan', { method: 'POST' });
    state.pendingScan = body.new || [];
    renderScanResults();
    if (els.scanDialog.showModal) els.scanDialog.showModal();
    else els.scanDialog.hidden = false;
  } catch (exc) {
    toast('Scan failed: ' + (exc.message || exc), 'error');
  }
}

function renderScanResults() {
  els.scanResults.innerHTML = '';
  if (!state.pendingScan.length) {
    const p = document.createElement('p');
    p.className = 'muted small';
    p.textContent = 'No new entries.';
    els.scanResults.appendChild(p);
    return;
  }
  const byKind = {};
  state.pendingScan.forEach(function (c) {
    (byKind[c.kind] = byKind[c.kind] || []).push(c);
  });
  Object.keys(byKind).sort().forEach(function (kind) {
    const section = document.createElement('div');
    section.className = 'scan-section';
    const h = document.createElement('h3');
    h.textContent = kind;
    section.appendChild(h);
    byKind[kind].forEach(function (c) {
      const label = document.createElement('label');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.value = c.id;
      cb.checked = true;
      label.appendChild(cb);
      const body = document.createElement('div');
      const name = document.createElement('div');
      name.textContent = c.name;
      const meta = document.createElement('div');
      meta.className = 'meta';
      meta.textContent = c.bat_path || c.project_dir || '';
      body.appendChild(name);
      body.appendChild(meta);
      label.appendChild(body);
      section.appendChild(label);
    });
    els.scanResults.appendChild(section);
  });
}

function wireScanDialog() {
  els.rescanBtn.addEventListener('click', runScan);
  els.scanCancel.addEventListener('click', function () {
    if (els.scanDialog.close) els.scanDialog.close();
  });
  els.scanSave.addEventListener('click', async function () {
    const checked = Array.from(els.scanResults.querySelectorAll('input[type="checkbox"]:checked'));
    const ids = checked.map(function (cb) { return cb.value; });
    if (!ids.length) {
      toast('Nothing selected.', 'error');
      return;
    }
    try {
      const body = await jsonApi('/api/apps/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids }),
      });
      toast('Added ' + (body.added || []).length + ' entry(ies).', 'good');
      if (els.scanDialog.close) els.scanDialog.close();
      await fetchApps();
    } catch (exc) {
      toast('Save failed: ' + (exc.message || exc), 'error');
    }
  });
}

// ----------------------------------------------------------- listeners panel (Apps tab)
export async function fetchListeners() {
  try {
    const body = await jsonApi('/api/ports/probe');
    renderListeners(body.listeners || []);
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      // Best-effort poll — don't spam toasts.
      console.warn('listeners fetch failed', exc);
    }
  }
}

function renderListeners(items) {
  const host = els.listenersList;
  host.innerHTML = '';
  els.listenersEmpty.hidden = items.length !== 0;
  items.forEach(function (l) {
    const row = document.createElement('div');
    row.className = 'listener-row';

    const meta = document.createElement('div');
    const strong = document.createElement('strong');
    strong.textContent = l.app || l.name || ('port ' + l.port);
    const sub = document.createElement('span');
    sub.className = 'meta';
    sub.textContent = ' :' + l.port + ' · pid ' + l.pid + ' · ' + (l.name || '?');
    meta.appendChild(strong);
    meta.appendChild(sub);
    row.appendChild(meta);

    const kill = document.createElement('button');
    kill.type = 'button';
    kill.textContent = '🛑 Kill';
    kill.addEventListener('click', async function () {
      const label = l.app || ('port ' + l.port);
      if (!confirm('Kill ' + label + '?\n\npid ' + l.pid + ' on :' + l.port)) return;
      try {
        const r = await jsonApi('/api/ports/' + l.port + '/kill', { method: 'POST' });
        toast('Killed ' + (r.killed || []).length + ' pid(s) on :' + l.port + '.', 'good');
        fetchListeners();
      } catch (exc) {
        toast('Kill failed: ' + (exc.message || exc), 'error');
      }
    });
    row.appendChild(kill);
    host.appendChild(row);
  });
}

export function wireApps() {
  els.refreshListeners.addEventListener('click', fetchListeners);
  els.refreshRunningApps.addEventListener('click', fetchRunningApps);
  // Refresh the running-apps panel the moment the Apps tab is opened —
  // the background poll pauses while the tab is hidden.
  els.tabApps.addEventListener('click', function () {
    fetchRunningApps().catch(function () {});
  });
  wireRenameDialog();
  wireScanDialog();
}
