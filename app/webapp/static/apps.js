/* Apps tab: registry list, launch, scan dialog, rename dialog,
 * running-listeners panel.
 *
 * All of this is on the Apps tab — except renderApps also feeds the
 * Coding tab's project list (the `claude-code` rows), which renders as
 * bare folder-name tiles with one launch button per coding agent.
 */

import { els, state } from './state.js';
import { apiFailToast, escapeHtml, jsonApi, toast, logPollFailure } from './api.js';
import { bindOutsideClickToClose, iconUrl } from './dom-utils.js';
import { renderBoard } from './board.js';
import { renderHomeHead } from './home-head.js';
import { fmtAgo } from './sessions.js';
import { applyLaunchSizePayload, handleLaunchResponse } from './terminal.js';
import { icon } from './_vendored/icons/icons.js';
import { setSwitch, switchEl } from './_vendored/switch/switch.js';

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

  els.claudeEmpty.hidden = codingApps.length !== 0;
  els.registeredTraysEmpty.hidden = trayApps.length !== 0;
  els.appsEmpty.hidden = otherApps.length !== 0;
}

// ------------------------------------------------------ Coding tab tiles
// A Coding tile shows only the bare on-disk folder name plus one icon
// button per coding agent (Claude Code, Antigravity, GitHub Copilot).
// An agent's button is disabled with a hover hint when its CLI isn't
// installed. Coding rows are disk-scanned, so they carry no rename/
// remove controls — Settings → Edit mode does not apply here.
function renderCodingList(host, items) {
  host.innerHTML = '';
  // Favorites pinned to the top (issue #250). `items` arrives alphabetical
  // from the scanner, so a stable partition keeps both groups A–Z. The
  // "Favorites" header toggle (state.codingFavFilter) narrows the list to
  // just the starred ones.
  const favs = items.filter(function (a) { return a.is_favorite; });
  const rest = items.filter(function (a) { return !a.is_favorite; });
  const ordered = state.codingFavFilter ? favs : favs.concat(rest);
  syncFavFilterBtn();

  if (state.codingFavFilter && favs.length === 0) {
    const note = document.createElement('li');
    note.className = 'coding-fav-empty muted small';
    note.innerHTML = 'No favorites yet — tap a project’s ' + icon('star') + ' to star it.';
    host.appendChild(note);
    return;
  }

  ordered.forEach(function (a) {
    const li = document.createElement('li');
    li.className = 'app-item coding-item';
    li.dataset.id = a.id;

    const main = document.createElement('div');
    main.className = 'app-main';
    const name = document.createElement('div');
    name.className = 'coding-name';
    name.textContent = a.name;   // raw folder name, exactly as on disk
    annotateGitStatus(name, a.id);
    main.appendChild(name);
    li.appendChild(main);

    const actions = document.createElement('div');
    actions.className = 'row-actions agent-actions';

    state.agents.forEach(function (agent) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'icon-btn agent-btn';
      btn.dataset.agent = agent.id;
      const icon = document.createElement('img');
      icon.className = 'agent-icon';
      icon.src = iconUrl(agent.id);
      icon.alt = agent.label;
      btn.appendChild(icon);
      if (agent.available) {
        btn.title = 'Launch ' + agent.label;
        btn.setAttribute('aria-label', 'Launch ' + agent.label);
        btn.addEventListener('click', function () { launchApp(a, agent.id); });
      } else {
        btn.disabled = true;
        btn.title = agent.label + ' is not installed';
        btn.setAttribute('aria-label', agent.label + ' is not installed');
      }
      actions.appendChild(btn);
    });

    // GitHub repo icon — opens the repo's open-issues list (sorted by last
    // updated, excluding audit-meta ledger/metadata issues — #341) in a new
    // browser tab. Spawns no process and creates no session. Disabled with a
    // hover hint when the project has no GitHub remote (a.repo_url is unset).
    const ghBtn = document.createElement('button');
    ghBtn.type = 'button';
    ghBtn.className = 'icon-btn agent-btn';
    const ghIcon = document.createElement('img');
    ghIcon.className = 'agent-icon';
    ghIcon.src = iconUrl('github');
    ghIcon.alt = 'GitHub';
    ghBtn.appendChild(ghIcon);
    if (a.repo_url) {
      ghBtn.title = 'Open GitHub issues';
      ghBtn.setAttribute('aria-label', 'Open GitHub issues');
      ghBtn.addEventListener('click', function () {
        const issuesUrl = a.repo_url + '/issues?q=is%3Aissue%20state%3Aopen%20sort%3Aupdated-desc%20-label%3Aaudit-meta';
        window.open(issuesUrl, '_blank', 'noopener,noreferrer');
      });
    } else {
      ghBtn.disabled = true;
      ghBtn.title = 'No GitHub remote';
      ghBtn.setAttribute('aria-label', 'No GitHub remote');
    }
    actions.appendChild(ghBtn);

    // Favorite star — rightmost in the action strip, a toggle distinct from
    // the agent-launch buttons. Filled when starred, outline otherwise
    // (see the .star-btn.is-fav CSS fill treatment).
    const starBtn = document.createElement('button');
    starBtn.type = 'button';
    starBtn.className = 'icon-btn agent-btn star-btn' + (a.is_favorite ? ' is-fav' : '');
    starBtn.innerHTML = icon('star');
    starBtn.title = a.is_favorite ? 'Unstar (remove from favorites)' : 'Star (add to favorites)';
    starBtn.setAttribute('aria-label', starBtn.title);
    starBtn.setAttribute('aria-pressed', a.is_favorite ? 'true' : 'false');
    starBtn.addEventListener('click', function () { toggleFavorite(a); });
    actions.appendChild(starBtn);

    li.appendChild(actions);
    host.appendChild(li);
  });
}

