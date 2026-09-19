/* Life OS tab (issue #102): skill tiles + one-tap launch + a read-only
 * private-content browser.
 *
 * ~80% a clone of the Coding tab. A tile launches a Claude session in the
 * life-os repo that auto-invokes the bare /<skill> slash-command; the
 * ☁️ Detached + model combo live in the Life OS Skills summary (same UX as
 * the Coding-options Detached toggle). The 📖 Browse button opens an
 * overlay that reads each skill's files — public SKILL.md/description.md
 * plus the private context/memory/examples/conversations + shared
 * identity. Those content endpoints are Tailscale + passkey gated
 * server-side, so a fetch may 403; we surface the reason rather than a
 * blank pane.
 */

import { els, state } from './state.js';
import { apiFailToast, jsonApi, toast, logPollFailure } from './api.js';
import { applyLaunchSizePayload, handleLaunchResponse } from './terminal.js';
import { icon } from './_vendored/icons/icons.js';
import { toggleAriaChecked, wireModelCombo } from './dom-utils.js';
import { renderMarkdown } from './markdown.js';

// The Skills-summary launch-model dropdown controller ({setValue, getValue}),
// created in the tab's wiring once the DOM exists (#540). Read at launch time;
// no server round-trip — it's per-launch, like the Board dispatch combo.
let lifeOsModelCombo = null;
let lifeOsConvosModelCombo = null;
export function setLifeOsModelOptions(items) {
  if (lifeOsModelCombo) lifeOsModelCombo.setOptions(items);
  if (lifeOsConvosModelCombo) lifeOsConvosModelCombo.setOptions(items);
}

function lifeOsModel() {
  if (!els.lifeOsConvos || els.lifeOsConvos.hidden) {
    return (lifeOsModelCombo && lifeOsModelCombo.getValue()) || 'claude:sonnet';
  }
  return (lifeOsConvosModelCombo && lifeOsConvosModelCombo.getValue()) ||
    'claude:sonnet';
}

// ----------------------------------------------------------- skills list
export async function fetchSkills() {
  try {
    const body = await jsonApi('/api/life-os/skills');
    state.lifeOsSkills = body.skills || [];
    renderSkills();
  } catch (exc) {
    logPollFailure('life-os skills fetch failed', exc);
  }
}

export function renderSkills() {
  const host = els.lifeOsList;
  if (!host) return;
  host.innerHTML = '';
  const skills = state.lifeOsSkills;
  els.lifeOsEmpty.hidden = skills.length !== 0;

  // Starred skills pinned to the top (#1070), the Coding tab's treatment
  // (#250) replicated here. `skills` arrives alphabetical from the scanner,
  // so a stable partition keeps both groups A–Z rather than reshuffling the
  // rest of the list around a star.
  const favs = skills.filter(function (s) { return s.is_favorite; });
  const rest = skills.filter(function (s) { return !s.is_favorite; });

  favs.concat(rest).forEach(function (s) {
    const li = document.createElement('li');
    li.className = 'app-item coding-item lifeos-item';
    li.dataset.id = s.id;

    const main = document.createElement('div');
    main.className = 'app-main';
    const name = document.createElement('div');
    name.className = 'coding-name';
    name.textContent = s.name;   // name only — one line per tile
    main.appendChild(name);
    li.appendChild(main);

    const actions = document.createElement('div');
    actions.className = 'row-actions agent-actions';

    // 📖 Browse — open the read-only content browser for this skill.
    const browseBtn = document.createElement('button');
    browseBtn.type = 'button';
    browseBtn.className = 'icon-btn agent-btn';
    browseBtn.innerHTML = icon('book-open');
    browseBtn.title = 'Browse what this skill knows';
    browseBtn.setAttribute('aria-label', 'Browse ' + s.name);
    browseBtn.addEventListener('click', function () { openBrowser(s); });
    actions.appendChild(browseBtn);

    // 🕘 Conversations — the digested index for this skill, with a per-row ↺
    // that reattaches to that exact session (#727).
    const convoBtn = document.createElement('button');
    convoBtn.type = 'button';
    convoBtn.className = 'icon-btn agent-btn lifeos-convo-btn';
    convoBtn.innerHTML = icon('messages-square');
    convoBtn.title = 'Past conversations with ' + s.name;
    convoBtn.setAttribute('aria-label', 'Conversations with ' + s.name);
    convoBtn.addEventListener('click', function () { openConvos(s); });
    actions.appendChild(convoBtn);

    // Launch — fires a fresh Claude session that auto-invokes /<skill>.
    const launchBtn = document.createElement('button');
    launchBtn.type = 'button';
    launchBtn.className = 'icon-btn agent-btn lifeos-launch';
    launchBtn.innerHTML = icon('rocket');
    launchBtn.title = 'Launch ' + s.name;
    launchBtn.setAttribute('aria-label', 'Launch ' + s.name);
    launchBtn.addEventListener('click', function () { launchSkill(s); });
    actions.appendChild(launchBtn);

    // Favorite star — rightmost, same glyph, same classes and the same
    // filled-gold `.is-fav` treatment the Coding row's star uses (#250), so
    // both tabs read identically.
    const starBtn = document.createElement('button');
    starBtn.type = 'button';
    starBtn.className = 'icon-btn agent-btn star-btn' + (s.is_favorite ? ' is-fav' : '');
    starBtn.innerHTML = icon('star');
    starBtn.title = s.is_favorite
      ? 'Unstar (remove from favorites)'
      : 'Star (add to favorites)';
    starBtn.setAttribute('aria-label', starBtn.title);
    starBtn.setAttribute('aria-pressed', s.is_favorite ? 'true' : 'false');
    starBtn.addEventListener('click', function () { toggleSkillFavorite(s); });
    actions.appendChild(starBtn);

    li.appendChild(actions);
    host.appendChild(li);
  });
}

