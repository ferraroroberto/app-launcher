/* Launcher hub — single-page app.
 *
 * State:
 *   state.tab          — 'claude' | 'apps'
 *   state.config       — /api/config payload (claude flags + scan paths)
 *   state.apps         — array from /api/apps (each entry carries its own .health)
 *   state.sessions     — array from /api/claude-code/sessions
 *   state.pendingScan  — array from /api/apps/scan, surfaced in scan dialog
 *   state.genPreview   — { workspaces, orphans } for the generate dialog
 *   state.webauthn     — { configured, enrollment_open, devices[] }
 *   state.terminal     — null when overlay closed, else { sid, ws, term, fit, onWindowResize }
 *   state.status       — /api/status payload (incl. terminal reachability)
 *   state.editMode     — boolean, persisted to localStorage (launcher.editMode)
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
    webauthn: { configured: false, enrollment_open: false, devices: [] },
    terminal: null,   // { sid, ws, term, fit, onWindowResize }
    status: null,     // /api/status payload (incl. terminal reachability)
    // Edit mode (Settings toggle) reveals rename + remove on every row,
    // so the lists stay icon-free in normal use. Persisted across reloads.
    editMode: localStorage.getItem('launcher.editMode') === '1',
  };

  const TT_KEY = 'launcher.tt';
  const TT_EXP_KEY = 'launcher.tt.exp';
  const WEBAUTHN_POLL_MS = 15000;

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
    claudeDetached: document.getElementById('claudeDetached'),
    claudeFlagsPreview: document.getElementById('claudeFlagsPreview'),
    claudeList: document.getElementById('claudeList'),
    claudeEmpty: document.getElementById('claudeEmpty'),
    sessionsList: document.getElementById('sessionsList'),
    sessionsEmpty: document.getElementById('sessionsEmpty'),
    refreshSessions: document.getElementById('refreshSessions'),
    appsList: document.getElementById('appsList'),
    appsEmpty: document.getElementById('appsEmpty'),

    rescanBtn: document.getElementById('rescanBtn'),
    settingsPanel: document.getElementById('settingsPanel'),
    editMode: document.getElementById('editMode'),
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

    terminalOverlay: document.getElementById('terminalOverlay'),
    terminalBar: document.querySelector('.terminal-bar'),
    terminalBack: document.getElementById('terminalBack'),
    terminalTitle: document.getElementById('terminalTitle'),
    terminalHost: document.getElementById('terminalHost'),
    terminalStatus: document.getElementById('terminalStatus'),
    terminalImage: document.getElementById('terminalImage'),
    terminalImageInput: document.getElementById('terminalImageInput'),
    terminalPaste: document.getElementById('terminalPaste'),
    terminalJumpEnd: document.getElementById('terminalJumpEnd'),
    terminalCtrlC: document.getElementById('terminalCtrlC'),
    terminalQuit: document.getElementById('terminalQuit'),

    webauthnStatus: document.getElementById('webauthnStatus'),
    webauthnDevices: document.getElementById('webauthnDevices'),
    enrollDeviceBtn: document.getElementById('enrollDeviceBtn'),
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

  // ?terminal=<sid> deep-link — the PC mirror window opens straight into a
  // session's terminal. Read it once, strip it from the visible URL.
  function terminalFromUrl() {
    const params = new URLSearchParams(window.location.search);
    const sid = (params.get('terminal') || '').trim();
    if (!sid) return null;
    params.delete('terminal');
    const q = params.toString();
    window.history.replaceState(
      {}, '',
      window.location.pathname + (q ? '?' + q : '') + window.location.hash
    );
    return sid;
  }

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

  async function stopSession(s, closeWindow) {
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
  els.editMode.checked = state.editMode;
  els.editMode.addEventListener('change', function () {
    state.editMode = els.editMode.checked;
    localStorage.setItem('launcher.editMode', state.editMode ? '1' : '0');
    renderApps();
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
      state.status = body;
      const tail = body.tunnel_url ? '📡 ' + body.tunnel_url : 'no tunnel';
      let line = (body.tls ? '🔒 TLS · ' : 'http · ') + tail;
      if (body.terminal && body.terminal.reachable === false) {
        line += ' · ⚠️ terminal needs the Tailscale URL';
      }
      els.statusReadout.textContent = line;
    } catch (_) {
      els.statusReadout.textContent = '';
    }
  }

  // --------------------------------------------------- webauthn helpers
  function b64urlToBuf(s) {
    s = String(s).replace(/-/g, '+').replace(/_/g, '/');
    const pad = s.length % 4 ? '='.repeat(4 - (s.length % 4)) : '';
    const bin = atob(s + pad);
    const buf = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
    return buf.buffer;
  }
  function bufToB64url(buf) {
    const bytes = new Uint8Array(buf);
    let bin = '';
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }
  function prepCreate(o) {
    o.challenge = b64urlToBuf(o.challenge);
    o.user.id = b64urlToBuf(o.user.id);
    (o.excludeCredentials || []).forEach(function (c) { c.id = b64urlToBuf(c.id); });
    return o;
  }
  function prepGet(o) {
    o.challenge = b64urlToBuf(o.challenge);
    (o.allowCredentials || []).forEach(function (c) { c.id = b64urlToBuf(c.id); });
    return o;
  }
  function serializeReg(c) {
    return {
      id: c.id,
      rawId: bufToB64url(c.rawId),
      type: c.type,
      response: {
        attestationObject: bufToB64url(c.response.attestationObject),
        clientDataJSON: bufToB64url(c.response.clientDataJSON),
      },
      clientExtensionResults: c.getClientExtensionResults ? c.getClientExtensionResults() : {},
      authenticatorAttachment: c.authenticatorAttachment || undefined,
    };
  }
  function serializeAuth(c) {
    return {
      id: c.id,
      rawId: bufToB64url(c.rawId),
      type: c.type,
      response: {
        authenticatorData: bufToB64url(c.response.authenticatorData),
        clientDataJSON: bufToB64url(c.response.clientDataJSON),
        signature: bufToB64url(c.response.signature),
        userHandle: c.response.userHandle ? bufToB64url(c.response.userHandle) : null,
      },
      clientExtensionResults: c.getClientExtensionResults ? c.getClientExtensionResults() : {},
      authenticatorAttachment: c.authenticatorAttachment || undefined,
    };
  }

  // ------------------------------------------------- terminal token store
  function readTerminalToken() {
    const tok = localStorage.getItem(TT_KEY);
    const exp = parseInt(localStorage.getItem(TT_EXP_KEY) || '0', 10);
    if (tok && exp > Date.now()) return tok;
    return '';
  }
  function writeTerminalToken(tok, ttlSeconds) {
    if (!tok) return;
    localStorage.setItem(TT_KEY, tok);
    localStorage.setItem(
      TT_EXP_KEY, String(Date.now() + (ttlSeconds || 3600) * 1000)
    );
  }
  function clearTerminalToken() {
    localStorage.removeItem(TT_KEY);
    localStorage.removeItem(TT_EXP_KEY);
  }

  // ------------------------------------------------------- webauthn flows
  async function fetchWebauthnStatus() {
    try {
      state.webauthn = await jsonApi('/api/webauthn/status');
      renderWebauthn();
    } catch (_) { /* best-effort */ }
  }

  function renderWebauthn() {
    const w = state.webauthn || {};
    if (!els.webauthnStatus) return;
    if (!w.configured) {
      els.webauthnStatus.textContent =
        'Passkey gate not configured — the terminal is Tailscale-only.';
      els.webauthnDevices.innerHTML = '';
      els.enrollDeviceBtn.hidden = true;
      return;
    }
    const n = (w.devices || []).length;
    let msg = n ? n + ' device(s) enrolled.' : 'No device enrolled yet.';
    if (w.enrollment_open) {
      msg += ' Enrollment window open (' + w.enrollment_seconds_left + 's).';
    }
    els.webauthnStatus.textContent = msg;
    els.webauthnDevices.innerHTML = '';
    (w.devices || []).forEach(function (d) {
      const li = document.createElement('li');
      const label = document.createElement('span');
      label.textContent = d.label + ' · ' +
        (d.last_used ? 'last used ' + d.last_used : 'added ' + d.added_at);
      li.appendChild(label);
      const rm = document.createElement('button');
      rm.type = 'button';
      rm.className = 'icon-btn danger';
      rm.textContent = '🗑️';
      rm.title = 'Remove passkey';
      rm.addEventListener('click', function () { removeDevice(d); });
      li.appendChild(rm);
      els.webauthnDevices.appendChild(li);
    });
    els.enrollDeviceBtn.hidden = !w.enrollment_open;
  }

  async function removeDevice(d) {
    if (!confirm('Remove passkey "' + d.label + '"?')) return;
    try {
      await jsonApi('/api/webauthn/devices/' + encodeURIComponent(d.id), {
        method: 'DELETE',
      });
      toast('Removed ' + d.label, 'good');
      fetchWebauthnStatus();
    } catch (exc) {
      toast('Remove failed: ' + (exc.message || exc), 'error');
    }
  }

  async function enrollDevice() {
    if (!window.PublicKeyCredential) {
      toast('This browser has no passkey support.', 'error');
      return;
    }
    const label = prompt('Name this device', 'iPhone');
    if (!label) return;
    try {
      const opts = await jsonApi('/api/webauthn/enroll/begin', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label: label }),
      });
      const cred = await navigator.credentials.create({
        publicKey: prepCreate(opts),
      });
      await jsonApi('/api/webauthn/enroll/finish', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(serializeReg(cred)),
      });
      toast('✅ Device enrolled.', 'good');
      fetchWebauthnStatus();
    } catch (exc) {
      toast('Enrollment failed: ' + (exc.message || exc), 'error');
    }
  }

  async function unlockTerminal() {
    if (!window.PublicKeyCredential) {
      throw new Error('this browser has no passkey support');
    }
    const opts = await jsonApi('/api/webauthn/auth/begin', { method: 'POST' });
    const cred = await navigator.credentials.get({ publicKey: prepGet(opts) });
    const body = await jsonApi('/api/webauthn/auth/finish', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(serializeAuth(cred)),
    });
    writeTerminalToken(body.terminal_token, body.ttl_seconds);
    return body.terminal_token;
  }

  async function ensureTerminalToken() {
    if (!state.webauthn || !state.webauthn.configured) return '';
    // On the PC itself (loopback) the server bypasses the passkey gate —
    // and the iPhone's passkey isn't on this device anyway. Skip it.
    if (state.status && state.status.terminal &&
        state.status.terminal.reason === 'loopback') {
      return '';
    }
    const existing = readTerminalToken();
    if (existing) return existing;
    return await unlockTerminal();
  }

  // ------------------------------------------------------- terminal view
  function termWsUrl(sid, tt) {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const params = new URLSearchParams();
    const bt = readToken();
    if (bt) params.set('token', bt);
    if (tt) params.set('tt', tt);
    const q = params.toString();
    return proto + '//' + location.host + '/api/claude-code/sessions/' +
      encodeURIComponent(sid) + '/ws' + (q ? '?' + q : '');
  }

  function setTerminalStatus(msg) {
    if (!els.terminalStatus) return;
    if (msg) {
      els.terminalStatus.textContent = msg;
      els.terminalStatus.hidden = false;
    } else {
      els.terminalStatus.hidden = true;
    }
  }

  async function openTerminal(session) {
    const sid = session.session_id;
    if (!sid) return;

    // The live terminal is Tailscale-only. If this connection can't reach
    // it (public Cloudflare tunnel, off-tailnet Wi-Fi), explain that up
    // front instead of opening a terminal that only says "Disconnected".
    if (state.status && state.status.terminal &&
        state.status.terminal.reachable === false) {
      closeTerminal();
      els.terminalOverlay.hidden = false;
      document.body.classList.add('terminal-open');
      lockBodyScroll();
      els.terminalTitle.textContent = (session.live_title && session.live_title.trim()) ? session.live_title : (session.name || 'session');
      els.terminalHost.innerHTML = '';
      setTerminalStatus(
        '🔒 ' + (state.status.terminal.reason ||
          'The live terminal is Tailscale-only.')
      );
      return;
    }

    let tt = '';
    try {
      tt = await ensureTerminalToken();
    } catch (exc) {
      toast('Passkey unlock failed: ' + (exc.message || exc), 'error');
      return;
    }
    closeTerminal();
    els.terminalOverlay.hidden = false;
    document.body.classList.add('terminal-open');
    lockBodyScroll();
    els.terminalTitle.textContent = (session.live_title && session.live_title.trim()) ? session.live_title : (session.name || 'session');
    setTerminalStatus('Connecting…');

    // The PC mirror window connects over loopback. It renders whatever
    // size the phone set and never resizes the PTY — the phone is the
    // single size authority, so the two clients never fight (the server
    // also ignores resize frames from role=pc).
    const isMirror = !!(state.status && state.status.terminal &&
      state.status.terminal.reason === 'loopback');

    const term = new window.Terminal({
      cursorBlink: true,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
      fontSize: 13,
      scrollback: 10000,
      theme: { background: '#0a0a0a', foreground: '#e6e6e6' },
    });
    let fit = null;
    if (!isMirror) {
      fit = new window.FitAddon.FitAddon();
      term.loadAddon(fit);
    }
    try {
      term.loadAddon(new window.WebLinksAddon.WebLinksAddon());
    } catch (_) { /* optional */ }
    term.open(els.terminalHost);

    // GPU-accelerated renderer. Falls back to the default DOM renderer
    // on any failure (no WebGL2, driver bug, OS reclaiming the context
    // under memory pressure). Without the fallback the terminal would
    // freeze; with it, worst case is same perf as before.
    let webgl = null;
    try {
      if (window.WebglAddon && window.WebglAddon.WebglAddon) {
        webgl = new window.WebglAddon.WebglAddon();
        webgl.onContextLoss(function () {
          try { webgl.dispose(); } catch (_) {}
          webgl = null;
        });
        term.loadAddon(webgl);
      }
    } catch (exc) {
      try { if (webgl) webgl.dispose(); } catch (_) {}
      webgl = null;
    }

    const ws = new WebSocket(termWsUrl(sid, tt));
    const t = { sid: sid, ws: ws, term: term, fit: fit, webgl: webgl, mirror: isMirror };
    state.terminal = t;

    function applySize() {
      if (isMirror) {
        // Match the phone's PTY dimensions; never touch the PTY itself.
        const s = (state.sessions || []).find(function (x) {
          return x.session_id === sid;
        });
        const cols = (s && s.cols) || session.cols || 120;
        const rows = (s && s.rows) || session.rows || 40;
        try { term.resize(cols, rows); } catch (_) {}
        return;
      }
      try { if (fit) fit.fit(); } catch (_) {}
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
          type: 'resize', rows: term.rows, cols: term.cols,
        }));
      }
    }

    if (isMirror) {
      // The phone may rotate or resize — re-sync to its size periodically.
      t.sizeTimer = setInterval(function () {
        fetchSessions().then(applySize).catch(function () {});
      }, 2500);
    } else {
      setTimeout(applySize, 0);
      t.onWindowResize = applySize;
      window.addEventListener('resize', applySize);
      // iOS doesn't fire 'resize' when its chrome (URL bar / home
      // indicator) shows or hides — those changes ride on the
      // visualViewport API instead. Without re-fitting, xterm keeps
      // its old row count and the freed pixels show as a dead black
      // band at the bottom of the overlay.
      if (window.visualViewport) {
        t.onVisualViewport = applySize;
        window.visualViewport.addEventListener('resize', applySize);
      }
    }

    ws.onopen = function () {
      setTerminalStatus(null);
      applySize();
      term.focus();
    };
    ws.onmessage = function (ev) {
      // tail -f follow: snap back to bottom on new output, but only
      // if the user was already there. If they scrolled up to read
      // history, leave them alone — they'll resume auto-follow by
      // scrolling back to the bottom themselves. The -1 fudge handles
      // iOS fractional touch-scroll states that would otherwise stick
      // the view one row above the tail forever.
      const b = term.buffer.active;
      const wasAtBottom = b.viewportY >= b.baseY - 1;
      term.write(ev.data, function () {
        if (wasAtBottom) {
          try { term.scrollToBottom(); } catch (_) {}
        }
      });
    };
    ws.onerror = function () { setTerminalStatus('Connection error.'); };
    ws.onclose = function (ev) {
      const reason = (ev && ev.reason) ? ev.reason : '';
      const m = ev.code === 4000 ? 'Session ended.'
        : ev.code === 4401 ? '🔒 ' + (reason || 'Passkey unlock required') +
            ' — re-open from Sessions.'
        : ev.code === 4403 ? '🔒 ' + (reason ||
            'Terminal is Tailscale-only') + ' — open the launcher over your ' +
            'Tailscale URL.'
        : ev.code === 4404 ? 'Session not found — it may have ended.'
        : ev.code === 4502 ? 'Session host unreachable on the PC.'
        : (reason || 'Disconnected.');
      setTerminalStatus(m);
      if (ev.code === 4401) clearTerminalToken();
    };

    term.onData(function (d) {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'input', data: d }));
      }
    });
  }

  function closeTerminal() {
    const t = state.terminal;
    state.terminal = null;
    if (!t) return;
    if (t.sizeTimer) clearInterval(t.sizeTimer);
    if (t.onWindowResize) window.removeEventListener('resize', t.onWindowResize);
    if (t.onVisualViewport && window.visualViewport) {
      window.visualViewport.removeEventListener('resize', t.onVisualViewport);
    }
    try { if (t.ws) { t.ws.onclose = null; t.ws.close(); } } catch (_) {}
    try { if (t.webgl) t.webgl.dispose(); } catch (_) {}
    try { if (t.term) t.term.dispose(); } catch (_) {}
  }

  // iOS PWA rubber-band lets the user drag the whole body while the
  // terminal overlay is open, tucking the terminal header under the
  // status bar. Pin the body with position:fixed and stash the scroll
  // position so we can restore it on close. Idempotent — re-opens from
  // the sessions list re-enter through openTerminal but the body must
  // stay pinned with the original scrollY.
  let _savedScrollY = 0;
  function lockBodyScroll() {
    if (document.body.style.position === 'fixed') return;
    _savedScrollY = window.scrollY || window.pageYOffset || 0;
    const s = document.body.style;
    s.position = 'fixed';
    s.top = '-' + _savedScrollY + 'px';
    s.left = '0';
    s.right = '0';
    s.width = '100%';
  }
  function unlockBodyScroll() {
    if (document.body.style.position !== 'fixed') return;
    const s = document.body.style;
    s.position = '';
    s.top = '';
    s.left = '';
    s.right = '';
    s.width = '';
    window.scrollTo(0, _savedScrollY);
  }

  function hideTerminal() {
    closeTerminal();
    els.terminalOverlay.hidden = true;
    document.body.classList.remove('terminal-open');
    unlockBodyScroll();
    els.terminalHost.innerHTML = '';
    setTerminalStatus(null);
    fetchSessions().catch(function () {});
  }

  async function sendImage(file) {
    const t = state.terminal;
    if (!t || !file) return;
    const fd = new FormData();
    fd.append('file', file, file.name || 'image.png');
    try {
      const headers = new Headers();
      const bt = readToken();
      if (bt) headers.set('Authorization', 'Bearer ' + bt);
      const tt = readTerminalToken();
      if (tt) headers.set('X-Terminal-Token', tt);
      const res = await fetch(
        '/api/claude-code/sessions/' + encodeURIComponent(t.sid) + '/image',
        { method: 'POST', headers: headers, body: fd }
      );
      if (!res.ok) {
        const b = await res.json().catch(function () { return null; });
        throw new Error((b && b.detail) || ('HTTP ' + res.status));
      }
      toast('🖼️ Image sent — its path was pasted into the prompt.', 'good');
      if (t.term) t.term.focus();
    } catch (exc) {
      toast('Image failed: ' + (exc.message || exc), 'error');
    }
  }

  els.terminalBack.addEventListener('click', hideTerminal);
  els.terminalCtrlC.addEventListener('click', function () {
    const t = state.terminal;
    if (t && t.ws && t.ws.readyState === WebSocket.OPEN) {
      t.ws.send(JSON.stringify({ type: 'input', data: '\x03' }));
    }
    if (t && t.term) t.term.focus();
  });
  els.terminalQuit.addEventListener('click', async function () {
    const t = state.terminal;
    if (!t) return;
    if (!confirm('Quit this Claude session?')) return;
    try {
      await jsonApi(
        '/api/claude-code/sessions/' + encodeURIComponent(t.sid) + '/stop',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ mode: 'quit' }),
        }
      );
      toast('🛑 Quitting…', 'good');
    } catch (exc) {
      toast('Quit failed: ' + (exc.message || exc), 'error');
    }
  });
  els.terminalImage.addEventListener('click', function () {
    els.terminalImageInput.click();
  });
  els.terminalJumpEnd.addEventListener('click', function () {
    const t = state.terminal;
    if (!t || !t.term) return;
    try { t.term.scrollToBottom(); } catch (_) {}
    t.term.focus();
  });
  els.terminalPaste.addEventListener('click', async function () {
    const t = state.terminal;
    if (!t || !t.ws || t.ws.readyState !== WebSocket.OPEN) return;
    try {
      const text = await navigator.clipboard.readText();
      if (!text) return;
      t.ws.send(JSON.stringify({ type: 'input', data: text }));
      if (t.term) t.term.focus();
    } catch (exc) {
      toast('Clipboard unavailable — paste manually', 'error');
    }
  });
  els.terminalImageInput.addEventListener('change', function () {
    const file = els.terminalImageInput.files && els.terminalImageInput.files[0];
    els.terminalImageInput.value = '';
    if (file) sendImage(file);
  });
  els.terminalHost.addEventListener('paste', function (ev) {
    const items = (ev.clipboardData && ev.clipboardData.items) || [];
    for (let i = 0; i < items.length; i++) {
      if (items[i].type && items[i].type.indexOf('image') === 0) {
        const file = items[i].getAsFile();
        if (file) { ev.preventDefault(); sendImage(file); return; }
      }
    }
  });
  els.terminalHost.addEventListener('dragover', function (ev) {
    ev.preventDefault();
  });
  els.terminalHost.addEventListener('drop', function (ev) {
    const file = ev.dataTransfer && ev.dataTransfer.files &&
      ev.dataTransfer.files[0];
    if (file && file.type && file.type.indexOf('image') === 0) {
      ev.preventDefault();
      sendImage(file);
    }
  });
  els.enrollDeviceBtn.addEventListener('click', enrollDevice);

  // ----------------------------------------------------------- boot
  async function boot() {
    const fromUrl = tokenFromUrl();
    if (fromUrl) writeToken(fromUrl);
    const deepLinkSid = terminalFromUrl();

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
    await fetchWebauthnStatus();

    // PC mirror window opened with ?terminal=<sid> — drop straight in.
    if (deepLinkSid) {
      const found = state.sessions.find(function (s) {
        return s.session_id === deepLinkSid;
      });
      openTerminal(found || { session_id: deepLinkSid, name: deepLinkSid });
    }
    setInterval(function () {
      fetchApps().catch(function () {});
    }, TUNNEL_POLL_MS);
    setInterval(function () {
      // Pause the session poll while the terminal is open — it would
      // re-render the list under the overlay for no reason.
      if (!state.terminal) fetchSessions().catch(function () {});
    }, SESSIONS_POLL_MS);
    setInterval(function () {
      fetchListeners().catch(function () {});
    }, LISTENERS_POLL_MS);
    setInterval(function () {
      fetchWebauthnStatus().catch(function () {});
    }, WEBAUTHN_POLL_MS);
  }

  boot();
})();
