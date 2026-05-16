/* Entry point: wires every module together, runs boot(), drives polls.
 *
 * Modules export named functions; this file is the only place that
 * sequences them. Each wireX() attaches DOM listeners exactly once;
 * each fetchX() refreshes its slice of state and re-renders.
 */

import { els, state, LISTENERS_POLL_MS, SESSIONS_POLL_MS, TUNNEL_POLL_MS, WEBAUTHN_POLL_MS } from './state.js';
import { jsonApi, terminalFromUrl, tokenFromUrl, toast, wireLoginForm, writeToken } from './api.js';
import { wireTabs } from './tabs.js';
import { fetchConfig, patchConfig, wireClaudeOptions } from './claude-options.js';
import { fetchSessions, wireSessions } from './sessions.js';
import { fetchApps, fetchListeners, wireApps } from './apps.js';
import { openTerminal, wireTerminal } from './terminal.js';
import { fetchWebauthnStatus, wireWebauthn } from './webauthn.js';

// --------------------------------------------------------- settings panel
function wireSettings() {
  els.editMode.checked = state.editMode;
  els.editMode.addEventListener('change', function () {
    state.editMode = els.editMode.checked;
    localStorage.setItem('launcher.editMode', state.editMode ? '1' : '0');
    // Re-render apps lists to show/hide rename + remove buttons.
    fetchApps().catch(function () {});
  });
  els.saveSettings.addEventListener('click', async function () {
    const patch = {
      projects_dir: els.projectsDir.value.trim(),
      apps_scan_root: els.appsScanRoot.value.trim(),
    };
    await patchConfig(patch);
    toast('Settings saved.', 'good');
  });
}

// --------------------------------------------------------- status readout
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

// --------------------------------------------------------- boot
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

// --------------------------------------------------------- wire + go
wireLoginForm(boot);
wireTabs();
wireClaudeOptions();
wireSessions();
wireApps();
wireTerminal();
wireWebauthn();
wireSettings();

boot();
