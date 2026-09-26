/* Apps tab: the registry list, launching, and the running-apps panel — plus
 * the shared orchestration both tab surfaces drive (renderApps, launchApp,
 * fetchApps).
 *
 * renderApps also feeds the Coding tab's project list (the `claude-code`
 * rows); those tiles, their git-status annotation and the agent-visibility
 * toggles live in apps-coding.js, the rename/scan dialogs in
 * apps-dialogs.js, and the port-listeners panel in apps-listeners.js — all
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
  renderAgentVisibility, renderCodingList, renderFavoriteAgent, wireCoding,
  wireFavoriteAgent,
} from './apps-coding.js';
import { openRename, wireRenameDialog, wireScanDialog } from './apps-dialogs.js';
import { actionRow } from './action-rows.js';
import { listFilter } from './list-filter.js';
import { nameLabel, revealInCard } from './dom-utils.js';
import { fetchListeners } from './apps-listeners.js';
import { createRowMenu } from './row-menu.js';

// The Apps and Trays rows' kebab menu (#1128), on the shared row-menu.js.
const appMenu = createRowMenu('project-menu');
// The Apps list's name filter (#1132), re-applied after every render.
const appsFilter = listFilter({
  input: els.appsFilterInput,
  list: els.appsList,
  empty: els.appsFilterEmpty,
  storageKey: 'app-launcher.filter.apps',
});

// ----------------------------------------------------------- apps list
export function renderApps() {
  const codingApps = state.apps.filter(function (a) { return a.kind === 'claude-code'; });
  const trayApps = state.apps.filter(function (a) { return a.kind === 'tray'; });
  const otherApps = state.apps.filter(function (a) {
    return a.kind !== 'claude-code' && a.kind !== 'tray';
  });

  renderCodingList(els.claudeList, codingApps);
  renderList(els.registeredTraysList, trayApps);
  renderList(els.appsList, otherApps);
  appsFilter.apply();

  els.claudeEmpty.hidden = codingApps.length !== 0;
  els.registeredTraysEmpty.hidden = trayApps.length !== 0;
  els.appsEmpty.hidden = otherApps.length !== 0;
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

// The row's context line (#1128): kind in sentence case (#1156), a
// tunnel's probed health, and in Jobs → Edit mode the bat path the ✏️/🗑️
// menu rows act on — only worth its width when you're about to use them.
function appMeta(a) {
  if (state.editMode) return a.bat_path || a.project_dir || '';
  const parts = [nameLabel(a.kind)];
  if (a.health === 'up') parts.push('Up');
  else if (a.health === 'down') parts.push('Down');
  return parts.join(' · ');
}

// Apps and Trays rows on the vendored action-row (#1128). Tapping the row
// launches the bat in a visible window (#790's ⚡: watch a Streamlit boot,
// read a traceback); every other action rides the one kebab. A tray row
// keeps its autostart switch as the row's one leading toggle — it is state
// read at a glance, unlabelled because the panel is called Trays; screen
// readers still get "Autostart <name> at boot".
function renderList(host, items) {
  host.innerHTML = '';
  items.forEach(function (a) {
    const row = actionRow({
      id: a.id,
      className: 'app-row',
      title: a.name,
      meta: appMeta(a),
      label: 'Launch ' + a.name + ' in a visible window',
      onMain: function () { launchApp(a, undefined, false); },
      kebabClass: 'app-menu-anchor',
      kebabLabel: a.name + ' actions',
    });
    if (a.kind === 'tray') {
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
        // 🚫👁 (#790): the same bat with no console window at all — the
        // phone-first case, PC unattended.
        className: 'app-stealth-btn', glyph: 'eye-off',
        label: 'Launch ' + a.name + ' in stealth', text: 'Launch hidden',
        onTap: function () { launchApp(a, undefined, true); },
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
        // Rename + remove stay gated behind Jobs tab → Edit mode.
        className: 'app-rename-btn', glyph: 'pencil',
        label: 'Rename ' + a.name, text: 'Rename',
        hidden: !state.editMode,
        onTap: function () { openRename(a); },
      },
      {
        className: 'app-remove-btn', glyph: 'trash-2', danger: true,
        label: 'Remove ' + a.name, text: 'Remove',
        hidden: !state.editMode,
        onTap: function () { removeApp(a); },
      },
    ]));
    host.appendChild(row.li);
  });
  appMenu.endRender();
}

async function copyUrl(url) {
  try {
    await navigator.clipboard.writeText(url);
    toast('Link copied', 'good');
  } catch (exc) {
    apiFailToast('Could not copy the link', exc);
  }
}

// Coding-tab launch mode is the ☁️ Detached toggle in the options
// card: checked → 'remote' (detached console window, listed + killable
// here but no phone terminal); unchecked → full-control PTY streamed to
// the phone. The ↺ Resume toggle (issue #151) reopens the agent's own
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
  const resume = !!(a.kind === 'claude-code' && els.claudeResume &&
    els.claudeResume.getAttribute('aria-checked') === 'true');
  // Detached → 'remote', independent of Resume. The two combine: a
  // Detached+Resume launch renders the agent's picker in the console.
  const mode = (a.kind === 'claude-code' && els.claudeDetached &&
    els.claudeDetached.getAttribute('aria-checked') === 'true') ? 'remote' : null;
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
  if (!confirm('Remove ' + a.name + ' from the registry?')) return;
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

// -------------------------------------------------- running apps panel
// Apps spawned from the launcher (bats), as action-rows (#1129): tapping the
// row opens the app over Tailscale; Copy URL and the destructive Stop (last,
// confirmed) are in its ⋮ menu rather than a visible button.
const runningMenu = createRowMenu('project-menu');

export function renderRunningApps() {
  const host = els.runningAppsList;
  host.innerHTML = '';
  els.runningAppsEmpty.hidden = state.runningApps.length !== 0;
  renderHomeHead();

  state.runningApps.forEach(function (r) {
    const ago = fmtAgo(r.started_at);
    const parts = [nameLabel(r.kind)];
    if (ago) parts.push('up ' + ago);
    parts.push(r.port ? ':' + r.port : 'binding…');
    parts.push('pid ' + r.pid);
    const noUrl = r.port
      ? 'Set tailnet_host in config/config.json to enable Open'
      : 'Waiting for the app to bind a port…';
    const row = actionRow({
      className: 'running-app',
      title: r.name,
      meta: parts.join(' · '),
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
    ]));
    host.appendChild(row.li);
  });
  runningMenu.endRender();
}

async function stopAppInstance(r) {
  if (!confirm('Stop ' + r.name + ' (pid ' + r.pid + ')?')) return;
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
  // Empty-state actions (#1238 J-09): nothing running points at the port
  // listeners (where a restart's orphans show), and an empty listener list
  // re-probes.
  const runningAppsEmptyAction = document.getElementById('runningAppsEmptyAction');
  if (runningAppsEmptyAction) {
    runningAppsEmptyAction.addEventListener('click', function () {
      revealInCard(els.listenersList.closest('details'));
      fetchListeners().catch(function () {});
    });
  }
  const listenersEmptyAction = document.getElementById('listenersEmptyAction');
  if (listenersEmptyAction) {
    listenersEmptyAction.addEventListener('click', function () {
      fetchListeners().catch(function () {});
    });
  }
  wireCoding();
  wireFavoriteAgent();
  wireRenameDialog();
  wireScanDialog();
}
