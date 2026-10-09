/* Apps tab: the Running card, the registry list, launching — plus the
 * shared orchestration both tab surfaces drive (renderApps, launchApp,
 * fetchApps).
 *
 * renderApps also feeds the Coding tab's project list (the `claude-code`
 * rows); those tiles, their git-status annotation and the agent-visibility
 * toggles live in apps-coding.js, the rename/scan dialogs in
 * apps-dialogs.js, and the Other ports sheet in apps-listeners.js — all
 * split out in issue #723, following the shape jobs.js and board.js already
 * set.
 */

import { els, state } from './state.js';
import { apiFailToast, jsonApi, toast, logPollFailure } from './api.js';
import { renderHomeHead } from './home-head.js';
import { fmtAgo } from './sessions.js';
import { applyLaunchSizePayload, handleLaunchResponse } from './terminal.js';
import { switchEl } from './_vendored/switch/switch.js';
import {
  codingLaunch, renderAgentVisibility, renderCodingList, renderFavoriteAgent,
  wireCoding, wireFavoriteAgent,
} from './apps-coding.js';
import { openRename, wireRenameDialog, wireScanDialog } from './apps-dialogs.js';
import { actionRow } from './action-rows.js';
import { listFilter } from './list-filter.js';
import { nameLabel } from './dom-utils.js';
import { copyUrl, renderOtherPortsRow, wireListeners } from './apps-listeners.js';
import { createRowMenu } from './row-menu.js';
import { avatar, chip } from './glance.js';
import { confirmDialog } from './confirm-dialog.js';

// The Apps and Trays rows' kebab menu (#1128), on the shared row-menu.js.
const appMenu = createRowMenu('project-menu');
// The Apps list's name filter (#1132), re-applied after every render.
const appsFilter = listFilter({
  input: els.appsFilterInput,
  list: els.appsList,
  empty: els.appsFilterEmpty,
  storageKey: 'app-launcher.filter.apps',
});

// The kind avatar (#1437): one Lucide glyph per bat kind, the shared
// avatar's alive badge on top.
const KIND_GLYPH = { streamlit: 'gauge', webapp: 'globe', tunnel: 'cloud', tray: 'package' };

function kindAvatar(kind, badge) {
  return avatar(KIND_GLYPH[kind] || 'layout-grid', badge, 'app-avatar');
}

// The registry ids with a launcher-spawned instance up (the running-apps
// poll, gated to this tab like the rows it badges).
function runningIds() {
  return new Set(state.runningApps.map(function (r) { return r.app_id; }));
}

// ----------------------------------------------------------- apps list
export function renderApps() {
  const codingApps = state.apps.filter(function (a) { return a.kind === 'claude-code'; });
  renderCodingList(els.claudeList, codingApps);
  els.claudeEmpty.hidden = codingApps.length !== 0;
  renderBatLists();
}

