/* Board — the lane controls and the fleet chief's plumbing.
 *
 * Kept separate from `board.js` the same way `jobs.js` is split into
 * jobs-row/jobs-dialog/jobs-agenda and `terminal.js` into its own
 * feature-split modules. `board.js` keeps card
 * rendering, the drill-down drawer, one-tap issue-start and the lanes; this
 * module holds:
 *
 * - the lane toolbar's project combo, the kanban's card filter
 *   (`boardRepoFilter` / `matchesRepoFilter`, read by `renderBoard`);
 * - the Backlog lane's "Start with" model combo, which only feeds the issue
 *   rows' Start and YOLO;
 * - the fleet chief's lifecycle and send path (`ensureChief`,
 *   `runChiefAction`, `sendToChief`), used by the Code tab's chief row and
 *   the Board's answer sheet. The Board itself has no chief card or composer
 *   any more: the chief lives in its row under Code ›
 *   Sessions, and on the Board only as its session row in Claude's turn,
 *   whose drawer carries the shared composer like every session's.
 *
 * Both combos are static markup `renderBoard()` never touches, so the 5 s
 * poll can't close one mid-pick.
 *
 * `board.js` and this module import each other (this one calls
 * `renderBoard`; `board.js` calls `wireDispatch`/`syncDispatchBar` and the
 * two filter helpers). That mirrors the existing, working sessions.js <->
 * terminal.js cycle — every cross-module call happens inside a function body,
 * never at module-evaluation time.
 */

import { els, state } from '../state.js';
import { applyLaunchSizePayload } from '../shared/launch-size.js';
import { SensitiveOutcome, runSensitiveAction } from '../shared/sensitive-action.js';
import { startWorkTimer } from '../voice.js';
import { terminalJsonApi } from '../webauthn.js';
import { sendSessionMessage } from '../sessions.js';
import { wireModelCombo } from '../shared/model-combo.js';
import { isChiefSession } from '../shared/session-kinds.js';
import { confirmDialog } from '../confirm-dialog.js';
import { renderBoard } from '../board.js';

// ---------------------------------------------------- fleet chief
// The standing conversational orchestrator: one label="chief" PTY session.
// Server-side plumbing in routers/board.py; the brain is fleet-config's
// /chief skill.

// isChiefCard is the board-card-shaped alias of the shared session-kinds.js
// predicate — board cards carry the same label/kind/name fields the
// Coding tab's session rows do, so no board-specific logic is needed here.
// Exported so board.js's card rendering reads the same predicate rather than
// re-aliasing it.
export const isChiefCard = isChiefSession;

let dispatchModelCombo = null;

export function setBoardDispatchModelOptions(items) {
  if (dispatchModelCombo) dispatchModelCombo.setOptions(items);
}

export function getBoardDispatchModel() {
  return (dispatchModelCombo && dispatchModelCombo.getValue()) || 'claude:sonnet';
}

function findChiefCard() {
  const columns = (state.board && state.board.columns) || {};
  let found = null;
  Object.keys(columns).some(function (key) {
    return (columns[key] || []).some(function (c) {
      if (isChiefCard(c)) { found = c; return true; }
      return false;
    });
  });
  return found;
}

// The running chief's session id, or '' — where the answer sheet's Anything
// else stores an attachment, as Chat stores one on its session's.
export function chiefSessionId() {
  const chief = findChiefCard();
  return chief && chief.alive ? chief.session_id : '';
}

// Exported for the Coding tab's chief row (sessions.js): its Start,
// Resume and Restart call this ensure endpoint, as the answer sheet's send
// does through sendToChief below.
// ``resume`` reattaches the most recent chief conversation instead of
// starting fresh — mutually exclusive with ``fresh`` in practice (the two
// buttons are never both pressed), but the server decides precedence.
//
// No auto-ensure race with the Resume button's !alive-gated visibility:
// every call site — the Coding tab's chief row
// Start/Resume/Restart (sessions.js) and the answer sheet's spawn-then-type
// send — fires only from an explicit user action (a click or a Done tap),
// never a background poll or timer. So
// nothing silently spawns a fresh chief out from under a still-visible
// Resume button; the only way to "miss" Resume is to deliberately send the
// answer sheet instead, which is an ordinary Start-equivalent choice, not a
// race — the resumable conversation's state row survives untouched either
// way (pruned only after 24h, per _find_resumable_chief_session_id).
//
// `restart` is the explicit intent to stop a live chief; `resume`
// alone keeps one, so a stale page's send can never replace it server-side.
export async function ensureChief(fresh, resume, restart) {
  const payload = {};
  if (fresh) payload.fresh = true;
  if (resume) payload.resume = true;
  if (restart) payload.restart = true;
  // Same size contract as every launch.
  applyLaunchSizePayload(payload);
  return terminalJsonApi('/api/board/chief/ensure', { method: 'POST', body: payload });
}