// Star / unstar a coding project (issue #250). Persists server-side, then
// re-fetches /api/apps so the star and the favorites-first ordering update
// from the authoritative payload (no optimistic local mutation to drift).
async function toggleFavorite(a) {
  try {
    await jsonApi('/api/claude-code/favorites', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: a.id, favorite: !a.is_favorite }),
    });
    await fetchApps();
  } catch (exc) {
    apiFailToast('Could not update favorite', exc);
  }
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

// Keep the "Favorites" header toggle's pressed state + glyph in sync with
// state.codingFavFilter. Called on every coding re-render so the 4 s apps
// poll can't leave the button out of step with the list it's filtering.
function syncFavFilterBtn() {
  const btn = els.favFilterBtn;
  if (!btn) return;
  const on = state.codingFavFilter;
  btn.classList.toggle('active', on);
  btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  // Label in its own span so narrow phones can drop it to icon-only (#496:
  // the Projects summary now also carries the Detached/Resume toggles).
  btn.innerHTML = icon('star') + '<span class="fav-filter-label"> Favorites</span>';
}

// Colour a Coding tile's folder name from the cached git-status map
// (issue #115): red when the working tree is dirty (needs cleaning),
// yellow when parked on a non-default branch (not a fresh start). Red
// wins the colour when both apply, but the branch tag still shows so the
// "why" behind a yellow stays visible. No-op only until the boot fetch
// lands (#496) — state.gitStatus fills automatically now, no tap needed.
function annotateGitStatus(nameEl, id) {
  const gs = state.gitStatus && state.gitStatus[id];
  if (!gs || !gs.is_git) return;
  const offMain = !!gs.branch && !gs.on_default_branch;
  if (gs.dirty) nameEl.classList.add('git-dirty');
  else if (offMain) nameEl.classList.add('git-off-main');
  if (offMain) {
    const tag = document.createElement('span');
    tag.className = 'git-branch-tag';
    tag.textContent = gs.branch;
    tag.title = 'on ' + gs.branch +
      (gs.default_branch ? ' (default: ' + gs.default_branch + ')' : '');
    nameEl.appendChild(tag);
  }
}

