/* Session transcript — the Chat pane of the session overlay.
 *
 * The whole conversation of one live Coding session, chat-style: typed
 * user prompts and assistant replies expanded, everything else — tool calls
 * with their results, thinking, harness plumbing, sub-agent traffic —
 * folded per run into one quiet step line that opens item by item. Backed by
 * the paginated `/api/claude-code/sessions/{sid}/transcript` endpoint
 * (Tailscale + passkey gated, like the Board drawer's /exchange): the
 * newest page loads first, older pages prepend on "Load older" or when the
 * list is scrolled to its top.
 *
 * The shared composer (composer.js) is mounted under the list for
 * every session kind. ➤ Send always goes through the kind-agnostic
 * /input route, never the terminal's WebSocket — even for a full-control
 * session whose terminal is connected: Chat cannot show the PTY, so the
 * route's verdict (confirmed / unconfirmed / queued, sessions.js
 * sendOutcome) is the only feedback the phone gets, and the session-host
 * applies the same framing and settle protocol the terminal composer does
 * Attach uploads inline and appends the path, which a detached
 * session can take too. The ⌨ keys drive a full-control session's live
 * terminal socket while Chat shows; a detached session has none, so they
 * render disabled. A detached session whose agent was never probed for
 * console input keeps the composer but not ➤ Send.
 *
 * Claude Code's AskUserQuestion renders as a question card, never
 * folded. The pending one answers through its own route (/answer), not
 * /input: the picker needs raw keystrokes, and /input frames text as a
 * paste. That card, the ExitPlanMode card and the plan-picker panel
 * that answers a waiting plan from the terminal's own screen live in
 * transcript-decisions.js, which reads and mutates this module's
 * `view`. Entries become list nodes in transcript-render.js, the
 * renderer the Life OS viewer mounts too, which is handed the open session
 * rather than reading `view`; this module owns paging, live refresh, the
 * outbox cursor, the Edited N files card and the composer.
 *
 * This is one pane of #terminalOverlay, not its own overlay:
 * session-overlay.js opens/closes it and flips the overlay's data-mode;
 * the bar's ⋮ menu (terminal-bar.js) carries the pane's Hide-steps and
 * Reload actions. Switching panes never reloads: the pages and fold state
 * loaded here survive a trip through Terminal mode.
 *
 * ## Live refresh
 *
 * A chat view that is *being looked at* keeps itself current, and nothing
 * else does. The gate is deliberately narrow, because the cost of getting
 * it wrong is the thing the feature was asked to avoid — several session
 * windows open on a PC, each streaming a conversation nobody is reading:
 *
 *   - the overlay is open on THIS session, in chat mode (`data-mode`), and
 *   - `document.visibilityState === 'visible'` (a backgrounded tab or a
 *     locked phone stops entirely), and
 *   - the session is still alive.
 *
 * Fail any of those and the timer is cleared, not merely skipped, so a
 * window sitting on Terminal or an overlay that was closed fetches nothing
 * at all. Each page instance (a PC mirror window is its own) polls
 * only the one conversation its own overlay is showing. This is the one
 * place the terminal's design is deliberately NOT copied: terminal.js keeps
 * its socket warm after the overlay closes (:235) because an idle PTY costs
 * the server nothing and re-opening is instant — but a chat tick is a poll,
 * so a warm one is pure waste.
 *
 * Refresh is *incremental*, never a reload. The transcript route grew a
 * forward cursor (`?after=&size=`) that returns only what the agent has
 * appended, split into two halves:
 *
 *   - `entries` — settled. Appended to the list and never touched again, so
 *     scroll position, open disclosures and read-aloud are undisturbed. A
 *     run of folded entries merges into the trailing step line rather than
 *     starting a new one, or an autonomous stretch would fragment into a
 *     card per tick.
 *   - `pending` — the newest message, which may still be growing (a harness
 *     writes one message as several lines). Re-rendered wholesale each time
 *     it changes, with open disclosures carried across by entry offset.
 *
 * Rebuilding only that tail is what keeps this clear of the Board's lesson
 * a poll that rebuilds the DOM breaks whatever the reader is
 * interacting with.
 *
 * The pane's parts beside this file: chat-outbox.js (outbox files merged in
 * by time), chat-turn-cards.js (the Edited N files card and the reply's
 * file chips), chat-activity.js (the strip's live line and the step in
 * progress) and chat-send.js (sending from the composer and confirming the
 * agent took it). They read the open pane through this module's `view`.
 */

