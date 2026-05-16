/* Apps tab: registry list, launch, scan dialog, generate-bats dialog,
 * rename dialog, running-listeners panel.
 *
 * All of this is on the Apps tab — except renderApps also feeds the
 * Claude Code projects list on the Claude tab (split by `kind`).
 */

import { els, state } from './state.js';
import { jsonApi, toast } from './api.js';
import { fetchSessions } from './sessions.js';
import { openTerminal } from './terminal.js';

// ----------------------------------------------------------- apps list
export function renderApps() {
  const claudeApps = state.apps.filter(function (a) { return a.kind === 'claude-code'; });
  const otherApps = state.apps.filter(function (a) { return a.kind !== 'claude-code'; });

  renderList(els.claudeList, claudeApps);
  renderList(els.appsList, otherApps);

  els.claudeEmpty.hidden = claudeApps.length !== 0;
  els.appsEmpty.hidden = otherApps.length !== 0;
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

// Claude Code launch mode is the ☁️ Detached toggle in the options
// card: checked → 'remote' (detached console window, cloud-driven,
// listed + killable here but no phone terminal); unchecked →
// full-control PTY streamed to the phone.
async function launchApp(a) {
  const mode = (a.kind === 'claude-code' && els.claudeDetached &&
    els.claudeDetached.checked) ? 'remote' : null;
  try {
    const opts = { method: 'POST' };
    if (mode) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify({ mode: mode });
    }
    const body = await jsonApi(
      '/api/apps/' + encodeURIComponent(a.id) + '/launch', opts
    );
    toast(
      '🚀 Launched ' + a.name + (mode === 'remote' ? ' (detached)' : ''),
      'good'
    );
    if (a.kind === 'claude-code' && body.session) {
      fetchSessions().catch(function () {});
      // Full-control sessions drop straight into the terminal; detached
      // ones only appear in the running-sessions list.
      if (body.session.kind !== 'remote') openTerminal(body.session);
    } else if (a.kind === 'tunnel') {
      // The tunnel URL takes a few seconds to appear — schedule a refresh.
      setTimeout(fetchApps, 5000);
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

// ----------------------------------------------------------- generate-bat dialog
async function openGenDialog() {
  try {
    const body = await jsonApi('/api/claude-code/generate');
    state.genPreview = body;
    renderGenPreview();
    if (els.genDialog.showModal) els.genDialog.showModal();
  } catch (exc) {
    toast('Preview failed: ' + (exc.message || exc), 'error');
  }
}

function renderGenPreview() {
  els.genResults.innerHTML = '';
  const g = state.genPreview;
  if (!g) return;

  const new_bats = g.workspaces.filter(function (w) { return !w.bat_exists; });
  const existing = g.workspaces.filter(function (w) { return w.bat_exists; });

  if (new_bats.length) {
    const section = document.createElement('div');
    section.className = 'scan-section';
    const h = document.createElement('h3');
    h.textContent = 'New BAT files (will be created)';
    section.appendChild(h);
    new_bats.forEach(function (w) {
      const label = document.createElement('label');
      label.innerHTML = '<input type="checkbox" checked disabled> <div><div>' + w.bat_name + '</div><div class="meta">' + w.project_dir + '</div></div>';
      section.appendChild(label);
    });
    els.genResults.appendChild(section);
  }

  if (existing.length) {
    const section = document.createElement('div');
    section.className = 'scan-section';
    const h = document.createElement('h3');
    h.textContent = 'Existing BAT files (tick to overwrite)';
    section.appendChild(h);
    existing.forEach(function (w) {
      const label = document.createElement('label');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.value = w.name;
      cb.dataset.kind = 'overwrite';
      label.appendChild(cb);
      const body = document.createElement('div');
      body.innerHTML = '<div>' + w.bat_name + '</div><div class="meta">' + w.project_dir + '</div>';
      label.appendChild(body);
      section.appendChild(label);
    });
    els.genResults.appendChild(section);
  }

  if (g.orphans && g.orphans.length) {
    const section = document.createElement('div');
    section.className = 'scan-section';
    const h = document.createElement('h3');
    h.textContent = 'Orphan BATs (tick to create matching .code-workspace)';
    section.appendChild(h);
    g.orphans.forEach(function (o) {
      const label = document.createElement('label');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.value = o.name;
      cb.dataset.kind = 'create_ws';
      cb.checked = true;
      label.appendChild(cb);
      const body = document.createElement('div');
      body.innerHTML = '<div>' + o.ws_name + '</div><div class="meta">→ ' + o.project_dir + '</div>';
      label.appendChild(body);
      section.appendChild(label);
    });
    els.genResults.appendChild(section);
  }

  if (!new_bats.length && !existing.length && !(g.orphans || []).length) {
    const p = document.createElement('p');
    p.className = 'muted small';
    p.textContent = 'No workspaces or orphan BATs found in ' + g.projects_dir;
    els.genResults.appendChild(p);
  }
}

function wireGenDialog() {
  els.genBatBtn.addEventListener('click', openGenDialog);
  els.genCancel.addEventListener('click', function () {
    if (els.genDialog.close) els.genDialog.close();
  });
  els.genRun.addEventListener('click', async function () {
    const overwrite = Array.from(
      els.genResults.querySelectorAll('input[data-kind="overwrite"]:checked')
    ).map(function (cb) { return cb.value; });
    const create_ws = Array.from(
      els.genResults.querySelectorAll('input[data-kind="create_ws"]:checked')
    ).map(function (cb) { return cb.value; });
    try {
      const body = await jsonApi('/api/claude-code/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ overwrite, create_ws }),
      });
      const summary = [
        body.created.length ? 'created ' + body.created.length : null,
        body.overwritten.length ? 'overwrote ' + body.overwritten.length : null,
        body.ws_created.length ? 'created workspace ' + body.ws_created.length : null,
      ].filter(Boolean).join(' · ');
      toast(summary || 'Nothing to do.', summary ? 'good' : 'error');
      if (body.errors && body.errors.length) {
        body.errors.forEach(function (e) { toast(e, 'error'); });
      }
      if (els.genDialog.close) els.genDialog.close();
    } catch (exc) {
      toast('Generate failed: ' + (exc.message || exc), 'error');
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
  wireRenameDialog();
  wireScanDialog();
  wireGenDialog();
}
