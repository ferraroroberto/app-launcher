/* Launcher hub — single-page app.
 *
 * State:
 *   state.tab          — 'claude' | 'apps'
 *   state.config       — /api/config payload (claude flags + scan paths)
 *   state.apps         — array from /api/apps
 *   state.health       — { [appId]: 'up'|'down'|null }
 *   state.pendingScan  — array from /api/apps/scan, surfaced in scan dialog
 *   state.genPreview   — { workspaces, orphans } for the generate dialog
 *
 * Auth: a bearer token is stored in localStorage. The page extracts it
 * from ?token=… on first load and strips it from the URL. On 401, the
 * login overlay shows; password → /api/login → bearer token.
 */

(function () {
  'use strict';

  const TOKEN_KEY = 'launcher.token';
  const TUNNEL_POLL_MS = 4000;       // refresh tunnel-kind URLs + health
  const SESSIONS_POLL_MS = 5000;     // refresh running Claude Code sessions
  const LISTENERS_POLL_MS = 5000;    // refresh running apps (port listeners)

  const state = {
    tab: 'claude',
    config: null,
    apps: [],
    sessions: [],
    pendingScan: [],
    genPreview: null,
  };

  // ----------------------------------------------------------------- DOM
  const els = {
    tabClaude: document.getElementById('tabClaude'),
    tabApps: document.getElementById('tabApps'),
    paneClaude: document.getElementById('paneClaude'),
    paneApps: document.getElementById('paneApps'),

    claudeModel: document.getElementById('claudeModel'),
    claudeEffort: document.getElementById('claudeEffort'),
    claudeVerbose: document.getElementById('claudeVerbose'),
    claudeDebug: document.getElementById('claudeDebug'),
    claudeFlagsPreview: document.getElementById('claudeFlagsPreview'),
    claudeList: document.getElementById('claudeList'),
    claudeEmpty: document.getElementById('claudeEmpty'),
    sessionsList: document.getElementById('sessionsList'),
    sessionsEmpty: document.getElementById('sessionsEmpty'),
    refreshSessions: document.getElementById('refreshSessions'),
    appsList: document.getElementById('appsList'),
    appsEmpty: document.getElementById('appsEmpty'),

    rescanBtn: document.getElementById('rescanBtn'),
    settingsBtn: document.getElementById('settingsBtn'),
    settingsPanel: document.getElementById('settingsPanel'),
    projectsDir: document.getElementById('projectsDir'),
    appsScanRoot: document.getElementById('appsScanRoot'),
    saveSettings: document.getElementById('saveSettings'),
    listenersList: document.getElementById('listenersList'),
    listenersEmpty: document.getElementById('listenersEmpty'),
    refreshListeners: document.getElementById('refreshListeners'),
    statusReadout: document.getElementById('statusReadout'),

    scanDialog: document.getElementById('scanDialog'),
    scanResults: document.getElementById('scanResults'),
    scanCancel: document.getElementById('scanCancel'),
    scanSave: document.getElementById('scanSave'),

    genBatBtn: document.getElementById('genBatBtn'),
    genDialog: document.getElementById('genDialog'),
    genResults: document.getElementById('genResults'),
    genCancel: document.getElementById('genCancel'),
    genRun: document.getElementById('genRun'),

    renameDialog: document.getElementById('renameDialog'),
    renameForm: document.getElementById('renameForm'),
    renameInput: document.getElementById('renameInput'),
    renameCancel: document.getElementById('renameCancel'),

    toast: document.getElementById('toast'),

    loginOverlay: document.getElementById('loginOverlay'),
    loginForm: document.getElementById('loginForm'),
    loginPassword: document.getElementById('loginPassword'),
    loginError: document.getElementById('loginError'),
  };

  // ----------------------------------------------------------- auth utils
  function tokenFromUrl() {
    const params = new URLSearchParams(window.location.search);
    const t = (params.get('token') || '').trim();
    if (!t) return null;
    params.delete('token');
    const newQuery = params.toString();
    const newUrl =
      window.location.pathname +
      (newQuery ? '?' + newQuery : '') +
      window.location.hash;
    window.history.replaceState({}, '', newUrl);
    return t;
  }
  function readToken() { return localStorage.getItem(TOKEN_KEY) || ''; }
  function writeToken(t) { if (t) localStorage.setItem(TOKEN_KEY, t); }

  async function api(path, opts) {
    opts = opts || {};
    const headers = new Headers(opts.headers || {});
    const token = readToken();
    if (token) headers.set('Authorization', 'Bearer ' + token);
    const res = await fetch(path, Object.assign({}, opts, { headers }));
    if (res.status === 401) {
      showLogin();
      throw new Error('auth required');
    }
    return res;
  }
  async function jsonApi(path, opts) {
    const res = await api(path, opts);
    let body = null;
    try { body = await res.json(); } catch (_) { body = null; }
    if (!res.ok) {
      const detail = (body && body.detail) || ('HTTP ' + res.status);
      const err = new Error(detail);
      err.status = res.status;
      err.body = body;
      throw err;
    }
    return body;
  }

  // ----------------------------------------------------------- login UI
  function showLogin() {
    if (!els.loginOverlay) return;
    els.loginOverlay.hidden = false;
    els.loginPassword.value = '';
    els.loginPassword.focus();
  }
  function hideLogin() {
    if (els.loginOverlay) els.loginOverlay.hidden = true;
  }
  els.loginForm.addEventListener('submit', async function (ev) {
    ev.preventDefault();
    els.loginError.hidden = true;
    const password = els.loginPassword.value;
    try {
      const res = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password }),
      });
      const body = await res.json().catch(function () { return null; });
      if (!res.ok || !body || !body.token) {
        const msg = (body && body.detail) || 'Login failed';
        els.loginError.textContent = msg;
        els.loginError.hidden = false;
        return;
      }
      writeToken(body.token);
      hideLogin();
      boot();
    } catch (exc) {
      els.loginError.textContent = String(exc.message || exc);
      els.loginError.hidden = false;
    }
  });

  // ----------------------------------------------------------- toast
  let toastTimer = null;
  function toast(msg, kind) {
    els.toast.textContent = msg;
    els.toast.className = 'toast ' + (kind || '');
    els.toast.hidden = false;
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {
      els.toast.hidden = true;
    }, kind === 'error' ? 4500 : 2200);
  }

  // ----------------------------------------------------------- tabs
  function setTab(tab) {
    state.tab = tab;
    els.tabClaude.classList.toggle('active', tab === 'claude');
    els.tabApps.classList.toggle('active', tab === 'apps');
    els.paneClaude.hidden = tab !== 'claude';
    els.paneApps.hidden = tab !== 'apps';
  }
  els.tabClaude.addEventListener('click', function () { setTab('claude'); });
  els.tabApps.addEventListener('click', function () { setTab('apps'); });

  // ----------------------------------------------------------- claude options
  function renderClaudeOptions() {
    const c = state.config && state.config.claude;
    if (!c) return;
    els.claudeModel.innerHTML = '';
    (c.models_available || []).forEach(function (m) {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = m.charAt(0).toUpperCase() + m.slice(1);
      b.dataset.value = m;
      if (m === c.model) b.classList.add('active');
      b.addEventListener('click', function () {
        patchConfig({ claude_model: m });
      });
      els.claudeModel.appendChild(b);
    });
    els.claudeEffort.innerHTML = '';
    (c.efforts_available || []).forEach(function (e) {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = e === 'off' ? 'Off' : e.charAt(0).toUpperCase() + e.slice(1);
      b.dataset.value = e;
      if (e === c.effort) b.classList.add('active');
      b.addEventListener('click', function () {
        patchConfig({ claude_effort: e });
      });
      els.claudeEffort.appendChild(b);
    });
    els.claudeVerbose.checked = !!c.verbose;
    els.claudeDebug.checked = !!c.debug;
    els.claudeFlagsPreview.textContent = 'claude ' + (c.computed_flags || '');
  }
  els.claudeVerbose.addEventListener('change', function () {
    patchConfig({ claude_verbose: els.claudeVerbose.checked });
  });
  els.claudeDebug.addEventListener('change', function () {
    patchConfig({ claude_debug: els.claudeDebug.checked });
  });

  async function patchConfig(patch) {
    try {
      await jsonApi('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      });
      await fetchConfig();
    } catch (exc) {
      toast('Save failed: ' + (exc.message || exc), 'error');
    }
  }

  // ----------------------------------------------------------- apps list rendering
  function renderApps() {
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

      const actions = document.createElement('div');
      actions.className = 'row-actions';
      const renameBtn = document.createElement('button');
      renameBtn.type = 'button';
      renameBtn.className = 'icon-btn';
      renameBtn.textContent = '✏️';
      renameBtn.title = 'Rename';
      renameBtn.setAttribute('aria-label', 'Rename');
      renameBtn.addEventListener('click', function () { openRename(a); });
      const removeBtn = document.createElement('button');
      removeBtn.type = 'button';
      removeBtn.className = 'icon-btn danger';
      removeBtn.textContent = '🗑️';
      removeBtn.title = 'Remove';
      removeBtn.setAttribute('aria-label', 'Remove');
      removeBtn.addEventListener('click', function () { removeApp(a); });
      actions.appendChild(renameBtn);
      actions.appendChild(removeBtn);
      li.appendChild(actions);

      host.appendChild(li);
    });
  }

  async function launchApp(a) {
    try {
      await jsonApi('/api/apps/' + encodeURIComponent(a.id) + '/launch', { method: 'POST' });
      toast('🚀 Launched ' + a.name, 'good');
      // After a tunnel launch the URL takes a few seconds to appear —
      // schedule a refresh.
      if (a.kind === 'tunnel') {
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

  // ----------------------------------------------------------- claude sessions
  function fmtAgo(epochSeconds) {
    if (!epochSeconds) return '';
    const secs = Math.max(0, Math.floor(Date.now() / 1000 - epochSeconds));
    if (secs < 60) return secs + 's';
    const mins = Math.floor(secs / 60);
    if (mins < 60) return mins + 'm';
    const hrs = Math.floor(mins / 60);
    return hrs + 'h ' + (mins % 60) + 'm';
  }

  function renderSessions() {
    const host = els.sessionsList;
    host.innerHTML = '';
    els.sessionsEmpty.hidden = state.sessions.length !== 0;

    state.sessions.forEach(function (s) {
      const li = document.createElement('li');
      li.className = 'app-item';

      const main = document.createElement('div');
      main.className = 'app-main';
      const body = document.createElement('div');
      body.className = 'session-body';

      const head = document.createElement('div');
      head.className = 'session-head';
      const dot = document.createElement('span');
      dot.className = 'health-dot up';
      head.appendChild(dot);
      const name = document.createElement('span');
      name.className = 'name';
      name.textContent = s.name;
      head.appendChild(name);
      body.appendChild(head);

      // Console title — Claude Code's task summary. The one thing that
      // tells two sessions in the same repo apart.
      if (s.title) {
        const titleEl = document.createElement('span');
        titleEl.className = 'session-title';
        titleEl.textContent = s.title;
        body.appendChild(titleEl);
      }

      const meta = document.createElement('span');
      meta.className = 'meta';
      const ago = fmtAgo(s.started_at);
      meta.textContent = 'pid ' + s.pid + (ago ? ' · up ' + ago : '') +
        ' · ' + s.project_dir;
      body.appendChild(meta);
      main.appendChild(body);
      li.appendChild(main);

      const actions = document.createElement('div');
      actions.className = 'row-actions';
      const stopBtn = document.createElement('button');
      stopBtn.type = 'button';
      stopBtn.className = 'icon-btn danger';
      stopBtn.textContent = '⏹️';
      stopBtn.title = 'Stop session';
      stopBtn.setAttribute('aria-label', 'Stop session');
      stopBtn.addEventListener('click', function () { stopSession(s); });
      actions.appendChild(stopBtn);
      li.appendChild(actions);

      host.appendChild(li);
    });
  }

  async function stopSession(s) {
    if (!confirm('Stop the Claude Code session "' + s.name + '" (pid ' + s.pid + ')?\n\n' +
        'It tries Ctrl+C first, then force-quits if it does not exit.')) {
      return;
    }
    try {
      const body = await jsonApi(
        '/api/claude-code/sessions/' + s.pid + '/stop', { method: 'POST' }
      );
      const how = body.method === 'ctrl_c' ? 'quit cleanly'
        : body.method === 'gone' ? 'already gone'
        : 'force-stopped';
      toast('🛑 ' + s.name + ' ' + how + '.', 'good');
      await fetchSessions();
    } catch (exc) {
      toast('Stop failed: ' + (exc.message || exc), 'error');
    }
  }

  async function fetchSessions() {
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

  els.refreshSessions.addEventListener('click', fetchSessions);

  // ----------------------------------------------------------- rename dialog
  let renameTargetId = null;
  function openRename(a) {
    renameTargetId = a.id;
    els.renameInput.value = a.name;
    if (els.renameDialog.showModal) els.renameDialog.showModal();
  }
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

  // ----------------------------------------------------------- scan dialog
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

  // ----------------------------------------------------------- generate-bat dialog
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

  // ----------------------------------------------------------- settings
  els.settingsBtn.addEventListener('click', function () {
    els.settingsPanel.open = !els.settingsPanel.open;
  });
  els.saveSettings.addEventListener('click', async function () {
    const patch = {
      projects_dir: els.projectsDir.value.trim(),
      apps_scan_root: els.appsScanRoot.value.trim(),
    };
    await patchConfig(patch);
    toast('Settings saved.', 'good');
  });

  // ----------------------------------------------------------- running apps
  els.refreshListeners.addEventListener('click', fetchListeners);

  async function fetchListeners() {
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

  // ----------------------------------------------------------- fetchers
  async function fetchConfig() {
    const body = await jsonApi('/api/config');
    state.config = body;
    els.projectsDir.value = body.projects_dir || '';
    els.appsScanRoot.value = body.apps_scan_root || '';
    renderClaudeOptions();
  }

  async function fetchApps() {
    const body = await jsonApi('/api/apps');
    state.apps = body.apps || [];
    renderApps();
  }

  async function fetchStatus() {
    try {
      const body = await jsonApi('/api/status');
      const tail = body.tunnel_url ? '📡 ' + body.tunnel_url : 'no tunnel';
      els.statusReadout.textContent =
        (body.tls ? '🔒 TLS · ' : 'http · ') + tail;
    } catch (_) {
      els.statusReadout.textContent = '';
    }
  }

  // ----------------------------------------------------------- boot
  async function boot() {
    const fromUrl = tokenFromUrl();
    if (fromUrl) writeToken(fromUrl);

    try {
      await fetchConfig();
    } catch (exc) {
      if (String(exc.message) !== 'auth required') {
        toast('Boot failed: ' + (exc.message || exc), 'error');
      }
      return;
    }
    await fetchApps();
    await fetchSessions();
    await fetchListeners();
    await fetchStatus();
    setInterval(function () {
      fetchApps().catch(function () {});
    }, TUNNEL_POLL_MS);
    setInterval(function () {
      fetchSessions().catch(function () {});
    }, SESSIONS_POLL_MS);
    setInterval(function () {
      fetchListeners().catch(function () {});
    }, LISTENERS_POLL_MS);
  }

  boot();
})();
