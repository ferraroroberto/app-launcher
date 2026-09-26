/* Entry point: wires every module together, runs boot(), drives polls.
 *
 * Modules export named functions; this file is the only place that
 * sequences them. Each wireX() attaches DOM listeners exactly once;
 * each fetchX() refreshes its slice of state and re-renders.
 */

import { els, state, BOARD_POLL_MS, GIT_STATUS_POLL_MS, JOBS_POLL_MS, LISTENERS_POLL_MS, RUNNING_APPS_POLL_MS, SESSIONS_POLL_MS, TUNNEL_POLL_MS, WEBAUTHN_POLL_MS } from './state.js';
import { AuthRequiredError, apiFailToast, consumeUrlParam, jsonApi, toast, wireLoginForm, writeToken } from './api.js';
import { setTab, wireTabs } from './tabs.js';
import { bindTextSize } from './_vendored/text-size/text-size.js';
import { fetchConfig, patchConfig, wireClaudeOptions } from './claude-options.js';
import { fetchRateLimits, fetchSessions, wireSessions } from './sessions.js';
import { fetchContextFilter } from './context-filter.js';
import { fetchAgents, fetchApps, fetchRunningApps, wireApps } from './apps.js';
import { refreshGitStatus } from './apps-coding.js';
import { fetchListeners } from './apps-listeners.js';
import { fetchJobs, renderJobs, wireJobs } from './jobs.js';
import { fetchSkills, openConvoByLink, wireLifeOs } from './life-os.js';
import { fetchBoard, openBoardCard, renderBoard, wireBoard } from './board.js';
import { fetchSystemMapStatus, wireSystemMap } from './system-map.js';
import { wireTokens } from './tokens.js';
import { openTerminal, wireTerminal } from './terminal.js';
import { wireChatPane } from './session-transcript.js';
import { wireChanges } from './changes-overlay.js';
import { markMirrorWindowEarly } from './terminal-mirror.js';
import { fetchWebauthnStatus, wireWebauthn, writeTerminalToken } from './webauthn.js';
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
    const saved = await patchConfig(patch);
    if (!saved) return; // patchConfig already fired the failure toast
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
  // terminal. Every page header carries one (#1131, the home-head toggle
  // slot); they all flip the same attribute.
  document.querySelectorAll('.theme-toggle-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      const dark = document.documentElement.dataset.theme !== 'dark';
      document.documentElement.dataset.theme = dark ? 'dark' : 'light';
      localStorage.setItem('app-launcher.theme', dark ? 'dark' : 'light');
    });
  });
}

// --------------------------------------------------------- status readout
// Renders a sprite-icon span + a trailing text node — data (tunnel_url,
// etc.) always rides a text node, never innerHTML, even though it's
// locally-sourced (issue #355 straggler fix).
async function fetchStatus() {
  try {
    const body = await jsonApi('/api/status');
    state.status = body;
    // The TLS badge + tunnel URL used to render here too — dropped
    // (Settings tab cleanup): needless exposure of the tunnel hostname in
    // the UI, and not information the user needs day to day. The
    // reachability warning stays; it's actionable (fix by switching to
    // the Tailscale URL), not just informational.
    els.statusReadout.innerHTML = '';
    if (body.terminal && body.terminal.reachable === false) {
      const ic = document.createElement('span');
      ic.className = 'inline-icon';
      ic.innerHTML = icon('triangle-alert');
      els.statusReadout.appendChild(ic);
      els.statusReadout.appendChild(document.createTextNode(' terminal needs the Tailscale URL'));
    }
  } catch (_) {
    els.statusReadout.textContent = '';
  }
}

// --------------------------------------------------------- build identity
async function fetchVersion() {
  // Visible proof of which build the PWA is running. Catches stale-cache
  // confusion before it costs a debugging session. Uses jsonApi so the
  // bearer token is attached — /api/version is auth-gated like the rest.
  // A transient failure is retried on boot's backoff (#1287): one dropped
  // request used to leave the readout blank until a reload. No toast — it is
  // a readout; after the last attempt it stays blank.
  for (let attempt = 0; ; attempt++) {
    try {
      const body = await jsonApi('/api/version');
      const sha = body.git_sha || 'unknown';
      const ts = (body.built_at || '').replace('T', ' ').slice(0, 16);
      els.buildReadout.textContent = ts ? ('Build: ' + sha + ' · ' + ts) : ('Build: ' + sha);
      return;
    } catch (exc) {
      els.buildReadout.textContent = '';
      if (exc instanceof AuthRequiredError || attempt >= BOOT_RETRY_MS.length) return;
      console.warn('boot: /api/version failed, retrying', exc);
      await new Promise(function (resolve) {
        setTimeout(resolve, BOOT_RETRY_MS[attempt]);
      });
    }
  }
}