// Star / unstar a Life OS skill (#1070). Persists server-side, then
// re-fetches the skills list so the star and the favorites-first ordering
// update from the authoritative payload — no optimistic local mutation to
// drift, exactly like the Coding tab's toggleFavorite (#250).
async function toggleSkillFavorite(s) {
  try {
    await jsonApi('/api/life-os/favorites', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: s.id, favorite: !s.is_favorite }),
    });
    await fetchSkills();
  } catch (exc) {
    apiFailToast('Could not update favorite', exc);
  }
}

// ------------------------------------------------- weekly recap (issue #167)
// A pinned tile above the skills list: a staleness badge driven by the recap
// ledger's mtime, and a 🚀 that launches /weekly-recap (the interactive
// review). The drafting half runs headless on a schedule, so this is
// review-only. Fetched on every Life OS tab open (cheap stat + glob server-side).
export async function fetchRecapStatus() {
  try {
    state.lifeOsRecap = await jsonApi('/api/life-os/recap-status');
    renderRecap();
  } catch (exc) {
    logPollFailure('life-os recap-status fetch failed', exc);
  }
}

function renderRecap() {
  const host = els.lifeOsRecap;
  if (!host) return;
  const r = state.lifeOsRecap;
  // Hide the tile when life-os isn't checked out — same as the skills list.
  if (!r || !r.available) { host.hidden = true; return; }
  host.hidden = false;

  const badge = els.lifeOsRecapBadge;
  const status = r.staleness || 'never';
  badge.className = 'lifeos-recap-badge ' + status;
  let label;
  if (status === 'never') {
    label = 'never run';
  } else {
    const d = Math.round(r.age_days || 0);
    const ago = d <= 0 ? 'today' : (d + 'd ago');
    const tag = status === 'due' ? ' · due'
      : status === 'overdue' ? ' · overdue' : '';
    label = ago + tag;
  }
  if (r.proposal_pending) label += ' · draft ready';
  badge.textContent = label;
}

// Toast suffix for the launch model: silent on the Sonnet default (the
// common case), " (Opus)" / " (Fable)" otherwise — mirroring the old opus
// tag's terseness (#540).
function modelTag(model) {
  if (!model || model === 'sonnet' || model === 'claude:sonnet') return '';
  const value = model.split(':').pop();
  const label = value.replace(/^gpt-(?:5\.6|6)-/, '');
  return ' (' + label.charAt(0).toUpperCase() + label.slice(1) + ')';
}

async function launchRecap() {
  // Reuse the Skills summary controls: ☁️ Detached → remote, the model combo
  // → the launch model (#540, replacing the old opus on/off toggle).
  const mode = (els.lifeOsDetached && els.lifeOsDetached.getAttribute('aria-checked') === 'true')
    ? 'remote' : 'pty';
  const model = lifeOsModel();
  const payload = { mode: mode, model: model };
  // A desktop browser launch gets a dedicated PC Edge --app window (issue
  // #241); the phone carries its real terminal size so the PTY spawns at
  // the width the overlay will fit() to (issue #374, #126). Remote
  // launches have no terminal/mirror, so it only matters for pty.
  if (mode !== 'remote') applyLaunchSizePayload(payload);
  try {
    const body = await jsonApi('/api/life-os/recap/launch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    toast(
      'Launched weekly recap' + modelTag(model) +
        (mode === 'remote' ? ' (detached)' : ''),
      'good',
      { icon: 'sprout' }
    );
    // A desktop browser gets its terminal in a dedicated PC Edge window,
    // not in-page (issue #241) — so it stays on the launcher SPA.
    handleLaunchResponse(body.session);
  } catch (exc) {
    apiFailToast('Recap launch failed', exc);
  }
}

async function launchSkill(s) {
  // Resume (issue #151) reopens Claude's session picker, dropping the
  // /<skill> prompt. Detached and Resume are orthogonal (issue #157,
  // matching the Coding tab): Detached → 'remote' independent of Resume, so
  // a Detached+Resume launch renders the picker in the detached console
  // while Resume alone streams it to the phone over a PTY.
  const resume = !!(els.lifeOsResume && els.lifeOsResume.getAttribute('aria-checked') === 'true');
  const mode = (els.lifeOsDetached && els.lifeOsDetached.getAttribute('aria-checked') === 'true')
    ? 'remote' : 'pty';
  const model = lifeOsModel();
  const payload = { mode: mode, model: model, resume: resume };
  // Same size contract as launchRecap (issue #374, #126, #241). Remote
  // launches have no terminal/mirror, so it only matters for pty.
  if (mode !== 'remote') applyLaunchSizePayload(payload);
  try {
    const body = await jsonApi(
      '/api/life-os/skills/' + encodeURIComponent(s.id) + '/launch',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }
    );
    toast(
      (resume ? 'Resumed ' : 'Launched ') + s.name +
        modelTag(model) + (mode === 'remote' ? ' (detached)' : ''),
      'good',
      { icon: resume ? 'rotate-ccw' : 'sprout' }
    );
    // Full-control sessions drop straight into the terminal; detached
    // ones only appear in the Coding tab's running-sessions list. A
    // desktop browser gets its terminal in a dedicated PC Edge window
    // instead of in-page (issue #241), so it stays on the launcher SPA.
    handleLaunchResponse(body.session);
  } catch (exc) {
    apiFailToast('Launch failed', exc);
  }
}