import { els, state } from '../state.js';
import { apiFailToast, jsonApi, toast } from '../api.js';
import { detachedSendRefused } from '../sessions.js';
import { mountComposer } from '../composer.js';
import { stopReading } from '../terminal/terminal-readaloud.js';
import { voiceDictationAvailable } from '../voice.js';
import { mountScrollerPill, scrollerIsAway } from '../latest-pill.js';
import { closeResumeCard, wireResumeCard } from './chat-resume.js';
import { CHAT_ANSWERING, clearPicker, pollPicker, syncDecisionCards } from './transcript-decisions.js';
import {
  TRANSCRIPT_AGENTS, appendEntries, isAgentTurn, isTurn, openLinksOnThePc, syncTurnHead,
} from './transcript-render.js';
import { tagKey } from './transcript-steps.js';
import { resetBlobs } from './transcript-media.js';
import { checkSendWatch, liveTerminalFor, pinChatToKeyboard, sendFromChat, uploadFromChat } from './chat-send.js';
import { liveStep, renderStripLine, syncLiveStep } from './chat-activity.js';
import { mergeFiles, oldestTime, releaseHeld, takeOutbox } from './chat-outbox.js';
import { syncEditedCards, syncFileChips } from './chat-turn-cards.js';

// Whether Chat can read `s`'s history (TRANSCRIPT_AGENTS, transcript-render.js).
export function hasTranscriptReader(s) {
  const agent = String((s && s.agent) || 'claude').toLowerCase();
  return TRANSCRIPT_AGENTS.indexOf(agent) !== -1;
}

// Conversation turns (user + assistant entries) per page — ~20 exchanges.
const PAGE_LIMIT = 40;

// One Load older tap keeps fetching until an older user or assistant turn
// arrives. A page can hold no turn at all (the server's per-request
// read ceiling stops it inside a run of huge tool results), and with tool
// calls hidden such a page would add nothing visible on its own. Bounded by
// requests and by wall time, so a transcript that is all tool calls back to
// the file start costs a few round trips, not an unbounded walk.
const OLDER_CHAIN_MAX = 8;
const OLDER_CHAIN_MS = 2500;
const OLDER_LABEL = 'Load older';

// Live refresh. How often a chat view that is actually being looked
// at asks for what the agent has appended. 3s reads as "live" for a
// conversation without being a busy-loop; the tick itself is cheap by
// design — an unchanged file answers from one `stat` server-side (0.02ms
// measured against 18-45ms to re-read the newest page of a multi-MB
// session), so an idle session costs ~20 round-trips a minute and no read.
const LIVE_POLL_MS = 3000;

// A failing tick backs off instead of hammering: a dropped tunnel or a
// sleeping session-host would otherwise get 20 requests a minute from every
// open chat. Doubles to the cap, resets on the first success.
const LIVE_BACKOFF_MAX_MS = 30000;

// After a send, tick once this long later so the sent turn shows up without
// waiting for the next scheduled one — the agent appends it to its history
// file only after it has consumed the input, which nothing here can observe.
// A full `loadNewest()` would rebuild the whole pane
// (losing scroll position and every open disclosure) after every message.
export const SENT_REFRESH_MS = 3000;
// A forced read's "No new messages" stays this long.
const NEWER_NOTE_MS = 2000;
// The activity line's elapsed counter redraws this often. A local
// repaint of what the last read said, not a request.
export const ACTIVITY_TICK_MS = 1000;
// How far a finger must drag past an edge before it counts as a pull, well
// beyond a tap's jitter and short of a deliberate scroll.
const PULL_PX = 64;

// The chat pane's composer handle (composer.js), mounted once by
// wireChatPane() and re-bound to the open session by openChatPane().
export let chatComposer = null;

// One line per server-side reason, so "nothing there" never reads like
// "couldn't read it" (and vice versa).
const REASON_COPY = {
  session_not_found: 'This session is no longer running',
  // Not an ending: the session list could not be read, so nothing is
  // known about the session; the live tick keeps asking, backed off.
  session_host_unreachable: 'Can’t reach the session host — retrying',
  unsupported_agent: 'Transcript not supported for this agent yet',
  no_transcript: 'No transcript found for this session',
  read_failed: 'Couldn’t read the transcript',
};

// null when the pane is closed, else the session it shows plus the cursor
// for the next older page. `seq` guards a slow response from a previous
// open/refresh landing in a newer view.
//
// Exported as a live binding so transcript-decisions.js's cards and
// plan-picker panel — which read and mutate its fields, never reassign the
// binding itself — see the same object this module does; only openChatPane
// / closeChatPane ever assign `view =` a new value.
export let view = null;
// Whether the step lines (each run of tool calls, thinking and system
// entries) are hidden from the list. Shown by default: one quiet
// line per run, still folded, so a turn that edited four files says so; the
// ⋮ menu's "Hide steps" leaves a plain user ↔ agent exchange. Turns
// themselves render open and collapse one at a time on their own summary;
// there is no bar-wide collapse-all control.
let groupsHidden = false;

