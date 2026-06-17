/* Entry point: wires every module together, runs boot(), drives polls.
 *
 * Modules export named functions; this file is the only place that
 * sequences them. Each wireX() attaches DOM listeners exactly once;
 * each fetchX() refreshes its slice of state and re-renders.
 */

import { els, state, JOBS_POLL_MS, LISTENERS_POLL_MS, RUNNING_APPS_POLL_MS, SESSIONS_POLL_MS, TUNNEL_POLL_MS, WEBAUTHN_POLL_MS } from './state.js';
import { jsonApi, readToken, terminalFromUrl, tokenFromUrl, toast, wireLoginForm, writeToken } from './api.js';
import { wireTabs } from './tabs.js';
import { fetchConfig, patchConfig, wireClaudeOptions } from './claude-options.js';
import { fetchSessions, wireSessions } from './sessions.js';
import { fetchAgents, fetchApps, fetchListeners, fetchRunningApps, wireApps } from './apps.js';
import { fetchJobs, renderJobs, wireJobs } from './jobs.js';
import { fetchSkills, wireLifeOs } from './life-os.js';
import { fetchSystemMapStatus, wireSystemMap } from './system-map.js';
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
    // Same toggle drives the Jobs tab's ➕ Add + per-row edit/remove.
    renderJobs();
  });
  // ✏️ Edit mode now lives inside the Settings <summary> (issue #47
  // follow-up). Without stopPropagation, clicking the toggle would
  // also bubble to <summary> and expand/collapse the whole panel.
  const editLabel = els.editMode.closest('.edit-toggle');
  if (editLabel) {
    editLabel.addEventListener('click', function (ev) {
      ev.stopPropagation();
    });
  }
  els.saveSettings.addEventListener('click', async function () {
    const ignore = els.projectsIgnore.value
      .split('\n')
      .map(function (s) { return s.trim(); })
      .filter(Boolean);
    const patch = {
      projects_dir: els.projectsDir.value.trim(),
      projects_ignore: ignore,
      apps_scan_root: els.appsScanRoot.value.trim(),
      life_os_dir: els.lifeOsDir.value.trim(),
      claude_config_dir: els.claudeConfigDir.value.trim(),
    };
    await patchConfig(patch);
    await fetchApps();
    await fetchSkills();
    await fetchSystemMapStatus();
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

// --------------------------------------------------------- build identity
async function fetchVersion() {
  // Visible proof of which build the PWA is running. Catches stale-cache
  // confusion before it costs a debugging session. Uses jsonApi so the
  // bearer token is attached — /api/version is auth-gated like the rest.
  try {
    const body = await jsonApi('/api/version');
    const sha = body.git_sha || 'unknown';
    const ts = (body.built_at || '').replace('T', ' ').slice(0, 16);
    els.buildReadout.textContent = ts ? ('Build: ' + sha + ' · ' + ts) : ('Build: ' + sha);
  } catch (_) {
    els.buildReadout.textContent = '';
  }
}

// --------------------------------------------------------- boot
async function boot() {
  const fromUrl = tokenFromUrl();
  if (fromUrl) writeToken(fromUrl);
  // THROWAWAY spike #246: bake the bearer token into the spike link so a full
  // page-load of /spike/voice-loop passes the gate over the tunnel (the
  // middleware accepts ?token=). Loopback bypasses the gate, so a tokenless
  // href is fine on the PC.
  if (els.spikeVoiceLink) {
    const tok = readToken();
    els.spikeVoiceLink.href =
      '/spike/voice-loop' + (tok ? '?token=' + encodeURIComponent(tok) : '');
  }
  const deepLinkSid = terminalFromUrl();
  // Only the launcher-spawned PC mirror window opens via the ?terminal=<sid>
  // deep-link; a human's own browser never does. Recording it here (before
  // the param is stripped from the URL) is what lets terminal.js tell a real
  // mirror apart from a desktop browser that merely connects over loopback
  // (issue #241).
  state.isMirrorWindow = !!deepLinkSid;

  try {
    await fetchConfig();
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Boot failed: ' + (exc.message || exc), 'error');
    }
    return;
  }
  await fetchAgents();
  await fetchApps();
  await fetchSkills();
  await fetchSystemMapStatus();
  await fetchSessions();
  await fetchListeners();
  await fetchRunningApps();
  await fetchStatus();
  await fetchVersion();
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
    // fetchRunningApps() self-gates: it no-ops unless the Apps tab is up.
    fetchRunningApps().catch(function () {});
  }, RUNNING_APPS_POLL_MS);
  setInterval(function () {
    // fetchJobs() self-gates: only polls while the Jobs tab is visible.
    fetchJobs().catch(function () {});
  }, JOBS_POLL_MS);
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
wireJobs();
wireLifeOs();
wireSystemMap();
wireTerminal();
wireWebauthn();
wireSettings();

boot();
