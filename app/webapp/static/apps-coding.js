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
import { bindOutsideClickToClose, brandIcon } from './dom-utils.js';
import { renderBoard } from './board.js';
import { renderHomeHead } from './home-head.js';
import { createRowMenu } from './row-menu.js';
import { actionRow } from './action-rows.js';
import { listFilter } from './list-filter.js';
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

// The favourite agent — the one whose launch button stays on the row
// (#1070). Roberto picks it explicitly in the options card; it is never
// derived from usage, because a button that moves on its own is worse than
// one in the wrong place. `coding_favorite_agent` is validated server-side
// against the registry, so an id that reaches here always names a real
// agent; the `|| 'claude'` is only for the window before the first
// /api/config lands.
function favoriteAgentId() {
  const cfg = state.config || {};
  return String(cfg.coding_favorite_agent || 'claude');
}

// The ⋯ menu shared by every Coding row; drops below the rail (see
// `.project-menu` in styles.css) and survives the ~4 s apps re-render.
const projectMenu = createRowMenu('project-menu');
// The Projects list's name filter (#1132), re-applied after every render.
const projectsFilter = listFilter({
  input: els.claudeFilterInput,
  list: els.claudeList,
  empty: els.claudeFilterEmpty,
  storageKey: 'app-launcher.filter.projects',
});

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
  // Same order as the ⋯ menu itself, so the toggle list reads as a map of
  // the rows it controls. The `vscode` pseudo-id is deliberately absent
  // (#1070): it used to hide the whole ⋯ menu, but that menu is now the
  // only route to every non-favourite launch, so hiding it would strand
  // them. An existing stored `vscode` value is left alone and simply has
  // no effect — see the read in renderCodingList.
  rows.push({ id: GITHUB_BUTTON_ID, label: GITHUB_BUTTON_LABEL });

  rows.forEach(function (row) {
    const wrap = document.createElement('span');
    wrap.className = 'switch-row';
    const name = document.createElement('span');
    name.textContent = row.label;
    wrap.appendChild(name);
    const sw = switchEl(!hidden.has(row.id), {
      label: 'Offer ' + row.label + ' in a project row’s menu',
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

// The favourite-agent picker, generated from the same live registry the
// visibility switches are (#666's contract, #1070's control): adding an
// agent to src/agents.py puts it in this list with no further code change.
//
// The write joins `visibilityWrite` rather than starting its own chain.
// Both settings live in one config object, and each patch is composed from
// the DOM rather than from `state.config` — so serializing them together is
// what stops a favourite change landing between a hidden-list patch and its
// GET round-trip and silently reinstating the button that patch just hid.
export function renderFavoriteAgent() {
  const sel = els.codingFavoriteAgent;
  if (!sel) return;
  const current = favoriteAgentId();
  const agents = state.agents || [];
  // Rebuild only when the registry itself changed — the select is a live
  // control, and tearing it down under an open native picker on the phone
  // would drop the tap, the same failure mode #732 fixed for the switches.
  const wanted = agents.map(function (a) { return a.id; }).join(',');
  if (sel.dataset.agentSet !== wanted) {
    sel.innerHTML = '';
    agents.forEach(function (agent) {
      const opt = document.createElement('option');
      opt.value = agent.id;
      opt.textContent = agent.label;
      sel.appendChild(opt);
    });
    sel.dataset.agentSet = wanted;
  }
  // An id with no matching option (registry still loading, or an agent
  // dropped) would leave the select showing the first entry while the rows
  // render something else — show nothing rather than lie about it.
  sel.value = current;
}

export function wireFavoriteAgent() {
  const sel = els.codingFavoriteAgent;
  if (!sel) return;
  sel.addEventListener('change', function () {
    const wanted = sel.value;
    visibilityWrite = visibilityWrite.then(function () {
      return patchConfig({ coding_favorite_agent: wanted }).then(
        function (ok) {
          // renderApps() repaints every row so the button moves and the ⋯
          // menu gains the old favourite, with no reload. On a failed save
          // the select is re-synced from server truth, the same
          // self-correct-only rule the visibility switches follow (#732).
          renderApps();
          if (!ok) renderFavoriteAgent();
        }
      );
    });
  });
}

// ------------------------------------------------------ Coding tab tiles
// A Coding tile shows the bare on-disk folder name plus exactly three
// controls (#1070): the favourite agent's launch button, the ⋯ project
// menu, and the favorite star. The row used to carry one button per
// registered agent plus GitHub plus ⋯ plus the star — nine controls, which
// wrapped to a second line on the phone and paid permanent rail width for
// five buttons that get tapped about once a month each. Everything that
// left the row is a row in the ⋯ menu, so nothing became unreachable; it
// just moved one tap deeper, which is where a monthly action belongs.
// An agent's button is disabled with a hover hint when its CLI isn't
// installed. Coding rows are disk-scanned, so they carry no rename/remove
// controls — Settings → Edit mode does not apply here.
// One agent's launch button for a project row. Extracted when the row
// dropped to a single agent button (#1070) so the row and the ⋯ menu's
// launch rows derive their label, their disabled state and their hint from
// exactly one place.
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
    projectsFilter.apply();
    return;
  }

  // Agents the user hid in the options card (issue #666) — since #1070
  // that governs the ⋯ menu's launch rows. Re-derived on every render (like
  // syncFavFilterBtn) so the ~4 s poll can't resurrect a hidden entry.
  const hidden = hiddenButtons();
  const favoriteId = favoriteAgentId();
  const agents = state.agents || [];
  const favorite = agents.find(function (x) { return x.id === favoriteId; });

  ordered.forEach(function (a) {
    const gs = state.gitStatus && state.gitStatus[a.id];
    const git = gitFlags(gs);
    // The row itself launches the favourite agent (#1128, the action-row
    // contract: tapping the row is its primary action). It is the row's one
    // launch affordance, so the hidden list never removes it.
    const row = actionRow({
      id: a.id,
      className: 'coding-item',
      title: a.name,
      meta: git.meta,
      label: favorite ? 'Launch ' + favorite.label + ' in ' + a.name : a.name,
      disabled: !favorite || !favorite.available,
      hint: favorite ? favorite.label + ' is not installed' : 'No launch agent',
      onMain: function () { launchApp(a, favorite.id); },
      favorite: { on: a.is_favorite, onToggle: function () { toggleFavorite(a); } },
      kebabClass: 'project-menu-anchor',
      kebabLabel: 'Project actions',
    });
    if (favorite) row.main.dataset.agent = favorite.id;
    if (git.flag) row.title.classList.add(git.flag);
    // Stored `vscode` hide values from #666 key on this; inert since #1070.
    row.kebab.dataset.agent = VSCODE_BUTTON_ID;

    // ⋯ project menu (#977, widened by #1070) — everything a project row
    // offers besides the favourite launch and the star:
    //   · Launch <agent> — one row per visible non-favourite agent (#1070).
    //   · GitHub issues — the repo's open-issues list (sorted by last
    //     updated, excluding audit-meta ledger issues — #341) in a new tab.
    //     Greyed with a hint when the project has no GitHub remote.
    //   · Open in VS Code (#802) — the project's sibling `.code-workspace`,
    //     created server-side first if missing. Greyed when the `code` CLI
    //     isn't on PATH.
    //   · Show changes — the read-only working-tree viewer (changes-
    //     overlay.js). Hidden once git-status says the folder isn't a repo.
    //     Since #1128 this is the only route in from the row: the coloured
    //     name can't be its own tap target inside the row's launch button.
    //   · Open folder — the project directory in Explorer on the PC.
    // Declared in the item list rather than appended conditionally:
    // row-menu.js detaches a hidden row instead of marking it [hidden], so a
    // count of the menu's buttons only ever sees what is actually offered.
    const menuItems = agents
      .filter(function (agent) { return agent.id !== favoriteId; })
      .map(function (agent) {
        return {
          className: 'project-launch-btn',
          dataset: { agent: agent.id },
          html: brandIcon(agent.id, 'row-menu-brand'),
          label: agent.available
            ? 'Launch ' + agent.label
            : agent.label + ' is not installed',
          text: agent.label,
          hidden: hidden.has(agent.id),
          disabled: !agent.available,
          title: agent.label + ' is not installed',
          onTap: function () { launchApp(a, agent.id); },
        };
      });
    menuItems.push({
      className: 'project-github-btn',
      html: brandIcon('github', 'row-menu-brand'),
      label: a.repo_url ? 'Open GitHub issues' : 'No GitHub remote',
      text: GITHUB_BUTTON_LABEL,
      hidden: hidden.has(GITHUB_BUTTON_ID),
      disabled: !a.repo_url,
      title: 'No GitHub remote',
      onTap: function () {
        const issuesUrl = a.repo_url +
          '/issues?q=is%3Aissue%20state%3Aopen%20sort%3Aupdated-desc%20-label%3Aaudit-meta';
        window.open(issuesUrl, '_blank', 'noopener,noreferrer');
      },
    });
    row.li.appendChild(projectMenu.attach(a.id, row.kebab, menuItems.concat([
      {
        className: 'project-vscode-btn',
        html: brandIcon('vscode', 'row-menu-brand'),
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
    ])));
    host.appendChild(row.li);
  });
  // An open menu whose row is gone drops its state; a reopened one keeps it.
  projectMenu.endRender();
  projectsFilter.apply();
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
  // Icon only (#1194): #1176 gave the Detached and Resume toggles beside it
  // a word, and briefly this one too; the star stays bare. The name
  // lives on aria-label/title ("Show only favorites"), where a screen
  // reader reads it anyway.
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
// What git-status says about a project row (#115/#496), as the title's
// colour class and the row's context line (#1128): yellow = parked on a
// non-default branch, red = uncommitted changes. Nothing for a folder that
// isn't a repo or hasn't been scanned yet.
function gitFlags(gs) {
  if (!gs || !gs.is_git) return { flag: '', meta: '' };
  const offMain = !!gs.branch && !gs.on_default_branch;
  const parts = [];
  if (offMain) parts.push('On ' + gs.branch);
  if (gs.dirty) parts.push('Uncommitted changes');
  return {
    flag: gs.dirty ? 'git-dirty' : (offMain ? 'git-off-main' : ''),
    meta: parts.join(' · '),
  };
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
    // Same precedence as gitFlags: red wins when also dirty.
    name.className = 'git-summary-name ' + (gs.dirty ? 'git-dirty' : 'git-off-main');
    name.textContent = a.name;
    name.title = a.name;
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
    els.favFilterBtn.addEventListener('click', function () {
      // In the Projects card's toolbar since #1132, not its <summary>, so a
      // tap can no longer fold the card and needs no stopPropagation.
      state.codingFavFilter = !state.codingFavFilter;
      localStorage.setItem(
        'launcher.codingFavFilter', state.codingFavFilter ? '1' : '0'
      );
      renderApps();
    });
  }
}