// The session id the pane currently shows, or null — session-overlay.js
// uses it so a switch back to Chat never reloads the same session.
export function chatPaneSession() {
  return view ? view.session.session_id : null;
}

// Older entries that end in an agent turn, landing above a list that starts
// with one, are the same turn cut by a page boundary: the older parts join
// the turn already on screen, at its top (its node, and so its open state,
// stay put), and the run straddling the cut stays two cards as before.
function joinOlderTurn(frag) {
  const older = frag.lastElementChild;
  const newer = els.transcriptList.firstElementChild;
  if (!isAgentTurn(older) || !isAgentTurn(newer)) return;
  const moving = document.createDocumentFragment();
  while (older._trParts.firstChild) moving.appendChild(older._trParts.firstChild);
  newer._trParts.insertBefore(moving, newer._trParts.firstChild);
  newer._trFirst = older._trFirst;
  tagKey(newer.querySelector('.tr-turn'), older._trFirst, 'turn');
  older.remove();
  syncTurnHead(newer, liveStep());
}

function syncGroups() {
  els.transcriptList.classList.toggle('tr-hide-groups', groupsHidden);
}

// ⋮ menu (terminal-bar.js) — the chat-only "Hide / Show steps" item.
export function groupsAreHidden() {
  return groupsHidden;
}

export function toggleGroups() {
  groupsHidden = !groupsHidden;
  syncGroups();
}

// ⋮ menu — "Reload transcript": the newest page again, from scratch.
//
// Kept deliberately even though refresh is automatic. Live refresh is a
// best-effort background tick that can stop for reasons the reader cannot
// see: it backs off on repeated fetch failures, and it latches off entirely
// when the session ends. This is the one control that restarts it, and it is
// also the way back from the rare cosmetic drift a forward cursor can leave
// (a harness that rewrites its history rather than appending to it). It is
// no longer the *only* way to see new turns, which is what it used to be —
// so it earns its slot as the escape hatch, not as the refresh button.
export function reloadNewest() {
  if (view) loadNewest();
}

// ⋮ menu — "Load new", and a pull up past the bottom of the list:
// one forward read now, the same one the live tick makes, so what
// arrives is appended without rebuilding the list. For when polling seems
// stuck: it runs whether or not a tick is due, and says what it found. A
// tick already in flight is waited out first, never raced.
export async function loadNew() {
  if (!view || view.forcing) return;
  const target = view;
  target.forcing = true;
  stopLiveTimer();
  newerNote('Checking for new messages…');
  try {
    if (target.ticking && target.inflight) await target.inflight;
    if (view !== target) return;
    target.inflight = forwardRead(target, true);
    const found = await target.inflight;
    if (view !== target) return;
    newerNote(found === 'unchanged' ? 'No new messages' : null, NEWER_NOTE_MS);
  } finally {
    target.forcing = false;
  }
}

// A forced read's status, in the bottom strip (it floated over the
// transcript before). Null clears it; `ms` clears it after that long. While
// it shows it takes the activity line's place.
function newerNote(text, ms) {
  if (!view) return;
  window.clearTimeout(view.newerTimer);
  view.note = text || null;
  renderStripLine();
  if (text && ms) {
    view.newerTimer = window.setTimeout(function () { newerNote(null); }, ms);
  }
}

// --- live refresh rendering ---------------------------------------
//
// Every disclosure the renderer draws carries its entry's key (tagKey,
// transcript-render.js), so an open card can be found again after the
// provisional tail is rebuilt.

// Open/closed state of every keyed disclosure inside `nodes`, so a rebuild
// can put it back. Keyed, not positional: between two ticks a card can move
// from the pending region into the settled list, and a run of folded entries
// can merge into an existing card rather than becoming a new one.
function harvestOpen(nodes) {
  const open = {};
  nodes.forEach(function (li) {
    li.querySelectorAll('[data-tr-key]').forEach(function (d) {
      open[d.dataset.trKey] = d.open;
    });
  });
  return open;
}

function restoreOpen(root, open) {
  if (!open) return;
  root.querySelectorAll('[data-tr-key]').forEach(function (d) {
    const was = open[d.dataset.trKey];
    if (was !== undefined) d.open = was;
  });
}