// Always-on git-status refresh (#496, deliberately reversing #115's
// on-demand contract). Runs git per project on the server, fanned out
// across threads; caches the result in state and re-renders every surface
// that reads it (Coding tiles + legend, home-head aggregate, Board
// backlog). Called at boot and on the GIT_STATUS_POLL_MS interval in
// main.js (quiet — poll failures log, never toast), and by the header
// status button below (loud).
export async function refreshGitStatus(options) {
  const quiet = !!(options && options.quiet);
  try {
    const body = await jsonApi('/api/claude-code/git-status');
    const map = {};
    (body.projects || []).forEach(function (p) { map[p.id] = p; });
    state.gitStatus = map;
    if (els.gitStatusLegend) els.gitStatusLegend.hidden = false;
    renderApps();
    renderHomeHead();
    // The Board backlog reads the same cache (#496 item 4); repaint it if
    // it's the visible tab — its own 5 s poll does no git work. Self-gate on
    // an open drawer (pattern: fetchBoard() at board.js:598) so this refresh
    // can't tear down a drawer's DOM out from under an in-progress
    // interaction (#512).
    if (state.tab === 'board' && !state.boardExpanded) renderBoard();
  } catch (exc) {
    if (!quiet) throw exc;
    logPollFailure('git status refresh failed', exc);
  }
}

// The header ⎇ status button: re-fetch fresh data, then open the off-main
// drill-down popover (#139). The data is usually already warm from the
// poll — the re-fetch just guarantees the popover never shows stale state.
export async function fetchGitStatus() {
  const btn = els.gitStatusBtn;
  if (btn) { btn.disabled = true; btn.classList.add('loading'); }
  try {
    await refreshGitStatus();
    openGitSummary();
  } catch (exc) {
    apiFailToast('Git status check failed', exc);
  } finally {
    if (btn) { btn.disabled = false; btn.classList.remove('loading'); }
  }
}

// Compact "what am I working on" popover (issue #139). Reads the same
// cached git-status the tiles use and lists one line per project parked
// off its default branch, colour-matched to the list (red = dirty,
// yellow = off-main). Anchored below the status button; closes on a
// second tap or any tap outside, mirroring the terminal keys popover.
let _disposeGitSummaryOutsideClick = null;

function closeGitSummary() {
  if (els.gitStatusSummary) els.gitStatusSummary.hidden = true;
  if (_disposeGitSummaryOutsideClick) {
    _disposeGitSummaryOutsideClick();
    _disposeGitSummaryOutsideClick = null;
  }
}

function buildGitSummary() {
  const box = els.gitStatusSummary;
  if (!box) return;
  box.innerHTML = '';
  // Off-default-branch coding projects, in the list's own order.
  const offMain = state.apps.filter(function (a) {
    if (a.kind !== 'claude-code') return false;
    const gs = state.gitStatus && state.gitStatus[a.id];
    return gs && gs.is_git && gs.branch && !gs.on_default_branch;
  });
  if (!offMain.length) {
    const note = document.createElement('div');
    note.className = 'git-summary-empty';
    note.innerHTML = 'All projects on their default branch ' + icon('circle-check');
    box.appendChild(note);
    return;
  }
  offMain.forEach(function (a) {
    const gs = state.gitStatus[a.id];
    const row = document.createElement('div');
    row.className = 'git-summary-row';
    row.setAttribute('role', 'listitem');
    const name = document.createElement('span');
    // Same precedence as annotateGitStatus: red wins when also dirty.
    name.className = 'git-summary-name ' + (gs.dirty ? 'git-dirty' : 'git-off-main');
    name.textContent = a.name;
    const tag = document.createElement('span');
    tag.className = 'git-branch-tag';
    tag.textContent = gs.branch;
    row.appendChild(name);
    row.appendChild(tag);
    box.appendChild(row);
  });
}

