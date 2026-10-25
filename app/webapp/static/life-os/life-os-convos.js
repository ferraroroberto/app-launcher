/* Life OS conversation search and sheet.
 *
 * The skills field searches conversations too: from
 * CONVO_MIN_QUERY characters, after the debounce, the matches show as a
 * second group under the skills. A skill's ⋮ Conversations narrows the group
 * to that skill's own list, newest first (the chip in the field); the chip's
 * ✕ widens it to every skill. A search runs only on typing: the text a
 * reload restores filters the skills alone.
 *
 * A row opens the conversation sheet: its name, "skill · when · N messages ·
 * from <harness>", the summary, then Resume as the one primary and every
 * other action as a link row, Delete last in danger. Opened from a row and
 * from a ?convo= link; the reader (life-os-viewer.js), a raw capture, the
 * rename and the confirms all stack over it. What the zombie cleanup
 * archived shows apart, under the list (the archived section below).
 */

import { els, state } from '../state.js';
import { apiFailToast, jsonApi, toast } from '../api.js';
import { applyLaunchSizePayload, handleLaunchResponse } from '../terminal.js';
import { icon } from '../_vendored/icons/icons.js';
import { calendarDaysFromToday, nameLabel } from '../dom-utils.js';
import { actionRow } from '../action-rows.js';
import { confirmDialog } from '../confirm-dialog.js';
import { setAsyncState } from '../async-state.js';
import { closeConvoViewer, openConvoViewer, wireConvoViewer } from './life-os-viewer.js';
import {
  compareSkills, launchDetached, launchSkill, lifeOsModel,
  modelTag, resumedModelTag, refreshAfterLogChange, skillAvatar, skillsFilter, wireSheet,
} from '../life-os.js';
import { askName, closeDoc, loadFile, RENAME_TITLE_SHEET } from './life-os-content.js';

// The group's mode: 'off' (hidden), 'browse' (one skill's index, nothing
// typed), 'search' (ranked hits, within the scope if there is one), or
// 'notice' (a ?convo= link that could not resolve, saying so).
let convoMode = 'off';
// The skill the group is narrowed to ({ id, name }), or null for every skill.
let convoScope = null;
// The rows on screen, as the server sent them (so a date re-sort of search
// hits can return to relevance without a refetch) and as rendered.
let convoSourceRows = [];
let convoRows = [];
let convoQueryTimer = null;
// Bumped by every read and by switching the group off, so an answer that
// lands after the field or scope moved on is dropped.
let convoSeq = 0;
// One letter matches nearly everything: the search starts at two.
const CONVO_MIN_QUERY = 2;

// The ordering applied to *search* results — 'relevance' (the server's rank
// order) until the user picks another, and reset to it whenever the search
// ends. Deliberately not persisted: the browse list keeps its own remembered
// date sort in state.lifeOsConvoSort.
let convoSearchSort = 'relevance';

function fieldQuery() {
  return els.lifeOsFilterInput ? els.lifeOsFilterInput.value.trim() : '';
}

// Whatever the field and the scope now ask for. Resolves to the rows
// rendered (a ?convo= link resolves against them), or null.
function refreshConvos() {
  window.clearTimeout(convoQueryTimer);
  const query = fieldQuery();
  if (query.length >= CONVO_MIN_QUERY) return runConvoSearch(query);
  // Nothing (or one letter) typed: the search's transient order goes, and
  // the group falls back to the skill's own list, or steps aside.
  convoSearchSort = 'relevance';
  if (convoScope) return loadConvos();
  setConvoMode('off');
  return Promise.resolve(null);
}

// Reruns the current browse/search read only while the group shows one —
// the Conversations half of life-os.js's refreshAfterLogChange (the browse
// file list is the other half, in life-os-content.js).
export function refreshConvosIfShowing() {
  if (convoMode === 'browse' || convoMode === 'search') return refreshConvos();
  return Promise.resolve(null);
}

function onFieldInput() {
  window.clearTimeout(convoQueryTimer);
  convoQueryTimer = window.setTimeout(refreshConvos, 250);
}

function setConvoMode(mode) {
  convoMode = mode;
  if (mode !== 'browse' && mode !== 'search') hideArchived();
  if (mode === 'off') {
    convoSeq += 1;
    convoSourceRows = [];
    convoRows = [];
    els.lifeOsConvoList.replaceChildren();
    setAsyncState(els.lifeOsConvoState, 'ready');
  }
  syncConvoGroup();
}