// The Chat pane's agent: its turns carry that agent's mark and name. An
// absent field (a ?session= deep link's bare {session_id, name}) is Claude,
// as the endpoint assumes. The renderer is handed the session it draws for
// and the step in progress (appendEntries, transcript-render.js).
function chatOpts(toolErrors, pending) {
  return {
    toolErrors: toolErrors,
    answering: CHAT_ANSWERING,
    agent: String((view && view.session.agent) || 'claude').toLowerCase(),
    pending: !!pending,
    edited: true,
    session: view ? view.session : null,
    live: liveStep(),
  };
}

// Append settled entries to the end of the list.
function appendSettled(entries, toolErrors) {
  appendEntries(els.transcriptList, entries, chatOpts(toolErrors, false));
}

// Replace the provisional tail. `open` carries the disclosure state of
// whatever was there before, including cards that have since settled; it
// holds only keys harvested from the old tail, so restoring it list-wide
// touches nothing else.
function renderPending(entries, toolErrors, open) {
  const nodes = appendEntries(els.transcriptList, entries, chatOpts(toolErrors, true));
  nodes.forEach(function (li) { li.dataset.trPending = '1'; });
  restoreOpen(els.transcriptList, open);
  return nodes;
}

// Drop the provisional tail, handing back what was open inside it. A part
// it added to a settled turn leaves that turn's header to be recomputed.
function clearPending() {
  const nodes = (view && view.pendingNodes) || [];
  const open = harvestOpen(nodes);
  const hosts = new Set();
  nodes.forEach(function (li) {
    const host = isAgentTurn(li) ? null : li.parentElement && li.parentElement.closest('.tr-agent-item');
    if (host) hosts.add(host);
    li.remove();
  });
  const live = liveStep();
  hosts.forEach(function (host) { syncTurnHead(host, live); });
  if (view) view.pendingNodes = [];
  return open;
}

// "Following the conversation": within the shared pill's slack of the
// bottom (latest-pill.js). Inside it, new turns scroll into view; outside it
// the reader has deliberately scrolled up into history, is left exactly where
// they are, and the ↓ Latest pill is their one tap back — which puts
// them inside it again, so sticking resumes.
export function atBottom() {
  return !scrollerIsAway(els.transcriptBody);
}

// Apply one tick's worth of new content. The reader's position is the point:
// following the conversation keeps following it, and having scrolled up into
// history stays exactly put.
function applyLive(settled, pending, toolErrors, files) {
  const stick = atBottom();
  const open = clearPending();
  if (settled.length || (files && files.length)) {
    appendSettled(mergeFiles(settled, files || []), toolErrors);
    restoreOpen(els.transcriptList, open);
    view.settled = view.settled.concat(settled);
  }
  view.pending = pending;
  view.pendingNodes = pending.length ? renderPending(pending, toolErrors, open) : [];
  view.entries = view.settled.concat(view.pending);
  syncDecisionCards();
  syncLiveStep();
  if (stick) els.transcriptBody.scrollTop = els.transcriptBody.scrollHeight;
}

function showState(text) {
  if (!els.transcriptState) return;
  els.transcriptState.textContent = text;
  els.transcriptState.hidden = false;
}

function hideState() {
  if (els.transcriptState) els.transcriptState.hidden = true;
}

// --- the live tick -------------------------------------------------

// Is this view allowed to fetch right now? Every condition is re-checked on
// every tick rather than trusted from when the timer was set, because all
// three can change without this module being told (the overlay closes, the
// phone locks, the session exits).
export function liveAllowed() {
  return !!view &&
    !view.ended &&
    !!els.terminalOverlay &&
    els.terminalOverlay.dataset.mode === 'chat' &&
    document.visibilityState === 'visible';
}

export function stopLiveTimer() {
  if (view && view.liveTimer) {
    window.clearTimeout(view.liveTimer);
    view.liveTimer = null;
  }
}

export function scheduleLive(delay) {
  if (!view) return;
  stopLiveTimer();
  if (!liveAllowed()) return;
  // Self-scheduling rather than setInterval: a tab that was hidden for an
  // hour resumes with one catch-up fetch instead of a backlog of missed
  // ticks, which is the acceptance criterion for a locked phone.
  view.liveTimer = window.setTimeout(liveTick, delay);
}

// Called whenever the answer to `liveAllowed()` may have changed.
export function syncLiveRefresh() {
  if (!view) return;
  renderStripLine();
  if (!liveAllowed()) {
    stopLiveTimer();
    return;
  }
  // `ticking` as well as the timer: a tick clears its own timer before
  // awaiting, so without this a visibilitychange (or the post-send nudge)
  // landing mid-request would start a second one alongside it.
  if (!view.liveTimer && !view.ticking) liveTick();
}