function openGitSummary() {
  const box = els.gitStatusSummary;
  if (!box) return;
  buildGitSummary();
  box.hidden = false;
  if (!_disposeGitSummaryOutsideClick) {
    _disposeGitSummaryOutsideClick = bindOutsideClickToClose(
      box, els.gitStatusBtn, closeGitSummary
    );
  }
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
        link.innerHTML = icon('satellite-dish') + ' ' + escapeHtml(a.tunnel_url);
        tr.appendChild(link);
      } else {
        const span = document.createElement('span');
        span.innerHTML = icon('satellite-dish') + ' Tunnel not running';
        tr.appendChild(span);
      }
      main.appendChild(tr);
    }

    // Registered Trays autostart switch (issue #456 part 2/2) — persists
    // via the same PATCH /api/apps/{id} the rename dialog uses, just with
    // `autostart` instead of `name`. The switch is a sibling of the launch
    // button, so its compact 44px target remains independent of launching.
    if (a.kind === 'tray') {
      const row = document.createElement('div');
      row.className = 'tray-autostart-row';
      const switchRow = document.createElement('div');
      switchRow.className = 'switch-row';
      const label = document.createElement('span');
      label.textContent = 'Autostart at boot';
      const toggleBtn = switchEl(!!a.autostart, {
        label: 'Autostart ' + a.name + ' at boot',
        onToggle: function (next, btn) {
          btn.disabled = true;
          toggleTrayAutostart(a, next).finally(function () {
            btn.disabled = false;
          });
        },
      });
      switchRow.appendChild(label);
      switchRow.appendChild(toggleBtn);
      row.appendChild(switchRow);
      main.appendChild(row);
    }

    li.appendChild(main);

    // Rename + remove are gated behind Settings → Edit mode, so the
    // lists stay icon-free in normal use (no per-row icon inflation).
    // Only the Apps tab's bat-based rows reach renderList — Coding-tab
    // rows render via renderCodingList instead.
    if (state.editMode) {
      const actions = document.createElement('div');
      actions.className = 'row-actions';

      const renameBtn = document.createElement('button');
      renameBtn.type = 'button';
      renameBtn.className = 'icon-btn';
      renameBtn.innerHTML = icon('pencil');
      renameBtn.title = 'Rename';
      renameBtn.setAttribute('aria-label', 'Rename');
      renameBtn.addEventListener('click', function () { openRename(a); });
      actions.appendChild(renameBtn);

      const removeBtn = document.createElement('button');
      removeBtn.type = 'button';
      removeBtn.className = 'icon-btn danger';
      removeBtn.innerHTML = icon('trash-2');
      removeBtn.title = 'Remove';
      removeBtn.setAttribute('aria-label', 'Remove');
      removeBtn.addEventListener('click', function () { removeApp(a); });
      actions.appendChild(removeBtn);

      li.appendChild(actions);
    }

    host.appendChild(li);
  });
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
async function launchApp(a, agentId) {
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
    if (a.kind === 'claude-code') payload.agent = agentId || 'claude';
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
    toast(
      (resume ? 'Resumed ' : 'Launched ') + a.name + agentTag +
        (mode === 'remote' ? ' (detached)' : ''),
      'good',
      { icon: resume ? 'rotate-ccw' : 'rocket' }
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
  } catch (exc) {
    logPollFailure('agents fetch failed', exc);
  }
}