// The field's chip, the group, and the Skills group beside it. Narrowed to
// one skill, the group is that skill's list, so the other skills step aside.
function syncConvoGroup() {
  els.lifeOsConvoGroup.hidden = convoMode === 'off';
  const card = els.lifeOsList.closest('.lifeos-list-card');
  if (card) {
    card.classList.toggle('is-convo-scoped', !!convoScope);
    // With the group under it, "Skills (0)" says what the no-match block
    // would, and the block would push the conversations off the screen.
    card.classList.toggle('is-convo-on', convoMode !== 'off');
  }
  els.lifeOsConvoScope.hidden = !convoScope;
  if (convoScope) {
    els.lifeOsConvoScopeName.textContent = 'in ' + convoScope.name;
    els.lifeOsConvoScope.setAttribute('aria-label',
      'Showing ' + convoScope.name + ' only. Remove to search every skill');
  }
  syncSkillsHead();
  syncConvoSort();
  syncStartNew();
}

// Start new shows whenever the Conversations group does: browse mode
// (a skill's own list, always narrowed) and search mode, hits or none. It is
// the way out of a search that found nothing. Narrowed to a skill it names
// that skill; spanning every skill (search only) it opens the picker. With the
// group off, the skill row and its kebab already start a new conversation.
function syncStartNew() {
  const btn = els.lifeOsConvoStartNew;
  if (!btn) return;
  btn.hidden = convoMode === 'off';
  if (btn.hidden) return;
  // One name for both cases; the popup hint is the only thing that differs.
  if (convoScope) btn.removeAttribute('aria-haspopup');
  else btn.setAttribute('aria-haspopup', 'dialog');
}

// Start new through the kebab's path (life-os.js's launchSkill), so the
// toolbar's model, Detached and the terminal size apply exactly as there. The
// search text is not sent: it only narrows what the owner is looking for.
function startNewFromSearch() {
  if (convoScope) {
    const skill = (state.lifeOsSkills || []).find(function (s) {
      return s.id === convoScope.id;
    }) || { id: convoScope.id, name: convoScope.name };
    launchSkill(skill);
    return;
  }
  openStartPicker();
}

function openStartPicker() {
  const d = els.lifeOsStartPicker;
  if (!d) return;
  const list = els.lifeOsStartPickerList;
  list.replaceChildren.apply(list, state.lifeOsSkills.slice().sort(compareSkills).map(
    function (s) {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'settings-row lifeos-start-pick';
      row.dataset.id = s.id;
      row.setAttribute('aria-label', 'Start a new conversation with ' + s.name);
      const label = document.createElement('span');
      label.className = 'settings-row-label';
      label.textContent = s.name;
      row.appendChild(skillAvatar(s, false));
      row.appendChild(label);
      row.addEventListener('click', function () {
        d.close();
        launchSkill(s);
      });
      return row;
    }));
  if (!d.open) d.showModal();
}

// "Skills (n)" heads the skill rows only while the Conversations group shows
// under them, so the two groups read as one result. Exported: life-os.js's
// skills-name filter re-syncs the count on every keystroke, since filtering
// the list changes how many rows are shown.
export function syncSkillsHead() {
  const head = els.lifeOsSkillsHead;
  if (!head) return;
  head.hidden = convoMode === 'off' || !!convoScope;
  if (head.hidden) return;
  const shown = els.lifeOsList.querySelectorAll(':scope > li.lifeos-item:not([hidden])').length;
  els.lifeOsSkillsCount.textContent = '(' + shown + ')';
}

// A collapsed Skills card would hide the group it holds.
function revealSkillsCard() {
  const card = els.lifeOsList && els.lifeOsList.closest('details');
  if (card && !card.open) card.open = true;
}

// The group's lifecycle states through the shared helper: a glyph,
// one line, and at most one Retry, never a blank group and never a toast.
const CONVO_STATE_GLYPH = {
  loading: 'hourglass',
  empty: 'messages-square',
  error: 'triangle-alert',
};