// The source went unavailable. Say which, and stop only for the reasons
// that are actually terminal.
//
// `session_not_found` means the session exited: latch off, because nothing
// will bring it back and polling a dead session forever is the thing the
// acceptance criterion forbids. `unsupported_agent` never changes for a
// session either. Every other reason is a condition that can
// clear on its own — most sharply `no_transcript`, which a *live* Claude
// session reports for as long as its hook row is deleted and the filesystem
// fallback can't name the file — so those keep ticking, backed off,
// and recover without the reader having to tap Reload. Treating a transient
// unknown as an ending would be the same mistake in reverse as treating an
// unresolved check as a pass.
function liveUnavailable(reason) {
  showState(REASON_COPY[reason] || 'Transcript unavailable');
  view.reasonShown = true;
  if (reason === 'session_not_found' || reason === 'unsupported_agent') {
    view.ended = true;   // the list stays on screen: what was read is still worth reading
    stopLiveTimer();
    return;
  }
  view.backoff = Math.min(LIVE_BACKOFF_MAX_MS, (view.backoff || LIVE_POLL_MS) * 2);
  scheduleLive(view.backoff);
}

async function liveTick() {
  if (!view || !liveAllowed()) return;
  view.inflight = forwardRead(view, false);
  await view.inflight;
}

// One forward-cursor read, the background tick's or a forced one
// Resolves to what it found — 'changed', 'unchanged',
// 'unavailable', 'reset', 'failed', or 'stale' when the pane moved on — so
// a forced read can say it. A failed forced read toasts, as a tapped action
// does; a failed tick backs off quietly, as it always has.
export async function forwardRead(target, manual) {
  const seq = target.seq;
  target.liveTimer = null;
  target.ticking = true;
  // Not awaited: the screen read is independent of the transcript, and a
  // session whose transcript is unavailable can still have a plan waiting.
  pollPicker();
  const q = new URLSearchParams({ after: String(target.tail) });
  if (target.size != null) q.set('size', String(target.size));
  if (target.outboxRev != null) q.set('ob', String(target.outboxRev));
  let body;
  try {
    body = await jsonApi(
      '/api/claude-code/sessions/' + encodeURIComponent(target.session.session_id) +
        '/transcript?' + q.toString()
    );
  } catch (exc) {
    target.ticking = false;
    if (view !== target || view.seq !== seq) return 'stale';
    if (manual) {
      apiFailToast('Load new failed', exc);
      scheduleLive(target.backoff || LIVE_POLL_MS);
      return 'failed';
    }
    // Quietly, and slower each time: a tick is background work the reader
    // did not ask for, so a failing one must not toast over the pane the way
    // a tapped Reload does.
    target.backoff = Math.min(
      LIVE_BACKOFF_MAX_MS, (target.backoff || LIVE_POLL_MS) * 2
    );
    scheduleLive(target.backoff);
    return 'failed';
  }
  target.ticking = false;
  if (view !== target || view.seq !== seq) return 'stale';
  target.backoff = 0;
  if (!body.available) {
    liveUnavailable(body.reason);
    return 'unavailable';
  }
  // Read before anything lands: a card that appears at the end of the turn
  // keeps a reader who was following the conversation at the bottom.
  const stick = atBottom();
  if (body.reset) {
    // The file was rotated, or this view fell further behind than one
    // request may read. Either way the newest turns are what it wants.
    loadNewest();
    return 'reset';
  }
  if (body.activity !== undefined) {
    target.activity = body.activity;
    renderStripLine();
  }
  // A file sent since the last read is the newest thing there is: it lands
  // after whatever settled this tick, never back in time.
  const files = takeOutbox(body, -Infinity);
  if (body.changed) {
    target.tail = body.tail;
    target.size = body.size;
    applyLive(body.entries || [], body.pending || [], body.tool_errors || target.toolErrors, files);
  } else {
    if (body.size != null) target.size = body.size;
    if (files.length) applyLive([], target.pending || [], target.toolErrors, files);
  }
  syncFileChips();
  if (syncEditedCards() && stick) els.transcriptBody.scrollTop = els.transcriptBody.scrollHeight;
  // A tick that got an answer clears any reason line an earlier failed one
  // left on screen, so a condition that cleared by itself looks like it.
  // Checked after this tick's turns land: a view that opened unavailable
  // has none until then.
  if (target.reasonShown) {
    target.reasonShown = false;
    if (view.entries && view.entries.length) hideState();
    else showState('Nothing in the transcript yet');
  }
  checkSendWatch(target);
  scheduleLive(LIVE_POLL_MS);
  return body.changed ? 'changed' : 'unchanged';
}