// One shape for every chief lifecycle button — the Coding tab's chief row
// Start/Resume/Restart — disable →
// ensureChief → toast → onDone → re-enable, always in a `finally`. `label`
// drives both the work-timer text (when `useTimer`) and the error toast
// ("Chief <label.toLowerCase()> failed"); `resume` drives the success
// wording ("Chief resumed" / "No resumable conversation — started fresh" vs
// "Chief started" / "Chief already running") and whether the crown icon is
// unconditional (resume/restart) or gated on an actual spawn (start).
// `confirmMessage`, when given, gates the whole action on the themed
// confirm (its { title, message, action }) before anything else runs
// (Restart's are-you-sure). `restart` is passed through to
// ensureChief (the Resume buttons' stop-a-live-chief intent).
export async function runChiefAction({
  button, label, fresh, resume, restart = false, onDone, useTimer = false,
  confirmMessage,
}) {
  if (confirmMessage && !(await confirmDialog(confirmMessage))) return;
  button.disabled = true;
  const stopTimer = useTimer ? startWorkTimer(button, label) : null;
  try {
    const verdict = await runSensitiveAction({
      key: 'chief',
      pending: 'Chief ' + label.toLowerCase() + '…',
      icon: 'crown',
      run: function () { return ensureChief(fresh, resume, restart); },
      done: function (body) {
        const text = resume
          ? (body.resumed ? 'Chief resumed' : 'No resumable conversation — started fresh')
          : (body.spawned ? 'Chief started' : 'Chief already running');
        // Resume/Restart's icon is unconditional (a chat is reachable either
        // way); Start's is gated on an actual spawn.
        return { text: text, icon: resume || body.spawned ? 'crown' : undefined };
      },
      failPrefix: 'Chief ' + label.toLowerCase() + ' failed',
      unconfirmed: 'Chief ' + label.toLowerCase() + ' not confirmed — check the session list before trying again.',
    });
    if (verdict.outcome === SensitiveOutcome.DONE && onDone) await onDone(verdict.result);
  } finally {
    if (stopTimer) stopTimer();
    button.disabled = false;
  }
}

// ------------------------------------------------------- repo filter

// Repo/project filter ← the same live claude-code listing the Coding tab
// renders (state.apps). It is a type-to-filter combobox: typing narrows the
// options by name (case-insensitive substring), Enter or a tap commits one,
// Escape drops the typed text. "All projects" is always the first option and
// the default. The committed selection lives in the hidden #boardDispatchRepo
// input (the visible text field only mirrors it, or holds uncommitted typing)
// and filters which cards renderBoard() shows in every column via
// boardRepoFilter()/cardRepoOf() below.
// Re-synced on tab activation and on every board render (so a boot /api/apps
// fetch that lands late still populates it), but the underlying name list is
// only rebuilt when it actually changed — a rebuild mid-browse would
// otherwise reset a list the user has open.
let _repoSig = null;
let _repoNames = [];
let _repoQuery = '';
let _repoActive = -1;

const ALL_PROJECTS_LABEL = 'All projects';

function repoListOpen() {
  const list = els.boardDispatchRepoList;
  return !!list && !list.hidden;
}

// The options the list shows for the typed query: "All projects" first, then
// the names that contain the query.
function repoOptions() {
  const q = _repoQuery.toLowerCase();
  const names = q
    ? _repoNames.filter(function (name) { return name.toLowerCase().indexOf(q) >= 0; })
    : _repoNames;
  return [''].concat(names);
}