function showConvoState(kind, message, retry) {
  // No rows on screen means none to re-sort: a sort change must not bring
  // back the list this state replaced.
  convoSourceRows = [];
  convoRows = [];
  els.lifeOsConvoList.replaceChildren();
  els.lifeOsConvoCount.textContent = '';
  setAsyncState(els.lifeOsConvoState, kind,
    { message: message, glyph: CONVO_STATE_GLYPH[kind], retry: retry });
  syncConvoSort();
}

// One skill's index — the no-query view. `available: false` means the
// indexer simply hasn't digested this skill yet; that's an honest empty
// state, not an error. Resolves to the rows it rendered, or null.
async function loadConvos() {
  const scope = convoScope;
  if (!scope) return null;
  const mine = ++convoSeq;
  setConvoMode('browse');
  loadArchived('', scope.id);
  showConvoState('loading', 'Reading conversations…');
  try {
    const body = await jsonApi(
      '/api/life-os/skills/' + encodeURIComponent(scope.id) + '/conversations'
    );
    if (mine !== convoSeq) return null;     // the field or scope moved on
    if (!body.available) {
      showConvoState('empty', 'No conversation index yet for this skill.');
      return null;
    }
    const rows = body.conversations || [];
    renderConvoRows(rows);
    return rows;
  } catch (exc) {
    if (mine !== convoSeq) return null;
    showConvoState('error', convoFailure(exc), refreshConvos);
    return null;
  }
}

