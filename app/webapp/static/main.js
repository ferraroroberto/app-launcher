/* Entry point: wires every module together, runs boot(), drives polls.
 *
 * Modules export named functions; this file is the only place that
 * sequences them. Each wireX() attaches DOM listeners exactly once;
 * each fetchX() refreshes its slice of state and re-renders.
 */

import { els, state, BOARD_POLL_MS, JOBS_POLL_MS, LISTENERS_POLL_MS, RUNNING_APPS_POLL_MS, SESSIONS_POLL_MS, TUNNEL_POLL_MS, WEBAUTHN_POLL_MS } from './state.js';
import { apiFailToast, consumeUrlParam, jsonApi, readToken, toast, wireLoginForm, writeToken } from './api.js';
import { wireTabs } from './tabs.js';
import { fetchConfig, patchConfig, wireClaudeOptions } from './claude-options.js';
import { fetchRateLimits, fetchSessions, wireSessions } from './sessions.js';
import { fetchAgents, fetchApps, fetchListeners, fetchRunningApps, wireApps } from './apps.js';
import { fetchJobs, renderJobs, wireJobs } from './jobs.js';
import { fetchSkills, wireLifeOs } from './life-os.js';
import { fetchBoard, openBoardCard, wireBoard } from './board.js';
import { fetchSystemMapStatus, wireSystemMap } from './system-map.js';
import { openTerminal, wireTerminal } from './terminal.js';
import { fetchWebauthnStatus, wireWebauthn, writeTerminalToken } from './webauthn.js';
import { icon } from './_vendored/icons/icons.js';

