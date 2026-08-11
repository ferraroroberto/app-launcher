/* Coding tab: project tiles, per-agent launch buttons, favorites, the
 * agent-visibility toggles, and the git-status annotation + summary popover.
 *
 * Split out of apps.js in issue #723, following the same shape jobs.js →
 * jobs-row/jobs-dialog/jobs-agenda and board.js → board-dispatch already
 * set. The import cycle back to apps.js is deliberate and matches those
 * siblings: apps.js owns the shared list orchestration (renderApps,
 * launchApp, fetchApps) that both tab surfaces drive.
 */

import { els, state } from './state.js';
import { apiFailToast, jsonApi, logPollFailure } from './api.js';
import { bindOutsideClickToClose, iconUrl } from './dom-utils.js';
import { renderBoard } from './board.js';
import { renderHomeHead } from './home-head.js';
import { icon } from './_vendored/icons/icons.js';
import { setSwitch, switchEl } from './_vendored/switch/switch.js';
import { patchConfig } from './claude-options.js';
import { fetchApps, launchApp, renderApps } from './apps.js';

// ------------------------------------------- Coding row button visibility
// The row strip grew to one button per registered agent plus GitHub plus the
// star (issue #666: six agents made it crowded on the phone). The user hides
// the ones they don't use from the options card. Persisted server-side as
// `coding_hidden_agents` — a *hidden* list, so a newly registered agent shows
// up by default and needs no config migration.
//
// `github` is a pseudo-agent id: the repo-issues button is hideable the same
// way, without inventing a second config key for one button.
const GITHUB_BUTTON_ID = 'github';
const GITHUB_BUTTON_LABEL = 'GitHub issues';

function hiddenButtons() {
  const cfg = state.config || {};
  return new Set((cfg.coding_hidden_agents || []).map(String));
}

// The list to *write*, read off the rendered switches rather than
// `state.config`. Tapping two switches in quick succession would otherwise
// compose the second patch from a config the first patch hasn't refreshed
// yet — a read-modify-write race that silently resurrects the button the
// first tap just hid (caught on the WebKit projection, where the slower
// round-trip loses every time; Chromium only won it by luck).
function hiddenFromSwitches() {
  const host = els.agentVisibility;
  if (!host) return [];
  return Array.prototype.filter
    .call(
      host.querySelectorAll('[data-visibility-toggle]'),
      function (sw) { return sw.getAttribute('aria-checked') !== 'true'; }
    )
    .map(function (sw) { return sw.dataset.visibilityToggle; });
}

// Writes are chained so two quick taps can't land out of order — each patch
// sends the whole list, so a late-arriving earlier write would otherwise
// clobber the newer one.
let visibilityWrite = Promise.resolve();

// The toggle list is *generated* from the live agent registry, never
// hand-written per agent: adding an agent to src/agents.py puts it in this
// list with no further code change (the whole point of issue #666).
export function renderAgentVisibility() {
  const host = els.agentVisibility;
  if (!host) return;
  const hidden = hiddenButtons();
  host.innerHTML = '';
  const rows = (state.agents || []).map(function (agent) {
    return { id: agent.id, label: agent.label };
  });
  rows.push({ id: GITHUB_BUTTON_ID, label: GITHUB_BUTTON_LABEL });

  rows.forEach(function (row) {
    const wrap = document.createElement('span');
    wrap.className = 'switch-row';
    const name = document.createElement('span');
    name.textContent = row.label;
    wrap.appendChild(name);
    const sw = switchEl(!hidden.has(row.id), {
      label: 'Show the ' + row.label + ' button on project rows',
      onToggle: function (next) {
        // Optimistic flip, then persist the whole list. The payload is
        // composed from the switches (including this flip), not from
        // state.config, and writes are serialized — see hiddenFromSwitches.
        //
        // renderApps() always repaints the (unrelated) coding tiles so their
        // per-row agent buttons pick up the new hidden set. But
        // renderAgentVisibility() — which tears down and rebuilds *these*
        // switch elements — only runs to self-correct a *failed* save
        // (patchConfig didn't round-trip through GET /api/config, so
        // state.config is still the pre-flip truth). On success the
        // optimistic flip is already the truth: rebuilding here too raced a
        // fast second tap against the teardown and dropped its click on a
        // detached element (issue #732).
        setSwitch(sw, next);
        const wanted = hiddenFromSwitches();
        visibilityWrite = visibilityWrite.then(function () {
          return patchConfig({ coding_hidden_agents: wanted }).then(
            function (ok) {
              renderApps();
              if (!ok) renderAgentVisibility();
            }
          );
        });
      },
    });
    sw.dataset.visibilityToggle = row.id;
    wrap.appendChild(sw);
    host.appendChild(wrap);
  });
}

// ------------------------------------------------------ Coding tab tiles
// A Coding tile shows only the bare on-disk folder name plus one icon
// button per coding agent (the /api/agents registry drives the set).
// An agent's button is disabled with a hover hint when its CLI isn't
// installed. Coding rows are disk-scanned, so they carry no rename/
// remove controls — Settings → Edit mode does not apply here.
export function renderCodingList(host, items) {
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

    // Buttons the user hid in the options card (issue #666). Re-derived on
    // every render (like syncFavFilterBtn) so the ~4 s poll can't resurrect
    // a hidden button.
    const hidden = hiddenButtons();

    state.agents.forEach(function (agent) {
      if (hidden.has(agent.id)) return;
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
    // Hideable under the same pseudo-id as the agents (issue #666).
    if (!hidden.has(GITHUB_BUTTON_ID)) {
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
    }

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

// The two Coding-tab header controls, wired from apps.js::wireApps so this
// module owns every listener that reads its own state.
export function wireCoding() {
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
}