// Ranked search across every skill (or the scope). The server owns the
// ranking; a missing CLI or database comes back as available:false with a
// short reason, which reads as "search unavailable" — never an error toast.
// The skills group above keeps working whatever this says.
async function runConvoSearch(query) {
  const scope = convoScope;
  const mine = ++convoSeq;
  setConvoMode('search');
  loadArchived(query, scope ? scope.id : '');
  showConvoState('loading', 'Searching conversations…');
  let url = '/api/life-os/conversations/search?q=' + encodeURIComponent(query);
  if (scope) url += '&skill=' + encodeURIComponent(scope.id);
  try {
    const body = await jsonApi(url);
    if (mine !== convoSeq) return null;
    if (!body.available) {
      showConvoState('error', 'Search unavailable — ' + (body.reason || 'try again later.'));
      return null;
    }
    const rows = body.results || [];
    if (!rows.length) {
      showConvoState('empty', 'Nothing matched “' + query + '”.');
      return null;
    }
    renderConvoRows(rows);
    return rows;
  } catch (exc) {
    if (mine !== convoSeq) return null;
    showConvoState('error', convoFailure(exc), refreshConvos);
    return null;
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

// A skill's ⋮ Conversations: the field, emptied, narrowed to that skill, its
// list newest first. Resolves to the rows once loaded (null otherwise),
// which is what a ?convo= link resolves against. `quiet` (the link) leaves
// the keyboard down.
export function openConvos(skill, opts) {
  if (!els.lifeOsConvoGroup || !skill) return Promise.resolve(null);
  convoScope = { id: skill.id, name: skill.name };
  revealSkillsCard();
  // set() goes through the field's input event, which schedules a debounced
  // refresh; refreshConvos() runs it now and drops that timer.
  skillsFilter.set('');
  const loaded = refreshConvos();
  if (!(opts && opts.quiet)) els.lifeOsFilterInput.focus();
  return loaded;
}

// A ?convo= link for a skill that is gone: say so in the group.
function showConvoNotice(message) {
  revealSkillsCard();
  convoSeq += 1;
  convoScope = null;
  setConvoMode('notice');
  showConvoState('error', message);
}

// ------------------------------------------------- sort
//
// Orderings over rows already on the client, so a change costs no round
// trip. The browse list and the search results land here with *different*
// defaults: browsing defaults to a date sort, while a search
// defaults to 'relevance' — the server's FTS5 rank order, which the client's
// only job is not to destroy.
//
// 'interaction' (Recent) is the browse default, because
// `last_interaction` is the capture file's mtime, which life-os moves
// forward whenever a resumed session appends turns, so the conversation you
// were last in floats to the top. 'created' is the date-stamped filename
// order the index is written in.

const CONVO_SORTS = ['relevance', 'interaction', 'created'];

// The ordering in force for whatever is on screen right now.
function activeConvoSort() {
  return convoMode === 'search' ? convoSearchSort : state.lifeOsConvoSort;
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

// The segmented control shows while the group lists conversations (it stays
// through a search's loading, so the group never jumps), Relevance only while
// searching, and the segment in force pressed.
function syncConvoSort() {
  const nav = els.lifeOsConvoSort;
  if (!nav) return;
  const mode = activeConvoSort();
  nav.hidden = convoMode !== 'browse' && convoMode !== 'search';
  nav.dataset.sort = mode;
  nav.querySelectorAll('.segmented-item').forEach(function (btn) {
    btn.hidden = btn.dataset.sort === 'relevance' && convoMode !== 'search';
    const on = btn.dataset.sort === mode;
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
}

function setConvoSort(mode) {
  if (CONVO_SORTS.indexOf(mode) === -1) return;
  if (convoMode === 'search') {
    // Search orderings are transient: the end of the search restores the
    // browse default, so a date ordering of the hits never sticks.
    convoSearchSort = mode;
  } else {
    if (mode === 'relevance') return;   // browsing has no rank to restore
    state.lifeOsConvoSort = mode;
    localStorage.setItem('launcher.lifeOsConvoSort', state.lifeOsConvoSort);
  }
  // Re-order what is already on screen; a refetch would return the same rows.
  // Re-sorting from the *server's* order, not the rendered one, is what makes
  // relevance reachable again after a date sort.
  if (convoSourceRows.length) renderConvoRows(convoSourceRows);
  else syncConvoSort();
}

function renderConvoRows(rows) {
  setAsyncState(els.lifeOsConvoState, 'ready');
  convoSourceRows = rows.slice();
  convoRows = sortedConvoRows(rows);
  syncConvoSort();
  if (!convoRows.length) {
    showConvoState('empty', 'No conversations yet.');
    return;
  }
  els.lifeOsConvoCount.textContent = '(' + convoRows.length + ')';
  els.lifeOsConvoList.replaceChildren.apply(els.lifeOsConvoList, convoRows.map(convoRow));
}

// What a conversation is called: the owner's title when set, else the
// digest topic, which keeps updating underneath it.
function convoName(r) {
  return r.title || r.topic || r.slug || r.file || 'untitled';
}

// The avatar of the skill a conversation is from: its glyph or monogram,
// the same as on its skill row.
function convoAvatar(r) {
  const skill = (state.lifeOsSkills || []).find(function (s) { return s.id === r.skill; });
  return skillAvatar(skill || { id: r.skill, name: r.skill }, false);
}

// The row's one trailing item: a chevron, because a tap opens a sheet.
function rowChevron(onTap) {
  const el = document.createElement('span');
  el.className = 'lifeos-convo-chevron';
  el.setAttribute('aria-hidden', 'true');
  el.innerHTML = icon('chevron-right');
  el.addEventListener('click', onTap);
  return el;
}

// One conversation on the shared action-row: a tap opens its
// sheet, the title is what it is called (an owner's rename replaces the
// topic outright), and the one context line is when it was last touched (or
// created, under that sort), which skill it is from when the group spans
// several, and the start of its summary.
function convoRow(r) {
  const name = convoName(r);
  const meta = [
    convoWhen(convoDateSort() === 'created' ? r.date : (r.last_interaction || r.date)),
    convoScope ? '' : r.skill,
    convoSummary(r),
  ].filter(Boolean).join(' · ');
  const open = function () { openConvoSheet(r); };
  return actionRow({
    id: (r.skill || '') + '/' + (r.file || ''),
    className: 'lifeos-convo-row',
    title: name,
    meta: meta,
    avatar: convoAvatar(r),
    label: 'Open ' + name,
    onMain: open,
    trailing: rowChevron(open),
  }).li;
}

// A day as people say it: Today, Yesterday, "3 days ago" within the week,
// then the short date (with the year only when it is not this one). The
// index's dates are calendar days (YYYY-MM-DD), read in local time. English
// like the words around it: the browser's own locale (es on the owner's
// phone) printed "1 ago" for 1 August, which reads as "1 [day] ago".
function convoWhen(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || ''));
  if (!m) return '';
  const day = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  const days = calendarDaysFromToday(day);
  if (days === 0) return 'Today';
  if (days === 1) return 'Yesterday';
  if (days > 1 && days < 7) return days + ' days ago';
  const format = { month: 'short', day: 'numeric' };
  if (day.getFullYear() !== new Date().getFullYear()) format.year = 'numeric';
  return new Intl.DateTimeFormat('en-GB', format).format(day);
}

// The conversation's summary as one plain paragraph. A newer index
// entry carries it whole; an older one has decisions and
// open loops, which read as consecutive sentences with their labels dropped.
// The digest writes a literal "none" for an empty section: that is noise,
// not a sentence.
function convoSummary(r) {
  const clean = function (value) {
    const text = String(value || '').trim();
    return text.toLowerCase() === 'none' ? '' : text;
  };
  const whole = clean(r.summary);
  if (whole) return whole;
  return [clean(r.decisions), clean(r.open_loops)].filter(Boolean)
    .map(function (text) { return /[.!?…]$/.test(text) ? text : text + '.'; })
    .join(' ');
}

// The conversation the sheet shows, or null while it is closed.
let sheetRow = null;

// Resume reattaches the stored session on the conversation's own model,
// so the toolbar's provider does not have to match it: there is no
// "select a Claude model" lockout. The index carries no model, so the
// button cannot name one ahead of time; the toast after it does.
function canResume(r) {
  return !!(r.resumable && r.skill);
}

// The harness a "Start new in …" would hand off to: the toolbar's, when it
// is not the conversation's own and the capture can seed one. '' when no
// handoff applies.
function handoffTarget(r) {
  const target = lifeOsModel().split(':')[0];
  if (!r.handoff_available || !r.skill || target === r.agent) return '';
  if (target === 'codex') return 'Codex';
  if (target === 'claude') return 'Claude';
  return '';
}

function convoSheetMeta(r) {
  const turns = Number(r.turns) || 0;
  return [
    r.skill,
    convoWhen(r.last_interaction || r.date),
    turns ? turns + (turns === 1 ? ' message' : ' messages') : '',
    r.agent ? 'from ' + nameLabel(r.agent) : '',
  ].filter(Boolean).join(' · ');
}

function sheetLink(cls) {
  return els.lifeOsConvoSheet.querySelector('.' + cls);
}

function fillConvoSheet() {
  const r = sheetRow;
  if (!r) return;
  els.lifeOsConvoSheetTitle.textContent = convoName(r);
  els.lifeOsConvoSheetTitle.title = convoName(r);
  els.lifeOsConvoSheetMeta.textContent = convoSheetMeta(r);
  const summary = convoSummary(r);
  els.lifeOsConvoSheetSummary.textContent = summary;
  els.lifeOsConvoSheetSummary.hidden = !summary;
  // A Resume that cannot run stays, disabled, and the note says why: a
  // phone has no hover for a tooltip.
  const resumable = canResume(r);
  els.lifeOsConvoSheetResume.disabled = !resumable;
  els.lifeOsConvoSheetNote.textContent = resumable ? ''
    : (r.resume_reason || 'No stored session; readable only.');
  els.lifeOsConvoSheetNote.hidden = resumable;
  const handoff = handoffTarget(r);
  const handoffRow = sheetLink('lifeos-convo-handoff');
  handoffRow.hidden = !handoff;
  handoffRow.querySelector('.settings-row-label').textContent = 'Start new in ' + handoff;
  // A row with no readable capture has nothing to read, open or change.
  const editable = !!(r.path && r.skill);
  sheetLink('lifeos-convo-read').hidden = !r.path;
  sheetLink('lifeos-convo-open-raw').hidden = !r.path;
  sheetLink('lifeos-convo-rename').hidden = !editable;
  sheetLink('lifeos-convo-copy-link').hidden = !(r.skill && r.file);
  sheetLink('lifeos-convo-delete').parentElement.hidden = !editable;
}

function openConvoSheet(r) {
  const d = els.lifeOsConvoSheet;
  if (!d) return;
  sheetRow = r;
  fillConvoSheet();
  if (!d.open) d.showModal();
}

function closeConvoSheet() {
  if (els.lifeOsConvoSheet && els.lifeOsConvoSheet.open) els.lifeOsConvoSheet.close();
}

// Everything a conversation stacked: the raw capture, the reader, the sheet.
function closeConvoLayers() {
  closeDoc();
  closeConvoViewer();
  closeConvoSheet();
}

// Called from life-os-content.js when a log file is deleted or renamed: if
// the open sheet is showing that exact capture, nothing is left for it (or
// anything stacked over it) to act on.
export function closeConvoLayersForPath(path) {
  if (sheetRow && sheetRow.path === path) closeConvoLayers();
}

// The launcher link that reopens one capture: main.js boot reads
// ?convo=<skill>/<file> and hands it to openConvoByLink. The skill id and the
// capture's filename, not its vault path — the server already resolves a
// file inside that skill's conversations/ folder.
function convoLinkUrl(r) {
  return window.location.origin + window.location.pathname + '?convo=' +
    encodeURIComponent(r.skill) + '/' + encodeURIComponent(r.file);
}

function copyConvoLink(r) {
  // iOS only allows a clipboard write inside the tap gesture: writeText is
  // called synchronously from the click handler, never after an await.
  try {
    navigator.clipboard.writeText(convoLinkUrl(r)).then(
      function () {
        toast('Launcher link copied (tailnet only)', 'good', { icon: 'link' });
      },
      function (exc) { apiFailToast('Copy link failed', exc); }
    );
  } catch (exc) {
    apiFailToast('Copy link failed', exc);
  }
}

// Open one capture from a ?convo= link: the field narrowed to its
// skill, then the conversation sheet and the reader over it, the same
// layering a tap builds. The sheet needs the full row, so the link resolves
// through the skill's index; anything that doesn't resolve says so in the
// Conversations group.
export async function openConvoByLink(skillId, file) {
  const skill = (state.lifeOsSkills || []).find(function (s) {
    return s.id === skillId;
  });
  if (!skill) {
    showConvoNotice(
      'This link is for the skill “' + skillId + '”, which Life OS no longer lists.');
    return;
  }
  const rows = await openConvos(skill, { quiet: true });
  if (!rows || !convoScope || convoScope.id !== skill.id) return;
  const row = rows.find(function (r) { return r.file === file; });
  if (!row) {
    showConvoState('error',
      'This link no longer matches a conversation — it may have been renamed or deleted.');
    return;
  }
  openConvoSheet(row);
  if (row.path) openConvoViewer(row, openCapture);
}

// Rename one conversation as the owner sees it: a title kept beside the
// capture, shown in place of the digest topic. Empty clears it. The open
// conversation sheet takes the new name in place.
async function renameConversationTitle(r) {
  const current = r.title || '';
  const typed = await askName(
    current, Object.assign({}, RENAME_TITLE_SHEET, { placeholder: r.topic || '' })
  );
  if (typed === null) return;               // cancelled
  const title = typed.trim().replace(/\s+/g, ' ');
  if (title === current) return;
  let reply;
  try {
    reply = await jsonApi(
      '/api/life-os/skills/' + encodeURIComponent(r.skill) +
        '/conversations?path=' + encodeURIComponent(r.path),
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: title }),
      }
    );
  } catch (exc) {
    apiFailToast('Rename failed', exc);
    return;
  }
  // The rename stands; an open session of this conversation that could not be
  // renamed with it says so rather than quietly keeping the old name.
  const sync = reply && reply.session_sync;
  if (sync && sync.status === 'failed') {
    toast(sync.error, 'error', { icon: 'pencil' });
  } else {
    toast(title ? 'Renamed to ' + title : 'Name cleared', 'good', { icon: 'pencil' });
  }
  r.title = title;
  if (sheetRow === r) fillConvoSheet();
  await refreshAfterLogChange();
}