// -------------------------------------------------- running apps panel
// Apps spawned from the launcher (bats). Mirrors the Claude Code tab's
// Running sessions panel: list, tap-to-open over Tailscale, per-app stop.
export function renderRunningApps() {
  const host = els.runningAppsList;
  host.innerHTML = '';
  els.runningAppsEmpty.hidden = state.runningApps.length !== 0;
  renderHomeHead();

  state.runningApps.forEach(function (r) {
    const li = document.createElement('li');
    li.className = 'app-item session-item';
    li.dataset.pid = r.pid;

    const main = document.createElement('div');
    main.className = 'app-main';

    // Inert info block — the row itself isn't tappable; actions are
    // the two buttons. Reuses .launch-btn styling minus the click.
    const info = document.createElement('div');
    info.className = 'launch-btn session-open inert';

    const head = document.createElement('div');
    head.className = 'session-head';
    const dot = document.createElement('span');
    dot.className = 'health-dot ' + ((r.alive && r.port) ? 'up' : 'down');
    head.appendChild(dot);
    const pill = document.createElement('span');
    pill.className = 'kind-pill';
    pill.textContent = r.kind;
    head.appendChild(pill);
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = r.name;
    head.appendChild(name);
    info.appendChild(head);

    const meta = document.createElement('span');
    meta.className = 'meta';
    const ago = fmtAgo(r.started_at);
    const parts = [];
    if (ago) parts.push('up ' + ago);
    parts.push(r.port ? ':' + r.port : 'binding…');
    parts.push('pid ' + r.pid);
    meta.textContent = parts.join(' · ');
    info.appendChild(meta);
    main.appendChild(info);
    li.appendChild(main);

    const actions = document.createElement('div');
    actions.className = 'row-actions session-actions';

    const openBtn = document.createElement('button');
    openBtn.type = 'button';
    openBtn.className = 'icon-btn action-open';
    openBtn.innerHTML = icon('globe');
    openBtn.setAttribute('aria-label', 'Open app');
    if (r.url) {
      openBtn.title = 'Open ' + r.url;
      openBtn.addEventListener('click', function () {
        window.open(r.url, '_blank', 'noopener,noreferrer');
      });
    } else {
      openBtn.disabled = true;
      openBtn.title = r.port
        ? 'Set tailnet_host in config/config.json to enable Open'
        : 'Waiting for the app to bind a port…';
    }
    actions.appendChild(openBtn);

    const stopBtn = document.createElement('button');
    stopBtn.type = 'button';
    stopBtn.className = 'icon-btn action-stop-close';
    stopBtn.innerHTML = icon('square');
    stopBtn.title = 'Stop ' + r.name;
    stopBtn.setAttribute('aria-label', 'Stop app');
    stopBtn.addEventListener('click', function () { stopAppInstance(r); });
    actions.appendChild(stopBtn);

    li.appendChild(actions);
    host.appendChild(li);
  });
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
      apiFailToast('Rename failed', exc);
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
    apiFailToast('Scan failed', exc);
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
      const row = document.createElement('div');
      row.className = 'scan-row';
      const body = document.createElement('div');
      const name = document.createElement('div');
      name.textContent = c.name;
      const meta = document.createElement('div');
      meta.className = 'meta';
      meta.textContent = c.bat_path || c.project_dir || '';
      body.appendChild(name);
      body.appendChild(meta);
      row.appendChild(body);
      const toggleBtn = switchEl(true, {
        label: 'Include ' + c.name,
        onToggle: function (next, btn) { setSwitch(btn, next); },
      });
      toggleBtn.dataset.value = c.id;
      row.appendChild(toggleBtn);
      section.appendChild(row);
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
    const checked = Array.from(
      els.scanResults.querySelectorAll('.scan-row .toggle[aria-checked="true"]')
    );
    const ids = checked.map(function (btn) { return btn.dataset.value; });
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
      apiFailToast('Save failed', exc);
    }
  });
}

// ----------------------------------------------------------- listeners panel (Apps tab)
// Parent rows with dependent children keep them collapsed behind a tap
// (#480). Module-level so the expand state survives the poll's re-renders.
const expandedListenerPorts = new Set();
let lastListenerItems = [];

export async function fetchListeners() {
  try {
    const body = await jsonApi('/api/ports/probe');
    renderListeners(body.listeners || []);
  } catch (exc) {
    // Best-effort poll — don't spam toasts.
    logPollFailure('listeners fetch failed', exc);
  }
}

