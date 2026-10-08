/* Board — the lane controls and the fleet chief's plumbing (issues #245 /
 * #337 / #500 / #547, reshaped by #1436).
 *
 * Split off `board.js` (issue #691, a `/codebase-audit` maintainability
 * finding) the same way `jobs.js` became jobs-row/jobs-dialog/jobs-agenda and
 * `terminal.js` became eight feature-split modules. `board.js` keeps card
 * rendering, the drill-down drawer, one-tap issue-start and the lanes; this
 * module holds:
 *
 * - the lane toolbar's project combo (#337), the kanban's card filter
 *   (`boardRepoFilter` / `matchesRepoFilter`, read by `renderBoard`);
 * - the Backlog lane's "Start with" model combo, which only feeds the issue
 *   rows' Start and YOLO;
 * - the fleet chief's lifecycle and send path (`ensureChief`,
 *   `runChiefAction`, `sendToChief`), used by the Code tab's chief row and
 *   the Board's answer sheet. The Board itself has no chief card or composer
 *   any more (#1436 decision log): the chief lives in its row under Code ›
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

import { els, state } from './state.js';
import { apiFailToast, toast } from './api.js';
import { applyLaunchSizePayload } from './terminal.js';
import { startWorkTimer } from './voice.js';
import { terminalJsonApi } from './webauthn.js';
import { sendSessionMessage } from './sessions.js';
import { isChiefSession, wireModelCombo } from './dom-utils.js';
import { renderBoard } from './board.js';

// ---------------------------------------------------- fleet chief (#245)
// The standing conversational orchestrator: one label="chief" PTY session.
// Server-side plumbing in routers/board.py; the brain is fleet-config's
// /chief skill.

// isChiefCard is the board-card-shaped alias of the shared dom-utils.js
// predicate (#547) — board cards carry the same label/kind/name fields the
// Coding tab's session rows do, so no board-specific logic is needed here.
// Exported so board.js's card rendering reads the same predicate rather than
// re-aliasing it (#691).
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
// else stores an attachment (#1295), as Chat stores one on its session's.
export function chiefSessionId() {
  const chief = findChiefCard();
  return chief && chief.alive ? chief.session_id : '';
}

// Exported (#547) for the Coding tab's chief row (sessions.js): its Start,
// Resume and Restart call this ensure endpoint, as the answer sheet's send
// does through sendToChief below.
// ``resume`` (#633) reattaches the most recent chief conversation instead of
// starting fresh — mutually exclusive with ``fresh`` in practice (the two
// buttons are never both pressed), but the server decides precedence.
//
// No auto-ensure race with the Resume button's !alive-gated visibility
// (#633 review): every call site — the Coding tab's chief row
// Start/Resume/Restart (sessions.js) and the answer sheet's spawn-then-type
// send — fires only from an explicit user action (a click or a Done tap),
// never a background poll or timer. So
// nothing silently spawns a fresh chief out from under a still-visible
// Resume button; the only way to "miss" Resume is to deliberately send the
// answer sheet instead, which is an ordinary Start-equivalent choice, not a
// race — the resumable conversation's state row survives untouched either
// way (pruned only after 24h, per _find_resumable_chief_session_id).
//
// `restart` (#1351) is the explicit intent to stop a live chief; `resume`
// alone keeps one, so a stale page's send can never replace it server-side.
export async function ensureChief(fresh, resume, restart) {
  const payload = {};
  if (fresh) payload.fresh = true;
  if (resume) payload.resume = true;
  if (restart) payload.restart = true;
  // Same size contract as every launch (issue #374).
  applyLaunchSizePayload(payload);
  return terminalJsonApi('/api/board/chief/ensure', { method: 'POST', body: payload });
}

// One shape for every chief lifecycle button — the Coding tab's chief row
// Start/Resume/Restart (#828, duplication audit) — disable →
// ensureChief → toast → onDone → re-enable, always in a `finally`. `label`
// drives both the work-timer text (when `useTimer`) and the error toast
// ("Chief <label.toLowerCase()> failed"); `resume` drives the success
// wording ("Chief resumed" / "No resumable conversation — started fresh" vs
// "Chief started" / "Chief already running") and whether the crown icon is
// unconditional (resume/restart) or gated on an actual spawn (start).
// `confirmMessage`, when given, gates the whole action on `confirm()` before
// anything else runs (Restart's are-you-sure). `restart` is passed through to
// ensureChief (the Resume buttons' stop-a-live-chief intent, #1351).
export async function runChiefAction({
  button, label, fresh, resume, restart = false, onDone, useTimer = false,
  confirmMessage,
}) {
  if (confirmMessage && !confirm(confirmMessage)) return;
  button.disabled = true;
  const stopTimer = useTimer ? startWorkTimer(button, label) : null;
  try {
    const body = await ensureChief(fresh, resume, restart);
    const text = resume
      ? (body.resumed ? 'Chief resumed' : 'No resumable conversation — started fresh')
      : (body.spawned ? 'Chief started' : 'Chief already running');
    // Resume/Restart's icon is unconditional (a chat is reachable either
    // way); Start's is gated on an actual spawn — matches the pre-#828 toasts.
    toast(text, 'good', resume || body.spawned ? { icon: 'crown' } : undefined);
    if (onDone) await onDone(body);
  } catch (exc) {
    apiFailToast('Chief ' + label.toLowerCase() + ' failed', exc);
  } finally {
    if (stopTimer) stopTimer();
    button.disabled = false;
  }
}

// ------------------------------------------------------- repo filter (#337)

// Repo/project dropdown (#337) ← the same live claude-code listing the
// Coding tab renders (state.apps). It is a plain tap-to-open/tap-to-select
// dropdown, not a typable field — a button trigger, not an <input>. The
// current selection (or "All projects", the default) lives in the hidden
// #boardDispatchRepo input and filters which cards renderBoard() shows
// in every column via boardRepoFilter()/cardRepoOf() below.
// Re-synced on tab activation and on every board render (so a boot /api/apps
// fetch that lands late still populates it), but the underlying name list is
// only rebuilt when it actually changed — a rebuild mid-browse would
// otherwise reset a dropdown the user has open.
let _repoSig = null;
let _repoNames = [];

const ALL_PROJECTS_LABEL = 'All projects';

function repoListOpen() {
  const list = els.boardDispatchRepoList;
  return !!list && !list.hidden;
}

function repoDisplayLabel(name) {
  return name || ALL_PROJECTS_LABEL;
}

// The trigger holds a folder glyph before its label, so only the label's
// text changes.
function setRepoLabel(name) {
  const label = els.boardDispatchRepoBtn.querySelector('.board-repo-label');
  label.textContent = repoDisplayLabel(name);
}

function renderRepoList() {
  const list = els.boardDispatchRepoList;
  const hidden = els.boardDispatchRepo;
  if (!list) return;
  list.replaceChildren();
  const allLi = document.createElement('li');
  allLi.textContent = ALL_PROJECTS_LABEL;
  allLi.dataset.repo = '';
  allLi.setAttribute('role', 'option');
  allLi.setAttribute('aria-selected', hidden.value === '' ? 'true' : 'false');
  list.appendChild(allLi);
  _repoNames.forEach(function (name) {
    const li = document.createElement('li');
    li.textContent = name;
    li.dataset.repo = name;
    li.setAttribute('role', 'option');
    li.setAttribute('aria-selected', name === hidden.value ? 'true' : 'false');
    list.appendChild(li);
  });
}

function openRepoList() {
  const list = els.boardDispatchRepoList;
  const btn = els.boardDispatchRepoBtn;
  if (!list || !btn) return;
  renderRepoList();
  list.hidden = false;
  btn.setAttribute('aria-expanded', 'true');
}

function closeRepoList() {
  const list = els.boardDispatchRepoList;
  const btn = els.boardDispatchRepoBtn;
  if (!list || !btn) return;
  list.hidden = true;
  btn.setAttribute('aria-expanded', 'false');
}

function selectRepo(name) {
  els.boardDispatchRepo.value = name;
  setRepoLabel(name);
  closeRepoList();
  // The same selection scopes the visible kanban cards (#337) — apply it
  // immediately rather than waiting for the next 5 s poll.
  renderBoard();
}

function syncDispatchRepos() {
  const hidden = els.boardDispatchRepo;
  const btn = els.boardDispatchRepoBtn;
  if (!hidden || !btn) return;
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
  const next = (!current || repos.indexOf(current) >= 0) ? current : '';
  hidden.value = next;
  setRepoLabel(next);
  if (repoListOpen()) renderRepoList();
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
// through the kind-agnostic /input route. The answer sheet (#1295,
// board-answers.js) sends through here. Returns ensure's body.
export async function sendToChief(text) {
  // resume=true (#651): the lazy first-send ensure used to always spawn a
  // blank chief, silently discarding a resumable conversation exactly like
  // Restart did before #649/#650 — this is in fact the most likely path a
  // user takes after a session-host restart, since chat mode reads as
  // conversational and the Start/Resume status row is easy to not notice.
  // Never `restart` (#1351): with a chief alive the text lands in it — a
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
  const btn = els.boardDispatchRepoBtn;
  const list = els.boardDispatchRepoList;
  const combo = btn && btn.closest('.board-repo-combo');
  if (!btn || !list || !combo) return;
  btn.addEventListener('click', function () {
    if (repoListOpen()) closeRepoList(); else openRepoList();
  });
  btn.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeRepoList();
  });
  list.addEventListener('click', function (e) {
    const li = e.target.closest('li[data-repo]');
    if (!li) return;
    selectRepo(li.dataset.repo);
  });
  // Tapping anywhere outside the trigger/list closes it — there's no text
  // focus to lose (it's a button, not an input), so a plain outside-click
  // check is enough; no blur-race handling needed.
  document.addEventListener('click', function (e) {
    if (repoListOpen() && !combo.contains(e.target)) closeRepoList();
  });
}

export function wireDispatch() {
  if (!els.boardDispatchModel) return;
  wireRepoCombo();
  // The Backlog lane's "Start with" (#500 / #869, moved there by #1436): a
  // plain client-side control (issue #355 pattern) — no server config, read
  // when an issue row's Start or YOLO is tapped.
  dispatchModelCombo = wireModelCombo(els.boardDispatchModel, function () {
    els.boardDispatchModel.dispatchEvent(new Event('change'));
  });
  syncDispatchBar();
}
