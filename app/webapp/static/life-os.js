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
import { apiFailToast, escapeHtml, jsonApi, toast, logPollFailure } from './api.js';
import { applyLaunchSizePayload, handleLaunchResponse } from './terminal.js';
import { icon } from './_vendored/icons/icons.js';
import { toggleAriaChecked, wireModelCombo } from './dom-utils.js';

// The Skills-summary launch-model dropdown controller ({setValue, getValue}),
// created in the tab's wiring once the DOM exists (#540). Read at launch time;
// no server round-trip — it's per-launch, like the Board dispatch combo.
let lifeOsModelCombo = null;
function lifeOsModel() {
  return (lifeOsModelCombo && lifeOsModelCombo.getValue()) || 'sonnet';
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

  skills.forEach(function (s) {
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

    li.appendChild(actions);
    host.appendChild(li);
  });
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
  if (!model || model === 'sonnet') return '';
  return ' (' + model.charAt(0).toUpperCase() + model.slice(1) + ')';
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
// { skill: <id|null>, name: <label>, allSkills: bool, rows: [] }
let convoView = null;
let convoQueryTimer = null;

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
    rows: [],
  };
  els.lifeOsConvosTitle.textContent = convoView.name;
  els.lifeOsConvoQuery.value = '';
  // The scope toggle only means something for a view that started scoped.
  if (els.lifeOsConvosScope) {
    els.lifeOsConvosScope.hidden = !skill;
    els.lifeOsConvosScope.setAttribute('aria-pressed', 'false');
  }
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
    renderConvoRows(body.conversations || [], { scoped: true });
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
    renderConvoRows(rows, { scoped: !!scope });
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
    // Cleared box: back to whatever the view shows with no query.
    if (convoScope()) loadConvos();
    else showConvoState('empty', 'Search every skill’s conversations.');
  }, 250);
}

function renderConvoRows(rows, opts) {
  const host = els.lifeOsConvoList;
  hideConvoState();
  host.innerHTML = '';
  convoView.rows = rows;
  if (!rows.length) {
    showConvoState('empty', 'No conversations yet.');
    return;
  }
  rows.forEach(function (r) {
    host.appendChild(convoRow(r, opts && opts.scoped));
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
  when.textContent = r.date || '';
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
  detail.appendChild(convoActions(r));
  li.appendChild(detail);

  head.addEventListener('click', function () {
    const open = detail.hidden;
    detail.hidden = !open;
    head.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  return li;
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

  // The launch route is per-skill, so a hit with no skill has nowhere to
  // resume into — treat it exactly like a missing session id rather than
  // rendering a button that can only 404.
  if (r.resumable && r.skill) {
    const resumeBtn = document.createElement('button');
    resumeBtn.type = 'button';
    // Ghost + accent-btn, not button-tint: this app sizes every .button-tint
    // at width:100%, which in a list row reads as the *view's* primary action
    // and swamps the rest of the row. The accent-btn modifier gives the same
    // accent emphasis at row scale.
    resumeBtn.className = 'button-ghost accent-btn lifeos-convo-resume';
    resumeBtn.innerHTML = icon('rotate-ccw') + ' Resume this';
    resumeBtn.addEventListener('click', function () { resumeConversation(r); });
    wrap.appendChild(resumeBtn);
  } else {
    // No stored session id (or a non-claude agent): the conversation is
    // readable but cannot be reopened. Said out loud, because a phone has no
    // hover to explain a greyed-out button — and roughly a quarter of the
    // archive predates the stored id.
    const chip = document.createElement('span');
    chip.className = 'lifeos-convo-nosession';
    chip.textContent = 'no session';
    chip.title = 'This conversation was captured before its session id was ' +
      'stored, so it can be read but not reopened.';
    wrap.appendChild(chip);
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

// ↺ — reattach to this exact conversation. Honours the same Detached toggle
// and model combo as every other Life OS launch; the server validates the id
// and composes `--resume <sid>`.
async function resumeConversation(r) {
  const mode = (els.lifeOsDetached && els.lifeOsDetached.getAttribute('aria-checked') === 'true')
    ? 'remote' : 'pty';
  const model = lifeOsModel();
  const payload = { mode: mode, model: model, resume_sid: r.sid };
  if (mode !== 'remote') applyLaunchSizePayload(payload);
  try {
    const body = await jsonApi(
      '/api/life-os/skills/' + encodeURIComponent(r.skill) + '/launch',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      }
    );
    toast(
      'Resumed ' + (r.topic || r.skill) + modelTag(model) +
        (mode === 'remote' ? ' (detached)' : ''),
      'good',
      { icon: 'rotate-ccw' }
    );
    closeConvos();
    handleLaunchResponse(body.session);
  } catch (exc) {
    apiFailToast('Resume failed', exc);
  }
}

// ------------------------------------------------ minimal markdown render
// Escape-first, then apply a small, safe subset (headings, bold, italic,
// inline code, fenced code, links, unordered lists, paragraphs). Content
// comes from the user's own private files over a passkey-gated tailnet
// link, but we still escape every byte before formatting so a stray
// `<script>` in a note can never execute.
function inlineMd(s) {
  return s
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

export function renderMarkdown(text) {
  const lines = escapeHtml(text).split('\n');
  const out = [];
  let inCode = false;
  let inList = false;
  let para = [];

  function flushPara() {
    if (para.length) {
      out.push('<p>' + inlineMd(para.join(' ')) + '</p>');
      para = [];
    }
  }
  function flushList() {
    if (inList) { out.push('</ul>'); inList = false; }
  }

  lines.forEach(function (line) {
    if (line.trim().startsWith('```')) {
      flushPara(); flushList();
      if (inCode) { out.push('</code></pre>'); inCode = false; }
      else { out.push('<pre class="md-code"><code>'); inCode = true; }
      return;
    }
    if (inCode) { out.push(line); return; }

    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      flushPara(); flushList();
      const level = h[1].length;
      out.push('<h' + level + '>' + inlineMd(h[2]) + '</h' + level + '>');
      return;
    }
    const li = line.match(/^\s*[-*]\s+(.*)$/);
    if (li) {
      flushPara();
      if (!inList) { out.push('<ul>'); inList = true; }
      out.push('<li>' + inlineMd(li[1]) + '</li>');
      return;
    }
    if (!line.trim()) { flushPara(); flushList(); return; }
    para.push(line.trim());
  });
  if (inCode) out.push('</code></pre>');
  flushPara(); flushList();
  return out.join('\n');
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
  // The model dropdown (#540) shares that summary; wireModelCombo owns its
  // open/close + the summary-tap guard. Read at launch time via lifeOsModel().
  lifeOsModelCombo = wireModelCombo(
    document.getElementById('lifeOsModelCombo'), null
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