// --------------------------------------------------------- boot
// A transient /api/config failure (a dropped connection on the phone, or a
// loopback socket error on a loaded box, #1243) used to end boot() for good:
// no session list, and none of the polls armed at the end of boot(), so the
// app stayed empty until a manual reload (issue #1230). Non-auth failures are
// retried on this backoff before the toast. A 401 is not retried: it has
// already raised the login overlay, whose success handler calls boot() again.
// fetchVersion() above retries on the same backoff (#1287).
const BOOT_RETRY_MS = [500, 1000, 2000, 4000];

async function fetchConfigForBoot() {
  for (let attempt = 0; ; attempt++) {
    try {
      await fetchConfig();
      return true;
    } catch (exc) {
      if (exc instanceof AuthRequiredError) return false;
      if (attempt >= BOOT_RETRY_MS.length) {
        apiFailToast('Boot failed', exc);
        return false;
      }
      console.warn('boot: /api/config failed, retrying', exc);
      await new Promise(function (resolve) {
        setTimeout(resolve, BOOT_RETRY_MS[attempt]);
      });
    }
  }
}

async function boot() {
  const fromUrl = consumeUrlParam('token');
  if (fromUrl) writeToken(fromUrl);
  // A launcher-spawned PC mirror window on the ts.net URL carries a
  // server-minted passkey terminal token (issue #356) — cache it like a
  // ceremony-minted one. TTL mirrors the server's 12 h _TERMINAL_TOKEN_TTL.
  const ttFromUrl = consumeUrlParam('tt');
  if (ttFromUrl) writeTerminalToken(ttFromUrl, 12 * 3600);
  const mirrorSid = consumeUrlParam('terminal');
  const sharedSessionSid = consumeUrlParam('session');
  // Only the launcher-spawned PC mirror window uses ?terminal=<sid>.
  // Human-copyable links use ?session=<sid>, so opening one on a phone does
  // not claim mirror ownership or surrender the phone's PTY-size authority.
  // Recording only the former here is what lets terminal.js tell a real
  // mirror apart from an ordinary browser (issues #241/#877).
  state.isMirrorWindow = !!mirrorSid;
  // Mark the window closable before the first network call: a mirror whose
  // /api/config 401s returns below and never reaches announceMirrorWindow
  // (issue #940). ?session= links stay unmarked.
  if (mirrorSid) markMirrorWindowEarly(mirrorSid);

  if (!(await fetchConfigForBoot())) return;
  // Each remaining boot fetch fills one panel — none is load-bearing for
  // the rest of the app, so a single failure must not abort boot() and take
  // the deep-link branch below down with it: the PC mirror window's title
  // marker + terminal connect depend on reaching it (issue #371).
  const safe = function (fn) { return fn().catch(function (exc) {
    console.warn('boot: non-critical fetch failed', exc);
  }); };

  // A PC mirror (?terminal=) or human-shared link (?session=) drops straight
  // into the same session; only the former set state.isMirrorWindow above.
  // ?board=<sid> (issue #301) lands a Slack ping on that session's Board
  // card, drawer open — mutually exclusive with the session links by
  // construction (each link carries one param).
  const deepLinkSid = mirrorSid || sharedSessionSid;
  const boardSid = deepLinkSid ? null : consumeUrlParam('board');
  const convo = deepLinkSid ? null : consumeUrlParam('convo');
  const cut = convo ? convo.indexOf('/') : -1;

  // Every panel fetch starts at once (#1258): awaited one by one they cost
  // the phone ~13 serial round trips over the tunnel before the git flags
  // (last in line) painted. Only real reads are chained. The session and
  // project lists render agent icons from state.agents, and fetchAgents
  // re-renders neither, so both wait for it.
  const lists = safe(fetchAgents).then(function () {
    return Promise.all([safe(fetchApps), safe(fetchSessions)]);
  }).then(function () {
    // A Board opened before the project list landed drew its issue actions
    // without it (they key on the registered projects). git-status used to
    // redraw it by coming last in the serial chain; now nothing else would.
    if (state.tab === 'board') renderBoard();
  });
  // ?convo=<skill>/<file> (issue #1170): a copied Life OS conversation link
  // reopens that capture in the viewer, over the Life OS tab. It needs only
  // the skills, so it opens as soon as they land, never behind the rest of
  // boot: the git-status fan-out alone took 1–7 s on an idle box, which is
  // what timed the link out (issue #1222).
  const skills = safe(fetchSkills).then(function () {
    if (boardSid || cut <= 0) return;
    setTab('lifeos');
    openConvoByLink(convo.slice(0, cut), convo.slice(cut + 1)).catch(function (exc) {
      console.warn('boot: conversation link failed', exc);
    });
  });
  // The terminal deep link reads both: reachability and the passkey gate.
  const status = safe(fetchStatus);
  const webauthn = safe(fetchWebauthnStatus);
  // Git flags fill without a tap (#496): one fetch at boot, then the slow
  // poll below keeps them current while a git-reading tab is visible.
  const git = safe(function () { return refreshGitStatus({ quiet: true }); });
  const usage = Promise.all([safe(fetchRateLimits), safe(fetchContextFilter)]);
  const listeners = safe(fetchListeners);
  const running = safe(fetchRunningApps);
  const others = [safe(fetchSystemMapStatus), safe(fetchVersion)];
  // The Board card link fetches the Board itself, so it needs none of these.
  if (boardSid) openBoardCard(boardSid).catch(function () {});

  // Each poll arms once its own first fetch has settled, so it never
  // overlaps it, and never waits on a slower, unrelated one: a git scan
  // still running must not hold back the Jobs poll (#1258).
  const noop = function () {};
  lists.then(function () {
    setInterval(function () {
      fetchApps().catch(noop);
    }, TUNNEL_POLL_MS);
    setInterval(function () {
      // Pause the session poll while the session overlay is open (either
      // mode, #982) — it would re-render the list under the overlay for no
      // reason. The open terminal keeps its own title poll (terminal.js).
      if (!state.sessionView) fetchSessions().catch(noop);
    }, SESSIONS_POLL_MS);
  });
  usage.then(function () {
    setInterval(function () {
      fetchRateLimits().catch(noop);
      // Context filter (issue #713) rides the same cadence as the usage
      // badges above — no dedicated timer for one more lightweight GET.
      fetchContextFilter().catch(noop);
    }, SESSIONS_POLL_MS);
  });
  listeners.then(function () {
    setInterval(function () {
      fetchListeners().catch(noop);
    }, LISTENERS_POLL_MS);
  });
  running.then(function () {
    setInterval(function () {
      // fetchRunningApps() self-gates: it no-ops unless the Apps tab is up.
      fetchRunningApps().catch(noop);
    }, RUNNING_APPS_POLL_MS);
  });
  webauthn.then(function () {
    setInterval(function () {
      fetchWebauthnStatus().catch(noop);
    }, WEBAUTHN_POLL_MS);
  });
  git.then(function () {
    setInterval(function () {
      // Always-on git flags (#496): refresh only while a tab that shows them
      // is visible (Coding tiles / Board backlog) and the page is foreground —
      // a backgrounded PWA must not keep spawning git subprocesses.
      if (document.hidden) return;
      if (state.tab !== 'claude' && state.tab !== 'board') return;
      refreshGitStatus({ quiet: true }).catch(noop);
    }, GIT_STATUS_POLL_MS);
  });
  // No boot fetch of their own: both self-gate to their tab.
  setInterval(function () {
    fetchJobs().catch(noop);
  }, JOBS_POLL_MS);
  setInterval(function () {
    fetchBoard().catch(noop);
  }, BOARD_POLL_MS);

  if (deepLinkSid) {
    await Promise.all([lists, status, webauthn]);
    const found = state.sessions.find(function (s) {
      return s.session_id === deepLinkSid;
    });
    openTerminal(found || { session_id: deepLinkSid, name: deepLinkSid });
  }
  // safe() never rejects, so neither does this: boot() resolves once every
  // boot fetch has settled.
  await Promise.all([lists, skills, status, webauthn, git, usage, listeners, running].concat(others));
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
wireChatPane();
wireChanges();
wireWebauthn();
wireSettings();
wireTokens();
wireTheme();
// Settings → Text size (#1134): same localStorage prefix as the theme,
// which the pre-paint boot script reads.
if (document.getElementById('textSizeControl')) {
  bindTextSize(document.getElementById('textSizeControl'), 'app-launcher');
}

boot();