// The Apps and Trays cards. Re-run when the registry lands and when the set
// of running apps changes, so a launch gains its badge on the next poll.
function renderBatLists() {
  const trayApps = state.apps.filter(function (a) { return a.kind === 'tray'; });
  const otherApps = state.apps.filter(function (a) {
    return a.kind !== 'claude-code' && a.kind !== 'tray';
  });
  const running = runningIds();

  renderList(els.registeredTraysList, trayApps, running);
  renderList(els.appsList, otherApps, running);
  // One endRender for both lists: they share the menu, and a call between
  // them would close a menu open on the second.
  appMenu.endRender();
  appsFilter.apply();

  els.registeredTraysEmpty.hidden = trayApps.length !== 0;
  els.appsEmpty.hidden = otherApps.length !== 0;
  // The empty state offers the scan itself; the row would say it twice.
  els.appsScanRow.hidden = otherApps.length === 0;
  const autostart = trayApps.filter(function (a) { return a.autostart; }).length;
  els.traysSummaryMeta.hidden = trayApps.length === 0;
  els.traysSummaryMeta.textContent = trayApps.length + ' · ' + autostart + ' autostart';
  renderHomeHead();
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

// A registered app's alive badge (#1437): red for a tunnel whose probe says
// down (it should be up), green while it is up or a launch from here is
// running, none otherwise.
function appBadge(a, running) {
  if (a.kind === 'tunnel' && a.health === 'down') return 'down';
  if (running.has(a.id) || (a.kind === 'tunnel' && a.health === 'up')) return 'alive';
  return '';
}

// A tray row's context line (#1437): "running" while a launch from here is
// up, and "starts at log on" when its autostart switch is on. Trays the
// tray process starts at log on are not in the running list, so a tray not
// seen running says nothing about it rather than "not running".
function trayMeta(a, running) {
  const parts = [];
  if (running.has(a.id)) parts.push('running');
  if (a.autostart) parts.push('starts at log on');
  return parts.join(' · ');
}

// Apps and Trays rows on the vendored action-row (#1128). Tapping the row
// launches the bat hidden, with no console window (#790's 🚫👁): the
// phone-first case, the PC unattended (#1269). Launch visible (watch a
// Streamlit boot, read a traceback) and every other action ride the kebab,
// whose header line is the bat path (#1437). An app row leads with its
// kind avatar; the context line is the kind, plus a danger "down" chip for
// a tunnel that is down (Up is normal, so it gets none). A tray row keeps
// its autostart switch as the row's one leading toggle — it is state read
// at a glance, unlabelled because the panel is called Trays; screen readers
// still get "Autostart <name> at boot".
function renderList(host, items, running) {
  host.innerHTML = '';
  items.forEach(function (a) {
    const tray = a.kind === 'tray';
    const down = a.kind === 'tunnel' && a.health === 'down';
    const row = actionRow({
      id: a.id,
      className: 'app-row',
      title: a.name,
      meta: tray ? trayMeta(a, running) : nameLabel(a.kind),
      chips: down ? [chip('down', 'danger', 'app-down-chip')] : [],
      avatar: tray ? null : kindAvatar(a.kind, appBadge(a, running)),
      label: 'Launch ' + a.name + ' hidden, with no window',
      onMain: function () { launchApp(a, undefined, true); },
      kebabClass: 'app-menu-anchor',
      kebabLabel: a.name + ' actions',
    });
    if (tray) {
      row.li.insertBefore(switchEl(!!a.autostart, {
        label: 'Autostart ' + a.name + ' at boot',
        onToggle: function (next, btn) {
          btn.disabled = true;
          toggleTrayAutostart(a, next).finally(function () {
            btn.disabled = false;
          });
        },
      }), row.main);
    }
    const tunnel = a.kind === 'tunnel';
    row.li.appendChild(appMenu.attach(a.id, row.kebab, [
      {
        // The same bat in a console window you can watch (#790's ⚡); the
        // row tap is the hidden launch (#1269).
        className: 'app-visible-btn', glyph: 'eye',
        label: 'Launch ' + a.name + ' in a visible window', text: 'Launch visible',
        onTap: function () { launchApp(a, undefined, false); },
      },
      {
        // A tunnel's URL is tapped, never read: a cloudflared URL with a
        // `?token=…` on it wrapped to three lines on the phone.
        className: 'app-tunnel-link', glyph: 'link',
        label: a.tunnel_url ? 'Open ' + a.name + ' tunnel' : a.name + ' tunnel not running',
        text: 'Open link',
        hidden: !tunnel,
        disabled: !a.tunnel_url,
        title: 'Tunnel not running',
        onTap: function () { window.open(a.tunnel_url, '_blank', 'noopener'); },
      },
      {
        className: 'app-copy-url-btn', glyph: 'copy',
        label: 'Copy ' + a.name + ' tunnel URL', text: 'Copy URL',
        hidden: !tunnel,
        disabled: !a.tunnel_url,
        title: 'Tunnel not running',
        onTap: function () { copyUrl(a.tunnel_url); },
      },
      {
        // Always offered since #1437 (decision 5 of #1432): no longer behind
        // the Jobs tab's Edit mode.
        className: 'app-rename-btn', glyph: 'pencil',
        label: 'Rename ' + a.name, text: 'Rename',
        onTap: function () { openRename(a); },
      },
      {
        className: 'app-remove-btn', glyph: 'trash-2', danger: true,
        label: 'Remove ' + a.name, text: 'Remove',
        onTap: function () { removeApp(a); },
      },
    ], { header: a.bat_path || a.project_dir || '' }));
    host.appendChild(row.li);
  });
}

// Coding-tab launch mode is the Detached switch in the Projects card's
// launch toolbar (launch-toolbar.js, #1434): on → 'remote' (detached
// console window, listed + killable here but no phone terminal); off →
// full-control PTY streamed to the phone. The Resume switch (issue #151)
// reopens the agent's own
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
  const coding = a.kind === 'claude-code' && codingLaunch;
  const resume = !!(coding && codingLaunch.isResume());
  // Detached → 'remote', independent of Resume. The two combine: a
  // Detached+Resume launch renders the agent's picker in the console.
  const mode = (coding && codingLaunch.isDetached()) ? 'remote' : null;
  try {
    const opts = { method: 'POST' };
    const payload = {};
    if (mode) payload.mode = mode;
    if (resume) payload.resume = true;
    if (a.kind === 'claude-code') {
      payload.agent = agentId || 'claude';
      const choice = document.getElementById('codingModelCombo');
      const selectedModel = choice && choice.dataset.value;
      if (selectedModel && selectedModel.startsWith(payload.agent + ':')) {
        payload.model = selectedModel;
      } else if (payload.agent === 'claude' && state.config && state.config.claude) {
        payload.model = 'claude:' + state.config.claude.model;
      } else if (payload.agent === 'codex' && state.config && state.config.codex) {
        payload.model = 'codex:' + state.config.codex.model;
      }
    }
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
  const ok = await confirmDialog({
    title: 'Remove ' + a.name + '?',
    message: 'It leaves the registry. Its files stay where they are.',
    action: 'Remove',
  });
  if (!ok) return;
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
  // The visibility list and the favourite-agent picker are both keyed off
  // the registry, so they render once the agents are known (boot order:
  // fetchConfig → fetchAgents). Rendering from the conservative fallback on
  // a failed fetch is fine — same ids, same labels.
  renderAgentVisibility();
  renderFavoriteAgent();
}

// -------------------------------------------------- the Running card
// Apps spawned from the launcher (bats), as action-rows (#1129): tapping the
// row opens the app over Tailscale; Copy URL and the destructive Stop (last,
// confirmed) are in its ⋮ menu rather than a visible button. Since #1437 a
// row leads with its kind avatar wearing the alive badge, its context line
// is "up 2h · :8501", and the pid is the menu's header line.
const runningMenu = createRowMenu('project-menu');
// The running app ids the Apps list was last badged from: it re-renders
// only when that set changes, not on every poll.
let badgedRunning = '';

export function renderRunningApps() {
  const host = els.runningAppsList;
  host.innerHTML = '';
  const none = state.runningApps.length === 0;
  els.runningAppsEmpty.hidden = !none;
  els.runningAppsNote.hidden = !none;
  renderOtherPortsRow();
  const ids = Array.from(runningIds()).sort().join('\n');
  if (ids !== badgedRunning) {
    badgedRunning = ids;
    renderBatLists();
  } else {
    renderHomeHead();
  }

  state.runningApps.forEach(function (r) {
    const ago = fmtAgo(r.started_at);
    const parts = [];
    if (ago) parts.push('up ' + ago);
    parts.push(r.port ? ':' + r.port : 'binding…');
    const noUrl = r.port
      ? 'Set tailnet_host in config/config.json to enable Open'
      : 'Waiting for the app to bind a port…';
    const row = actionRow({
      className: 'running-app',
      title: r.name,
      meta: parts.join(' · '),
      avatar: kindAvatar(r.kind, 'alive'),
      label: r.url ? 'Open ' + r.url : 'Open ' + r.name,
      disabled: !r.url,
      hint: noUrl,
      onMain: function () { window.open(r.url, '_blank', 'noopener,noreferrer'); },
      kebabClass: 'running-app-menu-anchor',
      kebabLabel: r.name + ' actions',
    });
    row.li.dataset.pid = r.pid;
    row.main.classList.add('action-open');
    row.li.appendChild(runningMenu.attach(r.app_id + ':' + r.pid, row.kebab, [
      {
        className: 'running-app-copy-btn', glyph: 'copy',
        label: 'Copy ' + r.name + ' URL', text: 'Copy URL',
        disabled: !r.url,
        title: noUrl,
        onTap: function () { copyUrl(r.url); },
      },
      {
        className: 'action-stop-close', glyph: 'square', danger: true,
        label: 'Stop ' + r.name, text: 'Stop',
        onTap: function () { stopAppInstance(r); },
      },
    ], { header: 'pid ' + r.pid }));
    host.appendChild(row.li);
  });
  runningMenu.endRender();
}

async function stopAppInstance(r) {
  const ok = await confirmDialog({
    title: 'Stop ' + r.name + '?',
    message: 'This ends pid ' + r.pid + ' and everything it started.',
    action: 'Stop',
  });
  if (!ok) return;
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
  // Nothing running points at Other ports, the row right under the empty
  // state, which is where a restart's orphans show.
  wireListeners();
  wireCoding();
  wireFavoriteAgent();
  wireRenameDialog();
  wireScanDialog();
}