// --------------------------------------------------- content browser
// The file currently shown in the doc view — drives the toolbar 🗑️ (which
// deletes conversation logs only). Null while we're on the file list.
let openDocFile = null;
// True when the browser overlay was opened *only* to read one capture from
// the Conversations view (#727) — there is no file list behind it, so
// closing the document closes the whole overlay and lands back on that view.
let captureOnlyDoc = false;

async function openBrowser(s) {
  captureOnlyDoc = false;
  state.lifeOsBrowser = { skillId: s.id, name: s.name, files: [] };
  els.lifeOsBrowserTitle.textContent = s.name;
  closeDoc();                       // start on the full-screen file list
  els.lifeOsBrowser.hidden = false;
  await loadFileList();
}

// (Re)load the current skill's file list — runs every time the browser
// overlay opens, so a conversation log added on the PC shows up on reopen.
async function loadFileList() {
  const b = state.lifeOsBrowser;
  if (!b) return;
  els.lifeOsFileList.innerHTML = '<li class="muted small">Loading…</li>';
  try {
    const body = await jsonApi(
      '/api/life-os/skills/' + encodeURIComponent(b.skillId) + '/files'
    );
    b.files = body.files || [];
    renderFileList(b.files);
  } catch (exc) {
    // The content endpoints are Tailscale + passkey gated — a 403 here
    // means this connection can't reach them. Say so plainly, in the
    // (full-screen) list area.
    const msg = (exc && exc.status === 403)
      ? 'The content browser is Tailscale-only (and passkey-gated). Open the ' +
        'launcher over your Tailscale URL on an enrolled device.'
      : 'Could not load files: ' + (exc.message || exc);
    els.lifeOsFileList.innerHTML = '';
    const li = document.createElement('li');
    li.className = 'muted small';
    li.textContent = msg;
    els.lifeOsFileList.appendChild(li);
  }
}

function renderFileList(files) {
  const host = els.lifeOsFileList;
  host.innerHTML = '';
  if (!files.length) {
    const p = document.createElement('li');
    p.className = 'muted small';
    p.textContent = 'No readable files.';
    host.appendChild(p);
    return;
  }
  let lastCat = null;
  files.forEach(function (f) {
    if (f.category !== lastCat) {
      const h = document.createElement('li');
      h.className = 'lifeos-file-cat';
      h.textContent = f.category;
      host.appendChild(h);
      lastCat = f.category;
    }
    const li = document.createElement('li');
    li.className = 'lifeos-file-row';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'lifeos-file-btn';
    btn.textContent = f.name;
    btn.title = f.path;
    btn.addEventListener('click', function () {
      Array.prototype.forEach.call(
        host.querySelectorAll('.lifeos-file-btn.active'),
        function (b) { b.classList.remove('active'); }
      );
      btn.classList.add('active');
      loadFile(f);
    });
    li.appendChild(btn);
    // No delete control in the list — the list is navigation only. The 🗑️
    // for a disposable conversation log lives in the document toolbar and
    // appears once the log is open (see openDoc / loadFile below).
    host.appendChild(li);
  });
}

async function deleteFile(f) {
  if (!confirm(
    'Delete this conversation log?\n\n' + f.name +
    '\n\nThe file is removed from disk — this cannot be undone.'
  )) return;
  try {
    await jsonApi(
      '/api/life-os/file?path=' + encodeURIComponent(f.path),
      { method: 'DELETE' }
    );
    toast('Deleted ' + f.name, 'good', { icon: 'trash-2' });
    closeDoc();             // in case the deleted file was the open one
    await refreshAfterLogChange();
  } catch (exc) {
    apiFailToast('Delete failed', exc);
  }
}

// A conversation log can be deleted or renamed from either surface — the
// Browse file list or the Conversations view (#727). Refresh whichever one
// is actually on screen; refreshing the other would render into a hidden
// overlay and leave the visible list stale.
async function refreshAfterLogChange() {
  if (els.lifeOsConvos && !els.lifeOsConvos.hidden) {
    const query = els.lifeOsConvoQuery.value.trim();
    if (query) await runConvoSearch(query);
    else if (convoScope()) await loadConvos();
    return;
  }
  await loadFileList();
}

// Lower-case, spaces (and any other punctuation) → single dashes, trimmed —
// the same shape the capture hook's slugs already have.
function slugify(s) {
  return String(s).trim().toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

async function renameFile(f) {
  const proposed = window.prompt(
    'Rename this conversation log.\n\n' +
    'The date keeps unchanged — type the new name (spaces become dashes, ' +
    'lower-cased):',
    ''
  );
  if (proposed === null) return;            // cancelled
  const slug = slugify(proposed);
  if (!slug) { toast('Name cannot be empty', 'error'); return; }
  try {
    const body = await jsonApi('/api/life-os/file/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: f.path, slug: slug }),
    });
    toast('Renamed to ' + (body.name || slug), 'good', { icon: 'pencil' });
    closeDoc();             // name (and path) changed — back to the list
    await refreshAfterLogChange();
  } catch (exc) {
    apiFailToast('Rename failed', exc);
  }
}