// Delete one conversation everywhere it lives: the capture, the
// Claude Code transcript it was captured from, and the indexes. One confirm
// that names the conversation and says what goes; a refusal (a conversation
// open in a running session, an unreachable session host) toasts the
// server's reason and changes nothing.
async function deleteConversation(r) {
  const title = convoName(r);
  const ok = await confirmDialog({
    title: 'Delete “' + title + '”?',
    message: 'This removes the saved log, the Claude Code transcript it came ' +
      'from, and its index entries. It cannot be undone.',
    action: 'Delete',
  });
  if (!ok) return;
  try {
    await jsonApi(
      '/api/life-os/skills/' + encodeURIComponent(r.skill) +
        '/conversations?path=' + encodeURIComponent(r.path),
      { method: 'DELETE' }
    );
  } catch (exc) {
    apiFailToast('Delete failed', exc);
    return;
  }
  closeConvoLayers();
  toast('Deleted ' + title, 'good', { icon: 'trash-2' });
  await refreshAfterLogChange();
}

// Read one capture *raw* in the document sheet, over the sheet that asked —
// closing it comes straight back there.
function openCapture(r) {
  loadFile({ path: r.path, name: r.file, category: 'conversations' });
}

// action 'resume' reattaches the stored session on its own model;
// 'handoff' starts a new conversation in the toolbar's harness, seeded from
// the capture, after a confirm.
async function resumeConversation(r, action) {
  const mode = launchDetached() ? 'remote' : 'pty';
  const model = lifeOsModel();
  const payload = {
    mode: mode, model: model, action: action,
    capture: { path: r.path, revision: r.revision, agent: r.agent, sid: r.sid },
  };
  // A resume comes back on the conversation's own model; the
  // toolbar's model applies only to the new conversation a handoff starts.
  if (action === 'resume') payload.keep_model = true;
  if (action === 'handoff') {
    const scope = r.handoff_truncated ?
      'Only the first ' + (r.handoff_limit || 24000).toLocaleString() + ' characters will be included.' :
      'Only this selected capture will be included.';
    const ok = await confirmDialog({
      title: 'Start a new conversation in ' + nameLabel(model.split(':')[0]) + '?',
      message: scope + ' Source provenance is preserved. This does not resume ' +
        'or convert the original session. Memory and knowledge edits still ' +
        'require your approval.',
      action: 'Start new conversation',
      tone: 'primary',
    });
    if (!ok) return;
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
        (r.title || r.topic || r.skill) +
        (action === 'handoff' ? modelTag(model) : resumedModelTag(body)) +
        (body.handoff_truncated ? ' (selected context truncated)' : '') +
        (mode === 'remote' ? ' (detached)' : ''),
      'good', { icon: action === 'handoff' ? 'messages-square' : 'rotate-ccw' }
    );
    // The terminal opens over the page, not in the top layer: the sheets
    // must go first.
    closeConvoLayers();
    handleLaunchResponse(body.session);
  } catch (exc) {
    apiFailToast(action === 'handoff' ? 'New conversation failed' : 'Resume failed', exc);
  }
}

