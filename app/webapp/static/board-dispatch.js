/* Board dispatch bar + fleet chief (issues #302 / #245 / #337 / #500 / #547).
 *
 * Split off `board.js` (issue #691, a `/codebase-audit` maintainability
 * finding) the same way `jobs.js` became jobs-row/jobs-dialog/jobs-agenda and
 * `terminal.js` became eight feature-split modules. `board.js` keeps card
 * rendering, the drill-down drawer, one-tap issue-start and the column
 * carousel; everything the *bar above the columns* owns lives here:
 *
 * - the repo/project combo (#337), which doubles as the kanban's card filter
 *   (`boardRepoFilter` / `matchesRepoFilter`, read by `renderBoard`);
 * - free-text dispatch (#302) — goal → a fresh /issue-add | /issue-add now |
 *   /issue-yolo session via POST /api/board/dispatch (spawn-then-type
 *   server-side, so free text never touches a spawn command line);
 * - chat mode (#245/#547) — the same bar rerouted into the standing fleet
 *   chief's PTY, plus the chief's Start/Resume/Restart status row and the
 *   chief settings dialog.
 *
 * The bar is static markup `renderBoard()` never touches, so the 5 s poll
 * can't wipe a goal being typed. Dictation mics (shared voice.js) mount on
 * the goal box here; the drawer reply boxes mount theirs in `board.js`.
 *
 * `board.js` and this module import each other (this one calls `renderBoard`
 * and `fetchBoard`; `board.js` calls `wireDispatch`/`syncDispatchBar` and the
 * two filter helpers). That mirrors the existing, working sessions.js <->
 * terminal.js cycle — every cross-module call happens inside a function body,
 * never at module-evaluation time.
 */

import { els, state } from './state.js';
import { apiFailToast, authHeaders, jsonApi, toast } from './api.js';
import { applyLaunchSizePayload } from './terminal.js';
import { createDictation, startWorkTimer, voiceDictationAvailable } from './voice.js';
import { icon } from './_vendored/icons/icons.js';
import { ensureTerminalToken } from './webauthn.js';
import { CHIEF_RESTART_CONFIRM, isChiefSession } from './dom-utils.js';
import { fetchBoard, renderBoard } from './board.js';

// ---------------------------------------------------- fleet chief (#245)
// The standing conversational orchestrator: one label="chief" PTY session
// the chat dispatch mode talks to. Server-side plumbing in routers/board.py;
// the brain is fleet-config's /chief skill.

// isChiefCard is the board-card-shaped alias of the shared dom-utils.js
// predicate (#547) — board cards carry the same label/kind/name fields the
// Coding tab's session rows do, so no board-specific logic is needed here.
// Exported so board.js's card rendering reads the same predicate as the
// chief status row rather than re-aliasing it (#691).
export const isChiefCard = isChiefSession;

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

// Exported (#547) so the Coding tab's manual Start-chief affordance
// (sessions.js) can call the same ensure endpoint the Board's chat mode
// uses.
// ``resume`` (#633) reattaches the most recent chief conversation instead of
// starting fresh — mutually exclusive with ``fresh`` in practice (the two
// buttons are never both pressed), but the server decides precedence.
//
// No auto-ensure race with the Resume button's !alive-gated visibility
// (#633 review): every call site — this Board row's Start/Restart/Resume
// (below), the Coding tab's Start (sessions.js), and dispatchChat's
// spawn-then-type on first chat send — fires only from an explicit user
// action (a click or a Send tap), never a background poll or timer. So
// nothing silently spawns a fresh chief out from under a still-visible
// Resume button; the only way to "miss" Resume is to deliberately type a
// chat message instead, which is an ordinary Start-equivalent choice, not a
// race — the resumable conversation's state row survives untouched either
// way (pruned only after 24h, per _find_resumable_chief_session_id).
export async function ensureChief(fresh, resume) {
  const tt = await ensureTerminalToken();
  const payload = {};
  if (fresh) payload.fresh = true;
  if (resume) payload.resume = true;
  // Same size contract as every launch (issue #374).
  applyLaunchSizePayload(payload);
  return jsonApi('/api/board/chief/ensure', {
    method: 'POST',
    headers: authHeaders({ terminalToken: tt, contentType: 'application/json' }),
    body: JSON.stringify(payload),
  });
}