async function loadFile(f) {
  // The file view is a full-screen layer over the list; the ✕ close-doc
  // button in the bar appears only while it's open.
  openDocFile = f;
  openDoc(f);
  els.lifeOsFileContent.innerHTML = '<p class="muted small">Loading…</p>';
  try {
    const body = await jsonApi(
      '/api/life-os/file?path=' + encodeURIComponent(f.path)
    );
    els.lifeOsFileContent.innerHTML = renderMarkdown(body.content || '');
    if (body.truncated) {
      const note = document.createElement('p');
      note.className = 'muted small';
      note.textContent = '… (truncated)';
      els.lifeOsFileContent.appendChild(note);
    }
    els.lifeOsFileContent.scrollTop = 0;
  } catch (exc) {
    els.lifeOsFileContent.innerHTML = '';
    const p = document.createElement('p');
    p.className = 'muted small';
    p.textContent = 'Could not load: ' + (exc.message || exc);
    els.lifeOsFileContent.appendChild(p);
  }
}

// A conversation log the toolbar may act on — any file under a skill's
// conversations/ EXCEPT the .gitkeep placeholder that keeps the (otherwise
// empty) dir tracked in git. Deleting/renaming that would untrack the dir,
// so it stays off-limits (the server refuses it too — defence in depth).
function isEditableLog(f) {
  return !!f && f.category === 'conversations' &&
    !/(^|\/)\.gitkeep$/.test(f.name || '');
}

// Reveal the full-screen file view (overlaying the list) + the ✕ button.
// The 🗑️ delete and ✏️ rename show only for a conversation log — disposable
// run transcripts, editable while you read them. Every other category (and
// the .gitkeep placeholder) keeps both hidden.
function openDoc(f) {
  els.lifeOsFileContent.hidden = false;
  if (els.lifeOsDocClose) els.lifeOsDocClose.hidden = false;
  const editable = isEditableLog(f);
  if (els.lifeOsDocDelete) els.lifeOsDocDelete.hidden = !editable;
  if (els.lifeOsDocRename) els.lifeOsDocRename.hidden = !editable;
}

// Close the open file → back to the full-screen file list, or — when the
// overlay only ever held this one capture — back to the Conversations view.
function closeDoc() {
  if (captureOnlyDoc) {
    captureOnlyDoc = false;
    closeBrowser();
    return;
  }
  openDocFile = null;
  els.lifeOsFileContent.hidden = true;
  els.lifeOsFileContent.innerHTML = '';
  if (els.lifeOsDocClose) els.lifeOsDocClose.hidden = true;
  if (els.lifeOsDocDelete) els.lifeOsDocDelete.hidden = true;
  if (els.lifeOsDocRename) els.lifeOsDocRename.hidden = true;
  Array.prototype.forEach.call(
    els.lifeOsFileList.querySelectorAll('.lifeos-file-btn.active'),
    function (b) { b.classList.remove('active'); }
  );
}

// Close the whole browser → back to the skill tiles.
function closeBrowser() {
  state.lifeOsBrowser = null;
  closeDoc();
  els.lifeOsBrowser.hidden = true;
}

// --------------------------------------------------- conversations (#727)
// The digested conversation index + ranked cross-skill search, with a ↺ that
// reattaches to one exact session instead of opening Claude's native picker.
// Opened scoped from a tile's 🕘, or unscoped from the Skills header's 🔎.
//
// { skill: <id|null>, name: <label>, allSkills: bool, scoped: bool,
//   searching: bool, sourceRows: [], rows: [] }
let convoView = null;
let convoQueryTimer = null;

// The ordering applied to *search* results — 'relevance' (the server's rank
// order) until the user taps the toggle, and reset to it whenever the query
// box is cleared or the view is reopened. Deliberately not persisted: the
// browse list keeps its own remembered date sort in state.lifeOsConvoSort.
let convoSearchSort = 'relevance';

function convoScope() {
  // The skill filter actually sent to the server: null once the view has been
  // widened to every skill, even though it was opened from one tile.
  return (convoView && !convoView.allSkills) ? convoView.skill : null;
}

export function openConvos(skill) {
  // A cached older index.html with this newer bundle would have no overlay to
  // render into — bail rather than throwing on the first property access.
  if (!els.lifeOsConvos || !els.lifeOsConvoQuery) return;
  convoView = {
    skill: skill ? skill.id : null,
    name: skill ? skill.name : 'Conversations',
    allSkills: !skill,
    // Whether the rows on screen came from a single-skill list — set by
    // renderConvoRows, so a sort toggle can re-render without a refetch.
    scoped: !!skill,
    // Whether those rows are search hits (server-ranked) rather than a
    // browse index — the two lists do not want the same default order.
    searching: false,
    // The rows exactly as the server sent them, so a date re-sort of search
    // results is reversible back to relevance without a refetch.
    sourceRows: [],
    rows: [],
  };
  convoSearchSort = 'relevance';
  els.lifeOsConvosTitle.textContent = convoView.name;
  els.lifeOsConvoQuery.value = '';
  // The scope toggle only means something for a view that started scoped.
  if (els.lifeOsConvosScope) {
    els.lifeOsConvosScope.hidden = !skill;
    els.lifeOsConvosScope.setAttribute('aria-pressed', 'false');
  }
  syncConvoSortBtn();
  els.lifeOsConvos.hidden = false;
  if (skill) loadConvos();
  else showConvoState('empty', 'Search every skill’s conversations.');
}