function fetchPage(before) {
  const q = new URLSearchParams({ limit: String(PAGE_LIMIT) });
  if (before != null) q.set('before', String(before));
  return jsonApi(
    '/api/claude-code/sessions/' + encodeURIComponent(view.session.session_id) +
      '/transcript?' + q.toString()
  );
}

async function loadNewest() {
  if (!view) return;
  const seq = ++view.seq;
  stopLiveTimer();
  els.transcriptList.innerHTML = '';
  els.transcriptOlder.hidden = true;
  els.transcriptOlder.textContent = OLDER_LABEL;
  view.cursor = null;
  view.pendingNodes = [];
  newerNote(null);
  clearPicker();
  view.picker = null;
  view.pickerSig = null;
  view.pickerSent = null;
  view.ended = false;
  view.backoff = 0;
  view.reasonShown = false;
  view.outboxRev = null;
  view.outboxShown = new Set();
  view.outboxHeld = [];
  showState('Loading…');
  let body;
  try {
    body = await fetchPage(null);
  } catch (exc) {
    if (!view || view.seq !== seq) return;
    showState('Couldn’t load the transcript');
    apiFailToast('Transcript failed', exc);
    return;
  }
  if (!view || view.seq !== seq) return;
  if (!body.available) {
    // Only an ending ends the view, as on a live tick. A `claude --resume`
    // launch sits here from the start, with no transcript yet and the resume
    // picker on its screen, so the screen is asked about now rather than
    // after the first backed-off tick.
    liveUnavailable(body.reason);
    pollPicker();
    return;
  }
  view.activity = body.activity || null;
  renderStripLine();
  // The page is one complete list, as it has always been. It also carries `tail`
  // — the offset where its newest, still-growing message begins — and keys
  // every entry from there on, so the live region can be told apart locally
  // and re-rendered later without disturbing anything above it.
  const all = body.entries || [];
  const cut = all.findIndex(function (e) {
    return e.offset != null && body.tail != null && e.offset >= body.tail;
  });
  const entries = cut < 0 ? all : all.slice(0, cut);
  const pending = cut < 0 ? [] : all.slice(cut);
  // Everything currently shown, kept for lastAssistantEntryFullText().
  // The live tick maintains it too, so read-aloud follows the conversation
  // instead of reading whatever was newest when the pane opened.
  view.settled = entries;
  view.pending = pending;
  view.entries = entries.concat(pending);
  view.tail = body.tail != null ? body.tail : 0;
  view.size = body.size;
  // Sent files older than this page wait for the page that reaches them.
  const files = takeOutbox(body, body.next_cursor == null ? -Infinity : oldestTime(all));
  if (!view.entries.length && !files.length && body.next_cursor == null) {
    showState('Nothing in the transcript yet');
    // Still live: an empty transcript is the normal state of a session that
    // has just started, and its first turn should appear on its own.
    scheduleLive(LIVE_POLL_MS);
    return;
  }
  hideState();
  // A property of the harness, so it is the same on every page of one
  // session — read from the newest page and reused when older pages are
  // prepended.
  view.toolErrors = body.tool_errors || 'reported';
  appendSettled(mergeFiles(entries, files), view.toolErrors);
  view.pendingNodes = pending.length
    ? renderPending(pending, view.toolErrors, null)
    : [];
  syncDecisionCards();
  syncLiveStep();
  view.cursor = body.next_cursor;
  els.transcriptOlder.hidden = view.cursor == null;
  syncFileChips();
  syncEditedCards();
  els.transcriptBody.scrollTop = els.transcriptBody.scrollHeight;
  scheduleLive(LIVE_POLL_MS);
}

// The line a Load older that found no turn leaves behind, so the
// reader is told what happened instead of seeing a tap that did nothing.
function olderLabel(entries) {
  const calls = entries.filter(function (e) { return e.kind === 'tool_call'; }).length;
  if (!calls) return 'No messages yet — ' + OLDER_LABEL;
  return calls + ' tool call' + (calls === 1 ? '' : 's') + ' loaded, no messages yet — ' + OLDER_LABEL;
}

function startMarker() {
  const li = document.createElement('li');
  li.className = 'tr-start';
  li.textContent = 'Start of transcript — no older messages';
  return li;
}