// One shape for every chief lifecycle button — Board's Start/Resume/Restart
// and the Coding tab's Start/Resume (#828, duplication audit) — disable →
// ensureChief → toast → onDone → re-enable, always in a `finally`. `label`
// drives both the work-timer text (when `useTimer`) and the error toast
// ("Chief <label.toLowerCase()> failed"); `resume` drives the success
// wording ("Chief resumed" / "No resumable conversation — started fresh" vs
// "Chief started" / "Chief already running") and whether the crown icon is
// unconditional (resume/restart) or gated on an actual spawn (start).
// `confirmMessage`, when given, gates the whole action on `confirm()` before
// anything else runs (Restart's are-you-sure).
export async function runChiefAction({
  button, label, fresh, resume, onDone, useTimer = false, confirmMessage,
}) {
  if (confirmMessage && !confirm(confirmMessage)) return;
  button.disabled = true;
  const stopTimer = useTimer ? startWorkTimer(button, label) : null;
  try {
    const body = await ensureChief(fresh, resume);
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

// -------------------------------------------------------- dispatch (#302)

let dispatchMode = 'add';

// Repo/project dropdown (#337) ← the same live claude-code listing the
// Coding tab renders (state.apps). It is a plain tap-to-open/tap-to-select
// dropdown, not a typable field — a button trigger, not an <input>. It does
// double duty: the real dispatch target lives in the hidden
// #boardDispatchRepo input dispatchGoal() reads, AND the current selection
// (or "All projects", the default) filters which cards renderBoard() shows
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
  els.boardDispatchRepoBtn.textContent = repoDisplayLabel(name);
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
  btn.textContent = repoDisplayLabel(next);
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

const DISPATCH_PLACEHOLDER =
  'Speak or type a goal — send dispatches an /issue-* session.';
const CHAT_PLACEHOLDER =
  'Ask the chief — questions answer, directions dispatch.';

function setDispatchMode(mode) {
  dispatchMode = mode;
  // The select itself already shows the chosen value (#547 collapsed the
  // old 4-segment radiogroup into a single <select>) — nothing else to
  // paint here beyond the mode-dependent chat UI.
  syncChatModeUi();
}

// Chat mode (#245): the same bar, rerouted — text goes to the standing
// chief's PTY instead of a fresh /issue-* session. Toggling off restores
// the one-shot dispatch exactly (the mode governs only the send path).
function syncChatModeUi() {
  const chat = dispatchMode === 'chat';
  els.boardDispatchGoal.placeholder = chat ? CHAT_PLACEHOLDER : DISPATCH_PLACEHOLDER;
  if (els.boardDispatchModel) {
    // The chief's model is owned by chief settings, not the per-dispatch
    // selector — grey it out so the bar doesn't suggest otherwise.
    els.boardDispatchModel.disabled = chat;
  }
  renderChiefStatus();
}

function renderChiefStatus() {
  const row = els.boardChiefStatus;
  if (!row) return;
  const chat = dispatchMode === 'chat';
  row.hidden = !chat;
  if (!chat) return;
  const chief = findChiefCard();
  const alive = !!(chief && chief.alive);
  els.boardChiefStart.hidden = alive;
  els.boardChiefResume.hidden = alive;
  els.boardChiefRestart.hidden = !alive;
  els.boardChiefStatusText.textContent = alive
    ? 'chief: ' + (chief.status || 'idle')
    : 'chief: not running';
}

async function dispatchChat() {
  const text = els.boardDispatchGoal.value.trim();
  if (!text) {
    toast('Type or dictate a message first', 'error');
    return;
  }
  const btn = els.boardDispatchSend;
  btn.disabled = true;
  // A first message may spawn the chief (spawn-then-type server-side), so
  // this legitimately takes seconds — tick like dispatchGoal does.
  const stopTimer = startWorkTimer(btn, icon('send-horizontal'));
  try {
    // resume=true (#651): the lazy first-send ensure used to always spawn a
    // blank chief, silently discarding a resumable conversation exactly like
    // Restart did before #649/#650 — this is in fact the most likely path a
    // user takes after a session-host restart, since chat mode reads as
    // conversational and the Start/Resume status row is easy to not notice.
    const ensured = await ensureChief(false, true);
    const sid = ensured.session_id;
    const tt = await ensureTerminalToken();
    await jsonApi(
      '/api/claude-code/sessions/' + encodeURIComponent(sid) + '/input',
      {
        method: 'POST',
        headers: authHeaders({ terminalToken: tt, contentType: 'application/json' }),
        body: JSON.stringify({ data: text, submit: true }),
      }
    );
    // Conversation semantics: unlike dispatch's keep-for-multi-dispatch,
    // a sent chat message clears — the reply is the next thing you want.
    els.boardDispatchGoal.value = '';
    if (ensured.spawned) {
      toast(
        ensured.resumed
          ? 'Chief resumed — first reply may take a moment'
          : 'Chief spawned — first reply may take a moment',
        'good', { icon: 'crown' },
      );
    }
    // Open the chief's drawer so the reply lands somewhere visible. The
    // card is already in /api/board (live sessions fold in before hook
    // state exists); fetch once with no drawer open, then expand.
    state.boardExpanded = null;
    await fetchBoard().catch(function () {});
    state.boardExpanded = sid;
    renderBoard();
  } catch (exc) {
    apiFailToast('Chief message failed', exc);
  } finally {
    stopTimer();
    btn.disabled = false;
  }
}

// ---- chief settings dialog (#245): GET on open, PUT on Save. The dialog
// is the modal contract's editor shape (rename-dialog base). #616 retired
// the daily-respawn setting (fleet-config#442/#449 shipped compact-and-
// continue, making an unattended respawn actively harmful to a live batch)
// — model and worker cap are all that's left to edit here.

async function openChiefSettings() {
  try {
    const tt = await ensureTerminalToken();
    const body = await jsonApi('/api/board/chief/settings', {
      headers: authHeaders({ terminalToken: tt }),
    });
    const s = body.settings || {};
    els.chiefModelSelect.value = s.model || 'fable';
    els.chiefWorkerCap.value = String(s.worker_cap || 3);
    els.chiefSettingsDialog.showModal();
  } catch (exc) {
    apiFailToast('Chief settings unavailable', exc);
  }
}

async function saveChiefSettings() {
  try {
    const tt = await ensureTerminalToken();
    await jsonApi('/api/board/chief/settings', {
      method: 'PUT',
      headers: authHeaders({ terminalToken: tt, contentType: 'application/json' }),
      body: JSON.stringify({
        model: els.chiefModelSelect.value,
        worker_cap: parseInt(els.chiefWorkerCap.value, 10),
      }),
    });
    els.chiefSettingsDialog.close();
    toast('Chief settings saved', 'good', { icon: 'circle-check' });
  } catch (exc) {
    apiFailToast('Chief settings save failed', exc);
  }
}

function wireChief() {
  if (!els.boardChiefStatus) return;
  const afterEnsure = async function () {
    await fetchBoard().catch(function () {});
    renderChiefStatus();
  };
  els.boardChiefStart.addEventListener('click', function () {
    runChiefAction({
      button: els.boardChiefStart, label: 'Start', fresh: false, resume: false,
      useTimer: true, onDone: afterEnsure,
    });
  });
  els.boardChiefResume.addEventListener('click', function () {
    // resume=true (#633): reattach the most recent chief conversation
    // (direct claude --resume <id>, label declared at spawn) instead of
    // starting fresh — falls back to a fresh spawn server-side when no
    // resumable conversation is found, never a hard failure.
    runChiefAction({
      button: els.boardChiefResume, label: 'Resume', fresh: false, resume: true,
      useTimer: true, onDone: afterEnsure,
    });
  });
  els.boardChiefRestart.addEventListener('click', function () {
    // fresh=true (#617): ensure_chief's own graceful stop-then-respawn —
    // never the session-host (:8446) restart, which would kill every PTY.
    // resume=true (#649): reattach the same conversation by default —
    // "Restart" read as "bring my chief back" while it silently discarded
    // the conversation, and Resume is never reachable while a chief is
    // alive to fall back on. Same resumed/resume_fallback_reason contract
    // as the Resume button, so the toast always says which happened.
    runChiefAction({
      button: els.boardChiefRestart, label: 'Restart', fresh: true, resume: true,
      useTimer: true, onDone: afterEnsure, confirmMessage: CHIEF_RESTART_CONFIRM,
    });
  });
  els.boardChiefSettings.addEventListener('click', openChiefSettings);
  els.chiefSettingsForm.addEventListener('submit', function (e) {
    e.preventDefault();
    saveChiefSettings();
  });
  els.chiefSettingsCancel.addEventListener('click', function () {
    els.chiefSettingsDialog.close();
  });
}

async function dispatchGoal() {
  const goal = els.boardDispatchGoal.value.trim();
  if (!goal) {
    toast('Type or dictate a goal first', 'error');
    return;
  }
  const repo = els.boardDispatchRepo.value;
  if (!repo) {
    toast('No repo to dispatch to', 'error');
    return;
  }
  const btn = els.boardDispatchSend;
  btn.disabled = true;
  // The server waits for the agent's first output before typing the goal
  // in (spawn-then-type), so this call legitimately takes seconds — tick.
  const stopTimer = startWorkTimer(btn, icon('send-horizontal'));
  try {
    const tt = await ensureTerminalToken();
    const payload = {
      repo: repo,
      goal: goal,
      mode: dispatchMode,
      model: (els.boardDispatchModel && els.boardDispatchModel.value) || 'sonnet',
    };
    // Same size contract as startIssue (issue #374).
    applyLaunchSizePayload(payload);
    const body = await jsonApi('/api/board/dispatch', {
      method: 'POST',
      headers: authHeaders({ terminalToken: tt, contentType: 'application/json' }),
      body: JSON.stringify(payload),
    });
    toast((body.launched || dispatchMode) + ' → ' + (body.repo || repo), 'good', { icon: 'rocket' });
    // The goal stays in the bar for rapid multi-dispatch ("create more");
    // ✕ clears it. The new card lands in Claude's turn on the next poll.
    fetchBoard().catch(function () {});
  } catch (exc) {
    apiFailToast('Dispatch failed', exc);
  } finally {
    stopTimer();
    btn.disabled = false;
  }
}

export function syncDispatchBar() {
  syncDispatchRepos();
  if (els.boardDispatchRecord) {
    els.boardDispatchRecord.hidden = !voiceDictationAvailable();
  }
  renderChiefStatus();
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
  if (!els.boardDispatchSend) return;
  wireRepoCombo();
  if (els.boardDispatchMode) {
    els.boardDispatchMode.addEventListener('change', function () {
      setDispatchMode(els.boardDispatchMode.value);
    });
  }
  // The model <select> (#500) is a plain client-side control (issue #355
  // pattern) — no server config, just read at dispatch time above.
  els.boardDispatchSend.addEventListener('click', function () {
    if (dispatchMode === 'chat') dispatchChat();
    else dispatchGoal();
  });
  els.boardDispatchClear.addEventListener('click', function () {
    els.boardDispatchGoal.value = '';
    els.boardDispatchGoal.focus();
  });
  const dictation = createDictation({
    button: els.boardDispatchRecord,
    getTextarea: function () { return els.boardDispatchGoal; },
  });
  els.boardDispatchRecord.addEventListener('click', dictation.toggle);
  wireChief();
  syncDispatchBar();
}