// The sheet's actions read the conversation it shows at tap time, and hold
// it through any await (a ✕ meanwhile clears sheetRow).
function wireConvoSheet() {
  const d = els.lifeOsConvoSheet;
  if (!d) return;
  wireSheet(d, [els.lifeOsConvoSheetClose], function () { sheetRow = null; });
  const on = function (cls, act) {
    sheetLink(cls).addEventListener('click', function () {
      if (sheetRow) act(sheetRow);
    });
  };
  on('lifeos-convo-read', function (r) { openConvoViewer(r, openCapture); });
  on('lifeos-convo-rename', renameConversationTitle);
  on('lifeos-convo-handoff', function (r) { resumeConversation(r, 'handoff'); });
  on('lifeos-convo-copy-link', copyConvoLink);
  on('lifeos-convo-open-raw', openCapture);
  on('lifeos-convo-delete', deleteConversation);
  els.lifeOsConvoSheetResume.addEventListener('click', function () {
    if (sheetRow && canResume(sheetRow)) resumeConversation(sheetRow, 'resume');
  });
}

// ------------------------------------------------------------ archived
// What the nightly zombie cleanup moved into its backup. Whenever the group
// shows a search or a skill's list, the archived entries that search (or
// skill) covers load beside it into a collapsed "Archived (n)" disclosure
// under the list, absent when there are none, never mixed into the live list
// or its count. A row opens the reader on the backup's copy with Restore as
// its one primary; a refusal (a same-name capture or session id has appeared
// since) shows the server's reason and changes nothing.