export async function loadOlder() {
  if (!view || view.loading || view.cursor == null) return;
  const target = view;
  const seq = view.seq;
  view.loading = true;
  els.transcriptOlder.disabled = true;
  els.transcriptOlder.textContent = 'Loading…';
  const box = els.transcriptBody;
  const heightBefore = box.scrollHeight;
  const topBefore = box.scrollTop;
  const startedAt = Date.now();
  // Older pages accumulate oldest-first and render once, so a run of tool
  // calls that straddles two pages folds into one group, not two.
  let older = [];
  let cursor = view.cursor;
  let toolErrors = null;
  let unavailable = null;
  let requests = 0;
  try {
    while (cursor != null) {
      const body = await fetchPage(cursor);
      if (view !== target || view.seq !== seq) return;
      requests += 1;
      if (!body.available) {
        unavailable = body.reason;
        cursor = null;
        break;
      }
      older = (body.entries || []).concat(older);
      toolErrors = body.tool_errors || toolErrors;
      cursor = body.next_cursor;
      if (older.some(isTurn)) break;
      if (requests >= OLDER_CHAIN_MAX || Date.now() - startedAt >= OLDER_CHAIN_MS) break;
    }
  } catch (exc) {
    if (view !== target || view.seq !== seq) return;
    // Whatever pages did arrive still render below; the cursor stays at the
    // last one that did, so the next tap resumes rather than skipping.
    apiFailToast('Load older failed', exc);
  } finally {
    if (view === target && view.seq === seq) {
      view.loading = false;
      els.transcriptOlder.disabled = false;
    }
  }
  if (unavailable) toast(REASON_COPY[unavailable] || 'Transcript unavailable', 'bad');
  const files = releaseHeld(cursor == null ? -Infinity : oldestTime(older));
  if (older.length || files.length) {
    const frag = document.createDocumentFragment();
    appendEntries(frag, mergeFiles(older, files), chatOpts(toolErrors || view.toolErrors || 'reported', false));
    joinOlderTurn(frag);
    els.transcriptList.insertBefore(frag, els.transcriptList.firstChild);
    // Older entries join `view.entries` too, so a question on an older page
    // can find its result and never reads as the pending one.
    view.settled = older.concat(view.settled || []);
    view.entries = view.settled.concat(view.pending || []);
    syncDecisionCards();
  }
  const foundTurn = older.some(isTurn);
  view.cursor = cursor;
  if (cursor == null && !foundTurn && !unavailable) {
    // Only tool calls back to the file start: say so, rather than let the
    // button vanish over a list that did not visibly change.
    els.transcriptList.insertBefore(startMarker(), els.transcriptList.firstChild);
  }
  els.transcriptOlder.textContent = foundTurn || !older.length ? OLDER_LABEL : olderLabel(older);
  els.transcriptOlder.hidden = view.cursor == null;
  syncFileChips();
  syncEditedCards();
  // Keep what was on screen where it was: grow scrollTop by exactly the
  // height the older page added above it.
  box.scrollTop = topBefore + (box.scrollHeight - heightBefore);
}


// ⌨ keys for `s`: a full-control session's PTY, even while Chat shows (answer
// a y/n prompt without switching); null for a detached one, which renders
// the button disabled with its reason.
function chatKeys(s) {
  if (s.kind === 'remote') return null;
  return {
    send: function (bytes) {
      const t = liveTerminalFor(s);
      if (t) t.ws.send(JSON.stringify({ type: 'input', data: bytes }));
    },
    onOpen: function () {
      if (liveTerminalFor(s)) return;
      chatComposer.closePopovers();
      toast('Keys drive the live terminal: show Terminal once to connect it', '', { icon: 'keyboard' });
    },
  };
}

function bindComposer(s) {
  if (!chatComposer) return;
  chatComposer.reset();
  const detached = s.kind === 'remote';
  chatComposer.setPlaceholder(detached ? 'Message for the agent' : 'Message');
  chatComposer.setKeys(chatKeys(s));
  // The console-input gate (agents.py) holds back Send alone: a draft,
  // dictation and attach still work for an agent never probed.
  if (detachedSendRefused(s)) {
    chatComposer.setSendable(false, 'No console input for this agent: sending is off');
  } else {
    chatComposer.setSendable(true);
  }
  chatComposer.setAvailability({
    dictate: voiceDictationAvailable(),
    ocr: !!(state.status && state.status.screenshot_ocr),
  });
}

export function closeChatComposerPopovers() {
  if (chatComposer) chatComposer.closePopovers();
}