function closeConvos() {
  convoView = null;
  window.clearTimeout(convoQueryTimer);
  els.lifeOsConvos.hidden = true;
  els.lifeOsConvoList.innerHTML = '';
}

// The five lifecycle states render through one canonical block — a glyph, a
// one-line reason, and at most one Retry — never a blank pane and never a
// toast for something that isn't a user-initiated command.
const CONVO_STATE_ICON = {
  loading: 'hourglass',
  empty: 'messages-square',
  error: 'triangle-alert',
};

function showConvoState(kind, message, retry) {
  const host = els.lifeOsConvoState;
  if (!host) return;
  els.lifeOsConvoList.innerHTML = '';
  host.innerHTML = '';
  host.hidden = false;
  const glyph = document.createElement('div');
  glyph.innerHTML = icon(CONVO_STATE_ICON[kind] || 'messages-square');
  host.appendChild(glyph);
  const text = document.createElement('div');
  text.textContent = message;
  host.appendChild(text);
  if (retry) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'button-ghost lifeos-convo-retry';
    btn.textContent = 'Retry';
    btn.addEventListener('click', retry);
    host.appendChild(btn);
  }
}

function hideConvoState() {
  if (els.lifeOsConvoState) els.lifeOsConvoState.hidden = true;
}

// Browse one skill's index — the no-query view. `available: false` means the
// indexer simply hasn't digested this skill yet; that's an honest empty
// state, not an error.
async function loadConvos() {
  const skill = convoView && convoView.skill;
  if (!skill) return;
  showConvoState('loading', 'Reading conversations…');
  try {
    const body = await jsonApi(
      '/api/life-os/skills/' + encodeURIComponent(skill) + '/conversations'
    );
    if (!convoView || convoView.skill !== skill) return;   // view moved on
    if (!body.available) {
      showConvoState('empty', 'No conversation index yet for this skill.');
      return;
    }
    renderConvoRows(body.conversations || [], { scoped: true, search: false });
  } catch (exc) {
    showConvoState('error', convoFailure(exc), loadConvos);
  }
}

// Ranked search across every skill (or the current scope). The server owns
// the ranking; a missing CLI or database comes back as available:false with a
// short reason, which reads as "search unavailable" — never an error toast.
async function runConvoSearch(query) {
  const scope = convoScope();
  showConvoState('loading', 'Searching…');
  let url = '/api/life-os/conversations/search?q=' + encodeURIComponent(query);
  if (scope) url += '&skill=' + encodeURIComponent(scope);
  try {
    const body = await jsonApi(url);
    if (!convoView || els.lifeOsConvoQuery.value.trim() !== query) return;
    if (!body.available) {
      showConvoState('error', 'Search unavailable — ' + (body.reason || 'try again later.'));
      return;
    }
    const rows = body.results || [];
    if (!rows.length) {
      showConvoState('empty', 'Nothing matched “' + query + '”.');
      return;
    }
    renderConvoRows(rows, { scoped: !!scope, search: true });
  } catch (exc) {
    showConvoState('error', convoFailure(exc), function () { runConvoSearch(query); });
  }
}

// The content endpoints are Tailscale + passkey gated — say that plainly
// rather than leaking a status code onto the phone.
function convoFailure(exc) {
  if (exc && exc.status === 403) {
    return 'Conversations are Tailscale-only (and passkey-gated). Open the ' +
      'launcher over your Tailscale URL on an enrolled device.';
  }
  return 'Could not load conversations.';
}

function onConvoQuery() {
  window.clearTimeout(convoQueryTimer);
  const query = els.lifeOsConvoQuery.value.trim();
  convoQueryTimer = window.setTimeout(function () {
    if (query) { runConvoSearch(query); return; }
    // Cleared box: drop the transient search ordering (the browse list keeps
    // its own remembered date sort) and go back to the no-query view.
    convoSearchSort = 'relevance';
    if (convoView) convoView.searching = false;
    syncConvoSortBtn();
    if (convoScope()) loadConvos();
    else showConvoState('empty', 'Search every skill’s conversations.');
  }, 250);
}

// ------------------------------------------------- sort (#886, #1074)
//
// Orderings over rows already on the client, so a toggle costs no round
// trip. The browse list and the search results land here with *different*
// defaults (#1074): browsing defaults to a date sort, while a search
// defaults to 'relevance' — the server's FTS5 rank order, which the client's
// only job is not to destroy.
//
// 'interaction' is the browse default and the point of #886:
// `last_interaction` is the capture file's mtime, which life-os moves
// forward whenever a resumed session appends turns, so the conversation you
// were last in floats to the top. 'created' is the date-stamped filename
// order the index is written in.

// The ordering in force for whatever is on screen right now.
function activeConvoSort() {
  if (convoView && convoView.searching) return convoSearchSort;
  return state.lifeOsConvoSort;
}

// Which date leads each row. In relevance order neither date drives the
// list, so the remembered browse preference still decides the display.
function convoDateSort() {
  const mode = activeConvoSort();
  return mode === 'relevance' ? state.lifeOsConvoSort : mode;
}

