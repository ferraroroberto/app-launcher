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
import { apiFailToast, jsonApi, logPollFailure, toast } from './api.js';
import { bindOutsideClickToClose, iconUrl } from './dom-utils.js';
import { renderBoard } from './board.js';
import { renderHomeHead } from './home-head.js';
import { createRowMenu } from './row-menu.js';
import { openChanges } from './changes-overlay.js';
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
// `github` and `vscode` are pseudo-agent ids: neither button spawns a coding
// agent, but both are hideable the same way, without inventing a second
// config key per button. Since #977 `vscode` names the whole ⋯ project menu
// (VS Code · Show changes · Open folder) — the key is kept so an existing
// hidden list keeps working, the label says what it hides now.
const GITHUB_BUTTON_ID = 'github';
const GITHUB_BUTTON_LABEL = 'GitHub issues';
const VSCODE_BUTTON_ID = 'vscode';
const VSCODE_BUTTON_LABEL = 'Visual Studio Code';
const PROJECT_MENU_LABEL = 'Project menu (VS Code · changes · folder)';

// The ⋯ menu shared by every Coding row; drops below the rail (see
// `.project-menu` in styles.css) and survives the ~4 s apps re-render.
const projectMenu = createRowMenu('project-menu');

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
  // Same order as the row strip itself, so the toggle list reads as a map
  // of the buttons it controls.
  rows.push({ id: VSCODE_BUTTON_ID, label: PROJECT_MENU_LABEL });
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
// button per coding agent (the /api/agents registry drives the set), then
// the two non-agent buttons — the ⋯ project menu (#977: VS Code #802 ·
// Show changes · Open folder) and GitHub issues — and the favorite star
// last. An agent's button is disabled with a hover hint when its CLI isn't
// installed. Coding rows are disk-scanned, so they carry no rename/remove
// controls — Settings → Edit mode does not apply here.
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
    projectMenu.close();
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
    annotateGitStatus(name, a);
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

    // ⋯ project menu (#977) — the three things a project row wants that are
    // neither an agent launch nor GitHub, in one anchor so the strip stays
    // one row on the phone:
    //   · Open in VS Code (#802) — the project's sibling `.code-workspace`,
    //     created server-side first if missing; no PTY, no session. Greyed
    //     with the same hint an uninstalled agent gets when the `code` CLI
    //     isn't on PATH.
    //   · Show changes — the read-only working-tree viewer (changes-
    //     overlay.js). Hidden once git-status says the folder isn't a repo.
    //   · Open folder — the project directory in Explorer on the PC.
    // The menu is appended after the star so the rail's `.icon-btn +
    // .icon-btn` divider rules still see adjacent buttons; it floats, so
    // DOM order doesn't show. Hideable as a whole under the `vscode`
    // pseudo-id (#666).
    let menuEl = null;
    if (!hidden.has(VSCODE_BUTTON_ID)) {
      const anchor = document.createElement('button');
      anchor.type = 'button';
      anchor.className = 'icon-btn agent-btn project-menu-anchor';
      anchor.dataset.agent = VSCODE_BUTTON_ID;
      anchor.innerHTML = icon('ellipsis-vertical');
      anchor.title = 'Project actions';
      anchor.setAttribute('aria-label', 'Project actions');
      const gs = state.gitStatus && state.gitStatus[a.id];
      menuEl = projectMenu.attach(a.id, anchor, [
        {
          className: 'project-vscode-btn',
          html: '<img class="agent-icon row-menu-brand" src="' + iconUrl('vscode') + '" alt="">',
          label: 'Open in ' + VSCODE_BUTTON_LABEL, text: 'Open in VS Code',
          disabled: !state.vscodeAvailable,
          title: VSCODE_BUTTON_LABEL + ' is not installed',
          onTap: function () { openInVscode(a); },
        },
        {
          className: 'project-changes-btn', glyph: 'git-branch',
          label: 'Show changes', text: 'Show changes',
          hidden: !!(gs && !gs.is_git),
          onTap: function () { openChanges(a); },
        },
        {
          className: 'project-folder-btn', glyph: 'folder',
          label: 'Open folder', text: 'Open folder',
          onTap: function () { openFolder(a); },
        },
      ]);
      actions.appendChild(anchor);
    }

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
    if (menuEl) actions.appendChild(menuEl);

    li.appendChild(actions);
    host.appendChild(li);
  });
  // An open menu whose row is gone drops its state; a reopened one keeps it.
  projectMenu.endRender();
}

// Open the project directory in Explorer on the PC (#977). Fire-and-report,
// like VS Code below: Explorer is its own app, nothing to track afterwards.
async function openFolder(a) {
  try {
    await jsonApi('/api/claude-code/folder/' + encodeURIComponent(a.id), { method: 'POST' });
    toast('Opening ' + a.name + ' in Explorer', 'ok');
  } catch (exc) {
    apiFailToast('Could not open folder', exc);
  }
}

// Open a coding project in the local VS Code (issue #802). Fire-and-report:
// the server creates the `.code-workspace` file if needed and spawns `code`,
// which detaches into its own GUI — so there's no session to poll, no list to
// re-fetch, and the only feedback worth showing is whether the spawn landed.
async function openInVscode(a) {
  try {
    const body = await jsonApi('/api/claude-code/vscode/' + encodeURIComponent(a.id), {
      method: 'POST',
    });
    // The workspace file being *created* is worth naming — it's a new file on
    // disk the user didn't ask for by name; a plain open isn't.
    toast(
      body && body.created
        ? 'Created ' + a.name + '.code-workspace — opening VS Code'
        : 'Opening ' + a.name + ' in VS Code',
      'ok'
    );
  } catch (exc) {
    apiFailToast('Could not open VS Code', exc);
  }
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
  // Icon-only at every width (#1070). The caption used to drop below 520px
  // only, which left a wide labelled pill next to three icon-sized controls
  // on a desktop header. The star alone is unambiguous; the text name lives
  // on aria-label/title, which is where a screen reader reads it anyway.
  btn.innerHTML = icon('star');
}

// Colour a Coding tile's folder name from the cached git-status map
// (issue #115): red when the working tree is dirty (needs cleaning),
// yellow when parked on a non-default branch (not a fresh start). Red
// wins the colour when both apply, but the branch tag still shows so the
// "why" behind a yellow stays visible. No-op only until the boot fetch
// lands (#496) — state.gitStatus fills automatically now, no tap needed.
//
// A coloured name is also the shortcut into Show changes (#977): the colour
// asks "what's different here?", one tap answers it. A clean, on-default
// name stays inert — nothing to show.
function annotateGitStatus(nameEl, a) {
  const gs = state.gitStatus && state.gitStatus[a.id];
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
  if (gs.dirty || offMain) {
    nameEl.setAttribute('role', 'button');
    nameEl.tabIndex = 0;
    nameEl.title = 'Show changes';
    nameEl.addEventListener('click', function () { openChanges(a); });
    nameEl.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        openChanges(a);
      }
    });
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
    // it's the visible tab — its own 5 s poll does no git work. renderBoard()
    // keeps an open drawer's DOM (#958), so this refresh can't tear it down
    // out from under an in-progress interaction (#512).
    if (state.tab === 'board') renderBoard();
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