// Load `s` into the chat pane. The overlay shell (visibility, title, body
// lock) and the data-mode flip are session-overlay.js's; this only owns the
// pane's content. The inline "Detached session — no terminal" note explains
// the disabled Terminal segment (mockup screen 6).
export function openChatPane(s) {
  if (!els.chatPane) return;
  closeResumeCard();
  if (view) {
    window.clearTimeout(view.refreshTimer);
    window.clearTimeout(view.watchTimer);
    stopLiveTimer();
  }
  resetBlobs(s.session_id);
  view = {
    session: s, cursor: null, loading: false, seq: 0, refreshTimer: null,
    entries: null, toolErrors: 'reported',
    // Live refresh: `tail`/`size` are the forward cursor, `pending*`
    // the provisional tail currently rendered, `ended` latches when the
    // session is gone so no timer is ever rescheduled for it.
    tail: 0, size: null, liveTimer: null, ticking: false, backoff: 0,
    ended: false, reasonShown: false,
    // A forced read: the read in flight (either kind), whether a
    // forced one is running, and the status line's hide timer.
    inflight: null, forcing: false, newerTimer: null, note: null,
    // The turn in progress, as the last read described it.
    activity: null,
    settled: [], pending: [], pendingNodes: [],
    // The plan panel: the last screen read, its render signature,
    // and when an answer was sent from it.
    picker: null, pickerSig: null, pickerSent: null, pickerBusy: false,
  };
  groupsHidden = false;
  syncGroups();
  bindComposer(s);
  els.chatNote.hidden = s.kind !== 'remote';
  loadNewest();
}

export function closeChatPane() {
  resetBlobs(null);
  if (view) {
    newerNote(null);
    view.seq += 1;  // any in-flight page lands nowhere
    window.clearTimeout(view.refreshTimer);
    stopLiveTimer();  // a closed overlay fetches nothing
  }
  view = null;
  renderStripLine();
  if (!els.chatPane) return;
  els.transcriptList.innerHTML = '';
  clearPicker();
  closeResumeCard();
  els.chatNote.hidden = true;
  if (chatComposer) chatComposer.reset();
  pinChatToKeyboard();
  hideState();
}

export function wireChatPane() {
  if (!els.chatPane) return;
  wireResumeCard({
    session: function () { return view ? view.session : null; },
    // A resume swaps the conversation under the pane: load it afresh.
    onResumed: function () { if (view) openChatPane(view.session); },
  });
  syncGroups();
  openLinksOnThePc(els.transcriptList);
  els.transcriptOlder.addEventListener('click', function () { loadOlder(); });
  mountScrollerPill(els.chatLatest, els.transcriptBody);
  chatComposer = mountComposer(els.chatComposeBar, {
    placeholder: 'Message',
    send: sendFromChat,
    upload: uploadFromChat,
    keys: null,
    // Starting to talk silences any in-flight read-aloud.
    onDictationStart: stopReading,
    // Chat is the surface a desktop keyboard actually types into, so it is
    // the one that takes the Ctrl/Cmd+Enter send. Plain Enter stays
    // a newline for everyone, phone included.
    sendOnModEnter: true,
  });
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', pinChatToKeyboard);
    window.visualViewport.addEventListener('scroll', pinChatToKeyboard);
  }
  // A backgrounded tab or a locked phone stops refreshing entirely; coming
  // back does one catch-up fetch, not a replay of every tick it missed
  // One listener for the page, not one per opened session.
  document.addEventListener('visibilitychange', syncLiveRefresh);
  // Scrolling to the very top pulls the next older page in without a tap.
  els.transcriptBody.addEventListener('scroll', function () {
    if (view && view.cursor != null && !view.loading && els.transcriptBody.scrollTop <= 0) {
      loadOlder();
    }
  });
  wirePullGestures(els.transcriptBody);
}

// Pull gestures: a drag that starts and ends at one edge of the list
// forces a fetch there — down at the top for older turns (which also covers
// a list too short to scroll, where the scroll listener above never fires),
// up past the bottom for new ones, the same read as ⋮ Load new. The
// listeners are passive and only read the drag, so native scrolling, iOS
// rubber-banding and the Latest pill are untouched; a drag that scrolled the
// list ends away from the edge it started at and does nothing.
function wirePullGestures(box) {
  let startY = null;
  let fromTop = false;
  let fromBottom = false;
  const atTop = function () { return box.scrollTop <= 0; };
  const atBottom = function () {
    return box.scrollTop + box.clientHeight >= box.scrollHeight - 2;
  };
  box.addEventListener('touchstart', function (ev) {
    if (!view || !ev.touches || ev.touches.length !== 1) {
      startY = null;
      return;
    }
    startY = ev.touches[0].clientY;
    fromTop = atTop();
    fromBottom = atBottom();
  }, { passive: true });
  box.addEventListener('touchend', function (ev) {
    if (startY == null || !view || !ev.changedTouches || !ev.changedTouches.length) return;
    const dy = ev.changedTouches[0].clientY - startY;
    startY = null;
    if (dy >= PULL_PX && fromTop && atTop()) {
      if (view.cursor != null) loadOlder();
    } else if (dy <= -PULL_PX && fromBottom && atBottom()) {
      loadNew();
    }
  }, { passive: true });
}