function renderListeners(items) {
  lastListenerItems = items;
  const host = els.listenersList;
  host.innerHTML = '';
  els.listenersEmpty.hidden = items.length !== 0;

  // Group helper services (parent_port set, parent present) under their
  // parent app's row so one app reads as one top-level entry — see #224.
  const byPort = {};
  items.forEach(function (l) { byPort[l.port] = l; });
  const childrenOf = {};
  const topLevel = [];
  items.forEach(function (l) {
    if (l.parent_port != null && byPort[l.parent_port]) {
      (childrenOf[l.parent_port] = childrenOf[l.parent_port] || []).push(l);
    } else {
      topLevel.push(l);
    }
  });

  topLevel.forEach(function (l) {
    const kids = childrenOf[l.port] || [];
    host.appendChild(buildListenerRow(l, false, kids.length > 0));
    if (expandedListenerPorts.has(l.port)) {
      kids.forEach(function (c) {
        host.appendChild(buildListenerRow(c, true, false));
      });
    }
  });
}

function buildListenerRow(l, isChild, hasChildren) {
  const row = document.createElement('div');
  row.className = isChild ? 'listener-row child' : 'listener-row';

  const meta = document.createElement('div');
  const strong = document.createElement('strong');
  strong.textContent = isChild
    ? ('↳ ' + (l.service || l.name || ('port ' + l.port)))
    : (l.app || l.name || ('port ' + l.port));
  const sub = document.createElement('span');
  sub.className = 'meta';
  sub.textContent = ' :' + l.port + ' · pid ' + l.pid + ' · ' + (l.name || '?');
  meta.appendChild(strong);
  meta.appendChild(sub);
  row.appendChild(meta);

  if (hasChildren) {
    // Collapsed by default (#480): the whole parent row is the tap target;
    // the chevron rotates open like the panel-level disclosure idiom.
    row.classList.add('expandable');
    row.setAttribute('aria-expanded', expandedListenerPorts.has(l.port) ? 'true' : 'false');
    const chev = document.createElement('span');
    chev.className = 'listener-chevron';
    chev.setAttribute('aria-hidden', 'true');
    chev.textContent = '›';
    row.appendChild(chev);
    row.addEventListener('click', function () {
      if (expandedListenerPorts.has(l.port)) expandedListenerPorts.delete(l.port);
      else expandedListenerPorts.add(l.port);
      renderListeners(lastListenerItems);
    });
  }

  const kill = document.createElement('button');
  kill.type = 'button';
  kill.innerHTML = icon('octagon-x') + ' Kill';
  kill.addEventListener('click', async function (ev) {
    // Kill on a parent row must never toggle the collapse (#480).
    ev.stopPropagation();
    const label = (isChild ? l.service : l.app) || ('port ' + l.port);
    if (!confirm('Kill ' + label + '?\n\npid ' + l.pid + ' on :' + l.port)) return;
    try {
      const r = await jsonApi('/api/ports/' + l.port + '/kill', { method: 'POST' });
      toast('Killed ' + (r.killed || []).length + ' pid(s) on :' + l.port + '.', 'good');
      fetchListeners();
    } catch (exc) {
      apiFailToast('Kill failed', exc);
    }
  });
  row.appendChild(kill);
  return row;
}

export function wireApps() {
  if (els.gitStatusBtn) {
    els.gitStatusBtn.addEventListener('click', function () {
      // Toggle: a second tap closes the summary; otherwise re-fetch fresh
      // git status and open it (fetchGitStatus opens on success).
      if (els.gitStatusSummary && !els.gitStatusSummary.hidden) {
        closeGitSummary();
        return;
      }
      fetchGitStatus().catch(function () {});
    });
  }
  if (els.favFilterBtn) {
    els.favFilterBtn.addEventListener('click', function (ev) {
      // The toggle lives inside the Projects <summary>; stopPropagation keeps
      // the tap from also collapsing the panel (same trick the Settings edit
      // toggle and the sessions header actions use).
      ev.stopPropagation();
      state.codingFavFilter = !state.codingFavFilter;
      localStorage.setItem(
        'launcher.codingFavFilter', state.codingFavFilter ? '1' : '0'
      );
      renderApps();
    });
  }
  // Refresh the running-apps panel the moment the Apps tab is opened —
  // the background poll pauses while the tab is hidden.
  els.tabApps.addEventListener('click', function () {
    fetchRunningApps().catch(function () {});
  });
  wireRenameDialog();
  wireScanDialog();
}