function sortedConvoRows(rows) {
  const mode = activeConvoSort();
  // Relevance is the server's own order — pass it through untouched.
  if (mode === 'relevance') return rows.slice();
  const key = mode === 'created' ? 'date' : 'last_interaction';
  return rows.slice().sort(function (a, b) {
    // A server too old to send `last_interaction` (or a row whose capture
    // could not be stat'd) degrades to the creation date rather than sorting
    // as the empty string and sinking to the bottom.
    const av = String(a[key] || a.date || '');
    const bv = String(b[key] || b.date || '');
    if (av !== bv) return av < bv ? 1 : -1;   // ISO dates: lexical == chronological
    // Same day: the date-stamped filename carries the time too, so it is the
    // stable tie-break — without it, equal rows could shuffle on re-render.
    return String(b.file || '').localeCompare(String(a.file || ''));
  });
}

function syncConvoSortBtn() {
  const btn = els.lifeOsConvosSort;
  if (!btn) return;
  const mode = activeConvoSort();
  // While searching the cycle has a third state, so the "tap to…" half of
  // each hint names that cycle's own next stop, never the browse one.
  const searching = !!(convoView && convoView.searching);
  if (mode === 'relevance') {
    // `search`, not `star` — a star already means "favorite" on the Coding
    // tab, and this state is "ordered by how well the hit matches".
    btn.innerHTML = icon('search') + ' Relevance';
    btn.title = 'Best match first — tap to sort by last interaction';
  } else if (mode === 'created') {
    btn.innerHTML = icon('calendar-days') + ' Created';
    btn.title = 'Sorted by creation date — tap to sort by ' +
      (searching ? 'best match' : 'last interaction');
  } else {
    btn.innerHTML = icon('timer') + ' Recent';
    btn.title = 'Sorted by last interaction — tap to sort by creation date';
  }
}

function toggleConvoSort() {
  if (convoView && convoView.searching) {
    // Search results cycle relevance → recent → created → relevance, so the
    // server's ranking stays reachable and a date ordering of the hits is
    // still available. The choice is transient — clearing the box restores
    // the browse default (#1074).
    convoSearchSort =
      convoSearchSort === 'relevance' ? 'interaction' :
      convoSearchSort === 'interaction' ? 'created' : 'relevance';
  } else {
    state.lifeOsConvoSort =
      state.lifeOsConvoSort === 'created' ? 'interaction' : 'created';
    localStorage.setItem('launcher.lifeOsConvoSort', state.lifeOsConvoSort);
  }
  // Re-order what is already on screen; a refetch would return the same rows.
  // Re-sorting from the *server's* order, not the rendered one, is what makes
  // relevance reachable again after a date sort.
  if (convoView && convoView.sourceRows.length) {
    renderConvoRows(convoView.sourceRows, {
      scoped: convoView.scoped, search: convoView.searching,
    });
  } else {
    syncConvoSortBtn();
  }
}

function renderConvoRows(rows, opts) {
  const host = els.lifeOsConvoList;
  hideConvoState();
  host.innerHTML = '';
  convoView.scoped = !!(opts && opts.scoped);
  // Set before sorting: sortedConvoRows() reads it to pick the ordering.
  convoView.searching = !!(opts && opts.search);
  convoView.sourceRows = rows.slice();
  // Store the sorted order: refreshConvoActions() pairs DOM nodes with
  // convoView.rows by index, so the two must not drift apart.
  convoView.rows = sortedConvoRows(rows);
  // The label depends on which list is on screen, so it is re-synced on
  // every render, not only on a tap.
  syncConvoSortBtn();
  if (!convoView.rows.length) {
    showConvoState('empty', 'No conversations yet.');
    return;
  }
  convoView.rows.forEach(function (r) {
    host.appendChild(convoRow(r, convoView.scoped));
  });
}

