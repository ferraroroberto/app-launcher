/* Entry point: wires every module together, runs boot(), drives polls.
 *
 * Modules export named functions; this file is the only place that
 * sequences them. Each wireX() attaches DOM listeners exactly once;
 * each fetchX() refreshes its slice of state and re-renders.
 */

import { els, state, BOARD_POLL_MS, GIT_STATUS_POLL_MS, JOBS_POLL_MS, LISTENERS_POLL_MS, RUNNING_APPS_POLL_MS, SESSIONS_POLL_MS, TUNNEL_POLL_MS, WEBAUTHN_POLL_MS } from './state.js';
import { apiFailToast, consumeUrlParam, jsonApi, readToken, toast, wireLoginForm, writeToken } from './api.js';
import { wireTabs } from './tabs.js';
import { fetchConfig, patchConfig, wireClaudeOptions } from './claude-options.js';
import { fetchRateLimits, fetchSessions, wireSessions } from './sessions.js';
import { fetchContextFilter } from './context-filter.js';
import { fetchAgents, fetchApps, fetchListeners, fetchRunningApps, refreshGitStatus, wireApps } from './apps.js';
import { fetchJobs, renderJobs, wireJobs } from './jobs.js';
import { fetchSkills, wireLifeOs } from './life-os.js';
import { fetchBoard, openBoardCard, wireBoard } from './board.js';
import { fetchSystemMapStatus, wireSystemMap } from './system-map.js';
import { wireTokens } from './tokens.js';
import { openTerminal, wireTerminal } from './terminal.js';
import { fetchWebauthnStatus, writeTerminalToken } from './webauthn.js';
import { icon } from './_vendored/icons/icons.js';
import { setSwitch } from './_vendored/switch/switch.js';

// --------------------------------------------------------- settings panel
// One edit-mode state, one switch: the Registered-jobs summary toggle on
// the Jobs tab (issue #719 removed the duplicate Settings-head toggle —
// the Jobs tab is where editing actually happens, so it carries the entry
// point for both Jobs-tab and Apps-tab row editing).
function syncEditModeButtons() {
  if (els.jobsEditBtn) setSwitch(els.jobsEditBtn, state.editMode);
}

function toggleEditMode() {
  state.editMode = !state.editMode;
  syncEditModeButtons();
  localStorage.setItem('launcher.editMode', state.editMode ? '1' : '0');
  // Re-render apps lists to show/hide rename + remove buttons.
  fetchApps().catch(function () {});
  // Same toggle drives the Jobs tab's ➕ Add + per-row edit/remove.
  renderJobs();
}

// Boot-autostart (issue #456 part 1/2) is its own dedicated endpoint, not a
// patchConfig() field — enabling/disabling it writes/removes a Startup-folder
// wrapper bat (a real filesystem side effect), so the click re-fetches
// /api/config for the actual on-disk state rather than optimistically
// flipping aria-checked.
async function toggleBootAutostart() {
  const next = els.bootAutostartToggle.getAttribute('aria-checked') !== 'true';
  try {
    await jsonApi('/api/settings/boot-autostart', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: next }),
    });
    await fetchConfig();
    toast(
      next ? 'App-launcher will start at log on.' : 'Boot autostart disabled.',
      'good'
    );
  } catch (exc) {
    apiFailToast('Boot autostart failed', exc);
  }
}

function wireSettings() {
  syncEditModeButtons();
  if (els.jobsEditBtn) els.jobsEditBtn.addEventListener('click', toggleEditMode);
  if (els.bootAutostartToggle) {
    els.bootAutostartToggle.addEventListener('click', toggleBootAutostart);
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
    if (els.terminalHistoryLines && els.terminalHistoryLines.value !== '') {
      const lines = parseInt(els.terminalHistoryLines.value, 10);
      if (Number.isFinite(lines)) patch.terminal_history_lines = lines;
    }
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
  // the terminal screen follows the app theme (issue #383) via
  // terminal.js's own data-theme observer, which restyles any open
  // terminal. The button lives in the home-head card's toggle slot (#496)
  // — no <summary> around it any more, so no stopPropagation needed.
  els.themeToggle.addEventListener('click', function () {
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
    // The TLS badge + tunnel URL used to render here too — dropped
    // (Settings tab cleanup): needless exposure of the tunnel hostname in
    // the UI, and not information the user needs day to day. The
    // reachability warning stays; it's actionable (fix by switching to
    // the Tailscale URL), not just informational.
    const parts = [];
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
  await safe(fetchContextFilter);
  await safe(fetchListeners);
  await safe(fetchRunningApps);
  await safe(fetchStatus);
  await safe(fetchVersion);
  await safe(fetchWebauthnStatus);
  // Git flags fill without a tap (#496): one fetch at boot, then the slow
  // poll below keeps them current while a git-reading tab is visible.
  await safe(function () { return refreshGitStatus({ quiet: true }); });

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
    // Context filter (issue #713) rides the same cadence as the usage
    // badges above — no dedicated timer for one more lightweight GET.
    fetchContextFilter().catch(function () {});
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
  setInterval(function () {
    // Always-on git flags (#496): refresh only while a tab that shows them
    // is visible (Coding tiles / Board backlog) and the page is foreground —
    // a backgrounded PWA must not keep spawning git subprocesses.
    if (document.hidden) return;
    if (state.tab !== 'claude' && state.tab !== 'board') return;
    refreshGitStatus({ quiet: true }).catch(function () {});
  }, GIT_STATUS_POLL_MS);
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
wireSettings();
wireTokens();
wireTheme();

boot();