// --------------------------------------------------------- settings panel
function wireSettings() {
  els.editMode.setAttribute('aria-checked', state.editMode ? 'true' : 'false');
  els.editMode.addEventListener('click', function (ev) {
    // ✏️ Edit mode lives inside the Settings <summary> (issue #47
    // follow-up). Without stopPropagation, clicking the toggle would
    // also bubble to <summary> and expand/collapse the whole panel.
    ev.stopPropagation();
    state.editMode = !state.editMode;
    els.editMode.setAttribute('aria-checked', state.editMode ? 'true' : 'false');
    localStorage.setItem('launcher.editMode', state.editMode ? '1' : '0');
    // Re-render apps lists to show/hide rename + remove buttons.
    fetchApps().catch(function () {});
    // Same toggle drives the Jobs tab's ➕ Add + per-row edit/remove.
    renderJobs();
  });
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

// --------------------------------------------------------- theme toggle
function wireTheme() {
  // The pre-paint boot script in index.html already stamped
  // html[data-theme] (localStorage override, prefers-color-scheme
  // fallback); the button just flips it. The sun/moon glyph swap is pure
  // CSS keyed on the attribute, so there is nothing to re-render here —
  // the xterm surface is dark by default, and under the opt-in follow-app
  // pref (issue #359) terminal.js's own data-theme observer restyles any
  // open terminal.
  els.themeToggle.addEventListener('click', function (ev) {
    // Inside the ⚙️ options <summary> row: don't also expand/collapse it.
    ev.stopPropagation();
    ev.preventDefault();
    const dark = document.documentElement.dataset.theme !== 'dark';
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    localStorage.setItem('app-launcher.theme', dark ? 'dark' : 'light');
  });
}

// --------------------------------------------------------- status readout
// Appends a sprite-icon span + a trailing text node — data (tunnel_url,
// etc.) always rides a text node, never innerHTML, even though it's
// locally-sourced (issue #355 straggler fix).
function appendStatusChunk(parts, iconName, text) {
  if (parts.length) parts.push(document.createTextNode(' \u00b7 '));
  if (iconName) {
    const ic = document.createElement('span');
    ic.className = 'inline-icon';
    ic.innerHTML = icon(iconName);
    parts.push(ic);
  }
  parts.push(document.createTextNode((iconName ? ' ' : '') + text));
}

async function fetchStatus() {
  try {
    const body = await jsonApi('/api/status');
    state.status = body;
    const parts = [];
    appendStatusChunk(parts, body.tls ? 'shield-check' : null, body.tls ? 'TLS' : 'http');
    appendStatusChunk(parts, body.tunnel_url ? 'satellite-dish' : null,
      body.tunnel_url || 'no tunnel');
    if (body.terminal && body.terminal.reachable === false) {
      appendStatusChunk(parts, 'triangle-alert', 'terminal needs the Tailscale URL');
    }
    els.statusReadout.innerHTML = '';
    parts.forEach(function (p) { els.statusReadout.appendChild(p); });
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
  const fromUrl = consumeUrlParam('token');
  if (fromUrl) writeToken(fromUrl);
  // A launcher-spawned PC mirror window on the ts.net URL carries a
  // server-minted passkey terminal token (issue #356) — cache it like a
  // ceremony-minted one. TTL mirrors the server's 12 h _TERMINAL_TOKEN_TTL.
  const ttFromUrl = consumeUrlParam('tt');
  if (ttFromUrl) writeTerminalToken(ttFromUrl, 12 * 3600);
  // THROWAWAY spike #246: bake the bearer token into the spike link so a full
  // page-load of /spike/voice-loop passes the gate over the tunnel (the
  // middleware accepts ?token=). Loopback bypasses the gate, so a tokenless
  // href is fine on the PC.
  if (els.spikeVoiceLink) {
    const tok = readToken();
    els.spikeVoiceLink.href =
      '/spike/voice-loop' + (tok ? '?token=' + encodeURIComponent(tok) : '');
  }
  const deepLinkSid = consumeUrlParam('terminal');
  // Only the launcher-spawned PC mirror window opens via the ?terminal=<sid>
  // deep-link; a human's own browser never does. Recording it here (before
  // the param is stripped from the URL) is what lets terminal.js tell a real
  // mirror apart from a desktop browser that merely connects over loopback
  // (issue #241).
  state.isMirrorWindow = !!deepLinkSid;

  try {
    await fetchConfig();
  } catch (exc) {
    apiFailToast('Boot failed', exc);
    return;
  }
  // Each remaining boot fetch fills one panel — none is load-bearing for
  // the rest of the app, so a single failure must not abort boot() and take
  // the deep-link branch below down with it: the PC mirror window's title
  // marker + terminal connect depend on reaching it (issue #371).
  const safe = function (fn) { return fn().catch(function (exc) {
    console.warn('boot: non-critical fetch failed', exc);
  }); };
  await safe(fetchAgents);
  await safe(fetchApps);
  await safe(fetchSkills);
  await safe(fetchSystemMapStatus);
  await safe(fetchSessions);
  await safe(fetchRateLimits);
  await safe(fetchListeners);
  await safe(fetchRunningApps);
  await safe(fetchStatus);
  await safe(fetchVersion);
  await safe(fetchWebauthnStatus);

  // PC mirror window opened with ?terminal=<sid> — drop straight in.
  if (deepLinkSid) {
    const found = state.sessions.find(function (s) {
      return s.session_id === deepLinkSid;
    });
    openTerminal(found || { session_id: deepLinkSid, name: deepLinkSid });
  } else {
    // ?board=<sid> (issue #301): a Slack ping lands on that session's
    // Board card, drawer open. Mutually exclusive with ?terminal= by
    // construction (each link carries one param).
    const boardSid = consumeUrlParam('board');
    if (boardSid) openBoardCard(boardSid).catch(function () {});
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
    fetchRateLimits().catch(function () {});
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
    // fetchBoard() self-gates: only polls while the Board tab is visible.
    fetchBoard().catch(function () {});
  }, BOARD_POLL_MS);
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
wireBoard();
wireSystemMap();
wireTerminal();
wireWebauthn();
wireSettings();
wireTheme();

boot();