function convoRow(r, scoped) {
  const li = document.createElement('li');
  li.className = 'lifeos-convo-row';

  const head = document.createElement('button');
  head.type = 'button';
  head.className = 'lifeos-convo-head';
  head.setAttribute('aria-expanded', 'false');
  const when = document.createElement('span');
  when.className = 'lifeos-convo-when';
  appendConvoDates(when, r);
  head.appendChild(when);
  const topic = document.createElement('span');
  topic.className = 'lifeos-convo-topic';
  topic.textContent = r.topic || r.slug || r.file || 'untitled';
  head.appendChild(topic);
  // Which skill a hit came from only matters when the list spans several.
  if (!scoped && r.skill) {
    const tag = document.createElement('span');
    tag.className = 'lifeos-convo-tag';
    tag.textContent = r.skill;
    head.appendChild(tag);
  }
  li.appendChild(head);

  const detail = document.createElement('div');
  detail.className = 'lifeos-convo-detail';
  detail.hidden = true;
  appendConvoField(detail, 'Decisions', r.decisions);
  appendConvoField(detail, 'Open loops', r.open_loops);
  appendConvoField(detail, 'Source', r.agent || 'Unknown legacy harness');
  detail.appendChild(convoActions(r));
  li.appendChild(detail);

  head.addEventListener('click', function () {
    const open = detail.hidden;
    detail.hidden = !open;
    head.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  return li;
}

// Both dates in the one existing column (#886): the active sort's on top,
// the other beneath it. The second line appears *only* when the two differ —
// a capture that was never resumed has mtime == its filename date, and
// echoing one day twice on every row is noise, not information.
function appendConvoDates(host, r) {
  const created = r.date || '';
  const touched = r.last_interaction || created;
  const byCreated = convoDateSort() === 'created';
  const primary = byCreated ? created : touched;
  const secondary = byCreated ? touched : created;
  const top = document.createElement('span');
  top.className = 'lifeos-convo-when-primary';
  top.textContent = primary;
  top.title = byCreated ? 'Created' : 'Last interaction';
  host.appendChild(top);
  if (secondary && secondary !== primary) {
    const alt = document.createElement('span');
    alt.className = 'lifeos-convo-when-alt';
    alt.textContent = secondary;
    alt.title = byCreated ? 'Last interaction' : 'Created';
    host.appendChild(alt);
  }
}

// The digest writes a literal "none" when a section is empty — showing that
// back as a field is noise, so it's dropped.
function appendConvoField(host, label, value) {
  const text = String(value || '').trim();
  if (!text || text.toLowerCase() === 'none') return;
  const p = document.createElement('p');
  p.className = 'lifeos-convo-field';
  const b = document.createElement('strong');
  b.textContent = label + ': ';
  p.appendChild(b);
  p.appendChild(document.createTextNode(text));
  host.appendChild(p);
}

function convoActions(r) {
  const wrap = document.createElement('div');
  wrap.className = 'lifeos-convo-actions';

  const target = lifeOsModel().split(':')[0];
  const matches = target === r.agent;
  const provider = r.agent === 'codex' ? 'Codex' : 'Claude';
  if (r.resumable && r.skill) {
    const resumeBtn = document.createElement('button');
    resumeBtn.type = 'button';
    resumeBtn.className = 'button-ghost lifeos-convo-resume' + (matches ? ' accent-btn' : '');
    resumeBtn.innerHTML = icon('rotate-ccw') + ' Resume in ' + provider;
    resumeBtn.disabled = !matches;
    resumeBtn.addEventListener('click', function () { resumeConversation(r, 'resume'); });
    wrap.appendChild(resumeBtn);
  }
  const reason = !r.resumable ? (r.resume_reason || 'No stored session; readable only.') :
    (!matches ? 'Select a ' + provider + ' model to resume this source.' : '');
  if (reason) {
    const chip = document.createElement('span');
    chip.className = 'lifeos-convo-nosession';
    chip.textContent = reason;
    wrap.appendChild(chip);
  }
  if (r.handoff_available && r.skill && !matches && ['claude', 'codex'].includes(target)) {
    const handoffBtn = document.createElement('button');
    handoffBtn.type = 'button';
    handoffBtn.className = 'button-ghost accent-btn lifeos-convo-handoff';
    handoffBtn.textContent = 'Start new in ' + (target === 'codex' ? 'Codex' : 'Claude');
    handoffBtn.addEventListener('click', function () { resumeConversation(r, 'handoff'); });
    wrap.appendChild(handoffBtn);
  }

  if (r.path) {
    const openBtn = document.createElement('button');
    openBtn.type = 'button';
    openBtn.className = 'button-ghost';
    openBtn.innerHTML = icon('book-open');
    openBtn.title = 'Open the raw capture';
    openBtn.setAttribute('aria-label', 'Open the raw capture');
    openBtn.addEventListener('click', function () { openCapture(r); });
    wrap.appendChild(openBtn);

    const delBtn = document.createElement('button');
    delBtn.type = 'button';
    delBtn.className = 'button-ghost';
    delBtn.innerHTML = icon('trash-2');
    delBtn.title = 'Delete this conversation log';
    delBtn.setAttribute('aria-label', 'Delete this conversation log');
    delBtn.addEventListener('click', function () {
      deleteFile({ path: r.path, name: r.file });
    });
    wrap.appendChild(delBtn);

    const renBtn = document.createElement('button');
    renBtn.type = 'button';
    renBtn.className = 'button-ghost';
    renBtn.innerHTML = icon('pencil');
    renBtn.title = 'Rename this conversation log';
    renBtn.setAttribute('aria-label', 'Rename this conversation log');
    renBtn.addEventListener('click', function () {
      renameFile({ path: r.path, name: r.file });
    });
    wrap.appendChild(renBtn);
  }
  return wrap;
}

// Read one capture in the existing document viewer, layered over this view —
// closing it comes straight back here, not to a file list we never loaded.
function openCapture(r) {
  captureOnlyDoc = true;
  els.lifeOsBrowserTitle.textContent = r.skill || 'conversation';
  els.lifeOsFileList.innerHTML = '';
  els.lifeOsBrowser.hidden = false;
  loadFile({ path: r.path, name: r.file, category: 'conversations' });
}

// Refresh only actions when the model changes, preserving expanded history rows.
function refreshConvoActions() {
  if (!convoView) return;
  const actions = els.lifeOsConvoList.querySelectorAll('.lifeos-convo-actions');
  actions.forEach(function (node, index) {
    if (convoView.rows[index]) node.replaceWith(convoActions(convoView.rows[index]));
  });
}

async function resumeConversation(r, action) {
  const mode = (els.lifeOsDetached && els.lifeOsDetached.getAttribute('aria-checked') === 'true')
    ? 'remote' : 'pty';
  const model = lifeOsModel();
  const payload = {
    mode: mode, model: model, action: action,
    capture: { path: r.path, revision: r.revision, agent: r.agent, sid: r.sid },
  };
  if (action === 'handoff') {
    const scope = r.handoff_truncated ?
      'Only the first ' + (r.handoff_limit || 24000).toLocaleString() + ' characters will be included.' :
      'Only this selected capture will be included.';
    if (!confirm('Start a NEW conversation in ' + model.split(':')[0] + '?\n\n' +
        scope + ' Source provenance is preserved. This does not resume or convert the original session. ' +
        'Memory and knowledge edits still require your approval.')) return;
    payload.confirm_new = true;
  }
  if (mode !== 'remote') applyLaunchSizePayload(payload);
  try {
    const body = await jsonApi(
      '/api/life-os/skills/' + encodeURIComponent(r.skill) + '/conversations/launch',
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }
    );
    toast(
      (action === 'handoff' ? 'Started new conversation from ' : 'Resumed ') +
        (r.topic || r.skill) + modelTag(model) +
        (body.handoff_truncated ? ' (selected context truncated)' : '') +
        (mode === 'remote' ? ' (detached)' : ''),
      'good', { icon: action === 'handoff' ? 'messages-square' : 'rotate-ccw' }
    );
    closeConvos();
    handleLaunchResponse(body.session);
  } catch (exc) {
    apiFailToast(action === 'handoff' ? 'New conversation failed' : 'Resume failed', exc);
  }
}

// --------------------------------------------------------------- wire
export function wireLifeOs() {
  if (els.lifeOsBrowserBack) {
    els.lifeOsBrowserBack.addEventListener('click', closeBrowser);
  }
  if (els.lifeOsDocClose) {
    els.lifeOsDocClose.addEventListener('click', closeDoc);
  }
  if (els.lifeOsDocDelete) {
    // Delete the open conversation log → confirm, DELETE, back to the list
    // (deleteFile closeDoc()s, exactly like ✕).
    els.lifeOsDocDelete.addEventListener('click', function () {
      if (openDocFile) deleteFile(openDocFile);
    });
  }
  if (els.lifeOsDocRename) {
    // Rename the open conversation log → prompt, POST, back to the list.
    els.lifeOsDocRename.addEventListener('click', function () {
      if (openDocFile) renameFile(openDocFile);
    });
  }
  if (els.lifeOsRecapLaunch) {
    els.lifeOsRecapLaunch.addEventListener('click', launchRecap);
  }
  // Conversations view (#727): ✕ back to the tiles, the debounced query box,
  // and the scope toggle that widens a skill-scoped view to every skill.
  if (els.lifeOsConvosBack) {
    els.lifeOsConvosBack.addEventListener('click', closeConvos);
  }
  if (els.lifeOsConvoQuery) {
    els.lifeOsConvoQuery.addEventListener('input', onConvoQuery);
  }
  if (els.lifeOsConvosScope) {
    els.lifeOsConvosScope.addEventListener('click', function () {
      if (!convoView) return;
      convoView.allSkills = !convoView.allSkills;
      els.lifeOsConvosScope.setAttribute(
        'aria-pressed', convoView.allSkills ? 'true' : 'false'
      );
      els.lifeOsConvosTitle.textContent =
        convoView.allSkills ? 'All skills' : convoView.name;
      onConvoQuery();
    });
  }
  if (els.lifeOsConvosSort) {
    syncConvoSortBtn();
    els.lifeOsConvosSort.addEventListener('click', toggleConvoSort);
  }
  // The Skills header 🔎 opens the same view unscoped. It shares the summary
  // with the model combo and the toggles, so a tap must not also collapse
  // the panel (same reason as the switches below).
  if (els.lifeOsConvoSearch) {
    els.lifeOsConvoSearch.addEventListener('click', function (ev) {
      ev.stopPropagation();
      ev.preventDefault();
      openConvos(null);
      if (els.lifeOsConvoQuery) els.lifeOsConvoQuery.focus();
    });
  }
  // Detached/Resume are plain client-side switches (issue #355) — no server
  // config, just read at launch time above. They live in the Skills card's
  // <summary> (#496 round 2, mirroring the Coding tab's Projects card), so
  // stopPropagation keeps a tap from also collapsing the panel.
  [els.lifeOsDetached, els.lifeOsResume].forEach(function (btn) {
    if (!btn) return;
    btn.addEventListener('click', function (ev) {
      ev.stopPropagation();
      toggleAriaChecked(btn);
    });
  });
  // The provider-qualified model dropdown (#540/#845) shares that summary;
  // wireModelCombo owns its open/close + the summary-tap guard.
  lifeOsModelCombo = wireModelCombo(
    document.getElementById('lifeOsModelCombo'), function (choice) {
      if (lifeOsConvosModelCombo) {
        lifeOsConvosModelCombo.setValue(choice);
        refreshConvoActions();
      }
    }
  );
  lifeOsConvosModelCombo = wireModelCombo(
    document.getElementById('lifeOsConvosModelCombo'), function (choice) {
      if (lifeOsModelCombo) lifeOsModelCombo.setValue(choice);
      refreshConvoActions();
    }
  );
  // Refresh skills + recap staleness the moment the tab opens (cheap: a live
  // directory scan + a single ledger stat).
  if (els.tabLifeOS) {
    els.tabLifeOS.addEventListener('click', function () {
      fetchSkills().catch(function () {});
      fetchRecapStatus().catch(function () {});
    });
  }
}