// Bumped by every archived read and by hiding, so an answer that lands
// after the field or scope moved on is dropped.
let archivedSeq = 0;

function entryUrl(r) {
  return '/api/life-os/archived/' + [r.day, r.skill, r.capture].map(encodeURIComponent).join('/');
}

// The owner's title when the conversation had one, else the file name's
// slug ("2026-09-13-0800-ferry-plans.md" reads "ferry plans").
function archivedName(r) {
  if (r.title) return r.title;
  const slug = String(r.capture || '').replace(/\.md$/, '').replace(/^\d{4}-\d{2}-\d{2}-\d{4}-?/, '');
  return slug.replace(/-/g, ' ') || r.capture || 'untitled';
}

function archivedWhen(r) {
  const when = convoWhen(r.day);
  if (!when) return '';
  return 'Archived ' + (/^(Today|Yesterday)$/.test(when) ? when.toLowerCase() : when);
}

function archivedRow(r, scoped) {
  const name = archivedName(r);
  const open = function () { openArchived(r); };
  return actionRow({
    id: 'archived/' + r.day + '/' + r.skill + '/' + r.capture,
    className: 'lifeos-convo-row lifeos-archived-row',
    title: name,
    meta: [archivedWhen(r), scoped ? '' : r.skill].filter(Boolean).join(' · '),
    avatar: convoAvatar(r),
    label: 'Read archived ' + name,
    onMain: open,
    trailing: rowChevron(open),
  }).li;
}