// The text field mirrors the committed pick ("" for All projects, shown as the
// placeholder); the ✕ shows only while the field holds text.
function syncRepoField() {
  els.boardDispatchRepoInput.value = els.boardDispatchRepo.value;
  syncRepoClear();
}

function syncRepoClear() {
  els.boardDispatchRepoClear.hidden = els.boardDispatchRepoInput.value === '';
}

function setRepoActive(index) {
  const list = els.boardDispatchRepoList;
  const input = els.boardDispatchRepoInput;
  _repoActive = index;
  let activeId = '';
  Array.prototype.forEach.call(list.querySelectorAll('li[data-repo]'), function (li, i) {
    if (i === index) {
      li.dataset.active = 'true';
      activeId = li.id;
      li.scrollIntoView({ block: 'nearest' });
    } else {
      delete li.dataset.active;
    }
  });
  if (activeId) input.setAttribute('aria-activedescendant', activeId);
  else input.removeAttribute('aria-activedescendant');
}

function renderRepoList() {
  const list = els.boardDispatchRepoList;
  const hidden = els.boardDispatchRepo;
  if (!list) return;
  list.replaceChildren();
  const options = repoOptions();
  options.forEach(function (name, i) {
    const li = document.createElement('li');
    li.id = 'boardDispatchRepoOpt' + i;
    li.dataset.track = 'board.dispatch-repo-option';
    li.textContent = name || ALL_PROJECTS_LABEL;
    li.dataset.repo = name;
    li.setAttribute('role', 'option');
    li.setAttribute('aria-selected', name === hidden.value ? 'true' : 'false');
    list.appendChild(li);
  });
  if (_repoQuery && options.length === 1) {
    const li = document.createElement('li');
    li.className = 'board-repo-empty';
    li.setAttribute('role', 'presentation');
    li.textContent = 'No project matches';
    list.appendChild(li);
  }
  setRepoActive(_repoActive < options.length ? _repoActive : -1);
}

function showRepoList() {
  const list = els.boardDispatchRepoList;
  if (!list) return;
  renderRepoList();
  list.hidden = false;
  els.boardDispatchRepoInput.setAttribute('aria-expanded', 'true');
}

function openRepoList() {
  _repoQuery = '';
  _repoActive = -1;
  showRepoList();
}

// Closing drops any uncommitted typing: the field goes back to the committed pick.
function closeRepoList() {
  const list = els.boardDispatchRepoList;
  if (!list) return;
  list.hidden = true;
  els.boardDispatchRepoInput.setAttribute('aria-expanded', 'false');
  els.boardDispatchRepoInput.removeAttribute('aria-activedescendant');
  _repoQuery = '';
  _repoActive = -1;
  syncRepoField();
}

function selectRepo(name) {
  els.boardDispatchRepo.value = name;
  closeRepoList();
  // The same selection scopes the visible kanban cards — apply it
  // immediately rather than waiting for the next 5 s poll.
  renderBoard();
}

function syncDispatchRepos() {
  const hidden = els.boardDispatchRepo;
  if (!hidden || !els.boardDispatchRepoInput) return;
  const repos = (state.apps || [])
    .filter(function (a) { return a.kind === 'claude-code'; })
    .map(function (a) { return String(a.name); });
  const sig = repos.join('\n');
  if (sig === _repoSig) return;
  _repoSig = sig;
  _repoNames = repos;
  // '' ("All projects") is always a valid selection — only reset a specific
  // repo pick back to "All" if that repo dropped out of the live list.
  const current = hidden.value;
  hidden.value = (!current || repos.indexOf(current) >= 0) ? current : '';
  if (repoListOpen()) renderRepoList();
  else syncRepoField();
}

// The repo/project identity of a card, whatever kind it is — issue/PR/done
// cards carry `repo`, live session cards carry `project`. Job cards carry
// neither (they aren't tied to a coding project) and are hidden by a
// specific-project filter, same as any other non-matching card.
function cardRepoOf(card) {
  return card.repo || card.project || null;
}

export function boardRepoFilter() {
  return els.boardDispatchRepo ? els.boardDispatchRepo.value : '';
}