function openArchived(r) {
  openConvoViewer({ title: archivedName(r), agent: r.agent }, null, {
    url: entryUrl(r) + '/transcript',
    primary: { text: 'Restore', onTap: function (btn) { restore(r, btn); } },
  });
}

async function restore(r, btn) {
  btn.disabled = true;
  btn.textContent = 'Restoring…';
  try {
    await jsonApi(entryUrl(r) + '/restore', { method: 'POST' });
  } catch (exc) {
    btn.disabled = false;
    btn.textContent = 'Restore';
    apiFailToast('Not restored', exc);
    return;
  }
  closeConvoViewer();
  toast('Restored ' + archivedName(r), 'good', { icon: 'rotate-ccw' });
  await refreshAfterLogChange();
}

function hideArchived() {
  archivedSeq += 1;
  if (!els.lifeOsArchived) return;
  els.lifeOsArchived.hidden = true;
  els.lifeOsArchived.open = false;
  els.lifeOsArchivedList.replaceChildren();
}

// The archived entries a search (`query`) or a skill's list (`skillId`)
// covers. Quiet on failure: the group is a side door, and the live list
// beside it says what is wrong with the content endpoints.
async function loadArchived(query, skillId) {
  if (!els.lifeOsArchived || (!query && !skillId)) { hideArchived(); return; }
  const mine = ++archivedSeq;
  let url = '/api/life-os/archived?q=' + encodeURIComponent(query || '');
  if (skillId) url += '&skill=' + encodeURIComponent(skillId);
  let rows;
  try {
    rows = (await jsonApi(url)).archived || [];
  } catch (exc) {
    if (mine === archivedSeq) hideArchived();
    return;
  }
  if (mine !== archivedSeq) return;
  if (!rows.length) { hideArchived(); return; }
  els.lifeOsArchivedCount.textContent = '(' + rows.length + ')';
  els.lifeOsArchivedList.replaceChildren.apply(els.lifeOsArchivedList,
    rows.map(function (r) { return archivedRow(r, !!skillId); }));
  els.lifeOsArchived.hidden = false;
}

// Every DOM wire for the search field, its chip/sort/start-new controls, the
// start picker and the conversation sheet + reader. life-os.js's wireLifeOs
// calls this once, keeping itself the thin entry point.
export function wireConvoSearch() {
  if (els.lifeOsFilterInput && els.lifeOsConvoGroup) {
    els.lifeOsFilterInput.addEventListener('input', onFieldInput);
    els.lifeOsConvoScope.addEventListener('click', function (ev) {
      // Inside the field's <label>: the tap would also focus the box, which
      // is where it should land anyway, so it does that once, here.
      ev.preventDefault();
      convoScope = null;
      refreshConvos();
      els.lifeOsFilterInput.focus();
    });
    els.lifeOsConvoSort.addEventListener('click', function (ev) {
      const tab = ev.target.closest('.segmented-item');
      if (tab) setConvoSort(tab.dataset.sort);
    });
  }
  if (els.lifeOsConvoStartNew) {
    els.lifeOsConvoStartNew.addEventListener('click', startNewFromSearch);
  }
  wireSheet(els.lifeOsStartPicker,
    [els.lifeOsStartPickerClose, els.lifeOsStartPickerDone]);
  wireConvoSheet();
  wireConvoViewer();
}