export function matchesRepoFilter(card, filter) {
  if (!filter) return true;
  const repo = cardRepoOf(card);
  return !!repo && String(repo).toLowerCase() === String(filter).toLowerCase();
}

// The one path a message takes into the chief's PTY: ensure, then type it
// through the kind-agnostic /input route. The answer sheet
// (board-answers.js) sends through here. Returns ensure's body.
export async function sendToChief(text) {
  // resume=true: the lazy first-send ensure used to always spawn a
  // blank chief, silently discarding a resumable conversation exactly like
  // Restart once did — this is in fact the most likely path a
  // user takes after a session-host restart, since chat mode reads as
  // conversational and the Start/Resume status row is easy to not notice.
  // Never `restart`: with a chief alive the text lands in it — a
  // restart here would drop the in-flight turn of the chief being answered.
  const ensured = await ensureChief(false, true);
  await sendSessionMessage(ensured.session_id, text);
  return ensured;
}

// Keep the project filter's list in step with a project list that may land
// after the first render (/api/apps).
export function syncDispatchBar() {
  syncDispatchRepos();
}

function wireRepoCombo() {
  const input = els.boardDispatchRepoInput;
  const clear = els.boardDispatchRepoClear;
  const list = els.boardDispatchRepoList;
  const combo = input && input.closest('.board-repo-combo');
  if (!input || !clear || !list || !combo) return;
  // A tap on the list or ✕ must not pull focus off the field: that would fire
  // the focus-out below before the click lands.
  [list, clear].forEach(function (el) {
    el.addEventListener('mousedown', function (e) { e.preventDefault(); });
  });
  // Focusing selects the pick, so typing replaces it; the mouseup that ends
  // the focusing click would otherwise collapse that selection.
  let justFocused = false;
  input.addEventListener('focus', function () {
    justFocused = true;
    input.select();
    if (!repoListOpen()) openRepoList();
  });
  input.addEventListener('mouseup', function (e) {
    if (justFocused) e.preventDefault();
    justFocused = false;
  });
  input.addEventListener('click', function () {
    if (!repoListOpen()) openRepoList();
  });
  input.addEventListener('input', function () {
    _repoQuery = input.value.trim();
    // Enter alone takes the first match.
    _repoActive = _repoQuery && repoOptions().length > 1 ? 1 : -1;
    syncRepoClear();
    showRepoList();
  });
  input.addEventListener('keydown', function (e) {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (!repoListOpen()) openRepoList();
      const count = repoOptions().length;
      const step = e.key === 'ArrowDown' ? 1 : -1;
      setRepoActive(_repoActive < 0 && step < 0 ? count - 1 : (_repoActive + step + count) % count);
    } else if (e.key === 'Enter') {
      if (!repoListOpen() || _repoActive < 0) return;
      e.preventDefault();
      selectRepo(repoOptions()[_repoActive]);
    } else if (e.key === 'Escape') {
      if (!repoListOpen()) return;
      e.preventDefault();
      closeRepoList();
    } else if (e.key === 'Tab') {
      closeRepoList();
    }
  });
  clear.addEventListener('click', function () {
    if (els.boardDispatchRepo.value !== '') selectRepo('');
    input.value = '';
    syncRepoClear();
    input.focus();
    openRepoList();
  });
  list.addEventListener('click', function (e) {
    const li = e.target.closest('li[data-repo]');
    if (!li) return;
    selectRepo(li.dataset.repo);
    // Drop the on-screen keyboard once a project is chosen.
    input.blur();
  });
  // Leaving the field by keyboard or by tapping elsewhere closes the list.
  combo.addEventListener('focusout', function (e) {
    if (!combo.contains(e.relatedTarget)) closeRepoList();
  });
  document.addEventListener('click', function (e) {
    if (repoListOpen() && !combo.contains(e.target)) closeRepoList();
  });
}

export function wireDispatch() {
  if (!els.boardDispatchModel) return;
  wireRepoCombo();
  // The Backlog lane's "Start with": a
  // plain client-side control — no server config, read
  // when an issue row's Start or YOLO is tapped.
  dispatchModelCombo = wireModelCombo(els.boardDispatchModel, function () {
    els.boardDispatchModel.dispatchEvent(new Event('change'));
  });
  syncDispatchBar();
}
