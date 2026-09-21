/* Session transcript — the Chat pane of the session overlay (#953, #982).
 *
 * The whole conversation of one live Coding session, chat-style: typed
 * user prompts and assistant replies expanded, everything else — tool calls
 * with their results, thinking, harness plumbing, sub-agent traffic —
 * folded per run into one disclosure that expands item by item. Backed by
 * the paginated `/api/claude-code/sessions/{sid}/transcript` endpoint
 * (Tailscale + passkey gated, like the Board drawer's /exchange): the
 * newest page loads first, older pages prepend on "Load older" or when the
 * list is scrolled to its top.
 *
 * The shared composer (composer.js, #980) is mounted under the list for
 * every session kind (#983). ➤ Send always goes through the kind-agnostic
 * /input route, never the terminal's WebSocket — even for a full-control
 * session whose terminal is connected: Chat cannot show the PTY, so the
 * route's verdict (confirmed / unconfirmed / queued, sessions.js
 * sendOutcome) is the only feedback the phone gets, and the session-host
 * applies the same framing and settle protocol the terminal composer does
 * (#611). Attach uploads inline and appends the path, which a detached
 * session can take too. The ⌨ keys drive a full-control session's live
 * terminal socket while Chat shows; a detached session has none, so they
 * render disabled. A detached session whose agent was never probed for
 * console input keeps the composer but not ➤ Send.
 *
 * Since #982 this is one pane of #terminalOverlay, not its own overlay:
 * session-overlay.js opens/closes it and flips the overlay's data-mode;
 * the bar's ⋮ menu (terminal-bar.js) carries the pane's Show-tool-calls and
 * Reload actions. Switching panes never reloads: the pages and fold state
 * loaded here survive a trip through Terminal mode.
 *
 * ## Live refresh (#1050)
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
 * at all. Each page instance (a PC mirror window is its own, #282) polls
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
 *     run of folded entries merges into the trailing run card rather than
 *     starting a new one, or an autonomous stretch would fragment into a
 *     card per tick.
 *   - `pending` — the newest message, which may still be growing (a harness
 *     writes one message as several lines). Re-rendered wholesale each time
 *     it changes, with open disclosures carried across by entry offset.
 *
 * Rebuilding only that tail is what keeps this clear of the Board's lesson
 * (#680/#958): a poll that rebuilds the DOM breaks whatever the reader is
 * interacting with.
 */

import { els, state } from './state.js';
import { apiFailToast, authHeaders, jsonApi, toast } from './api.js';
import { renderMarkdown } from './markdown.js';
import { detachedSendRefused, sendOutcome, sendSessionMessage } from './sessions.js';
import { keyboardOverlayHeight } from './terminal.js';
import { mountComposer } from './composer.js';
import { uploadSessionFile } from './terminal-compose.js';
import { stopReading } from './terminal-readaloud.js';
import { voiceDictationAvailable } from './voice.js';
import { ensureTerminalToken } from './webauthn.js';
import { icon } from './_vendored/icons/icons.js';

// Agents whose native history the server-side reader understands — the
// same set as the endpoint's flavour map (app/webapp/routers/
// session_transcript.py). Decides Chat availability without a probe; the
// pane still shows the server's own reason line if it disagrees. An absent
// agent field (a ?session= deep link's bare {session_id, name}) defaults
// to Claude, as the endpoint does.
const TRANSCRIPT_AGENTS = ['claude', 'codex', 'grok', 'pi', 'antigravity', 'copilot'];

export function hasTranscriptReader(s) {
  const agent = String((s && s.agent) || 'claude').toLowerCase();
  return TRANSCRIPT_AGENTS.indexOf(agent) !== -1;
}

// Conversation turns (user + assistant entries) per page — ~20 exchanges.
const PAGE_LIMIT = 40;

// One Load older tap keeps fetching until an older user or assistant turn
// arrives (#1120). A page can hold no turn at all (the server's per-request
// read ceiling stops it inside a run of huge tool results), and with tool
// calls hidden such a page used to add nothing visible. Bounded by requests
// and by wall time, so a transcript that is all tool calls back to the file
// start costs a few round trips, not an unbounded walk.
const OLDER_CHAIN_MAX = 8;
const OLDER_CHAIN_MS = 2500;
const OLDER_LABEL = 'Load older';

// Live refresh (#1050). How often a chat view that is actually being looked
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

// How close to the bottom still counts as "following the conversation", in
// px. Inside this, new turns scroll into view; outside it the reader has
// deliberately scrolled up into history and is left exactly where they are.
const STICK_PX = 120;

// After a send, tick once this long later so the sent turn shows up without
// waiting for the next scheduled one — the agent appends it to its history
// file only after it has consumed the input, which nothing here can observe.
// Before #1050 this was a full `loadNewest()`, which rebuilt the whole pane
// (losing scroll position and every open disclosure) after every message.
const SENT_REFRESH_MS = 3000;

// The chat pane's composer handle (composer.js), mounted once by
// wireChatPane() and re-bound to the open session by openChatPane().
let chatComposer = null;

// One line per server-side reason, so "nothing there" never reads like
// "couldn't read it" (and vice versa).
const REASON_COPY = {
  session_not_found: 'This session is no longer running',
  unsupported_agent: 'Transcript not supported for this agent yet',
  no_transcript: 'No transcript found for this session',
  read_failed: 'Couldn’t read the transcript',
};

// How far the open session's harness can be trusted to mark a failed tool
// call — the server's per-flavour `tool_errors` (#1020). Only a `reported`
// harness lets an unmarked call be read as "it worked"; for the other two
// values an unmarked call is "nobody recorded the outcome", which the
// expanded card says in as many words rather than leaving silence to pass
// for success. `reported` is the fallback for a page that predates the
// field only because that is also what Claude — the default flavour —
// reports.
const TOOL_ERRORS_NOTE = {
  partial: 'This agent doesn’t record every tool failure — an unmarked call may still have failed.',
  none: 'This agent doesn’t record whether a tool call failed.',
};

function toolOutcomeNote(e, toolErrors) {
  if (e.error === true) return '';
  if (e.kind !== 'tool_call' && e.kind !== 'tool_result') return '';
  return TOOL_ERRORS_NOTE[toolErrors] || '';
}

const KIND_ICON = {
  tool_call: 'terminal',
  tool_result: 'terminal',
  thinking: 'sparkle',
  system: 'plug',
  user: 'messages-square',
  assistant: 'messages-square',
};

// null when the pane is closed, else the session it shows plus the cursor
// for the next older page. `seq` guards a slow response from a previous
// open/refresh landing in a newer view.
let view = null;
// Whether the folded tool-call / system groups are hidden from the list.
// Hidden by default: the view opens as a plain user ↔ agent exchange; the
// ⋮ menu's "Show tool calls" reveals the groups, still folded, between the
// turns. Turns themselves render open and collapse one at a time on their
// own summary (the bar-wide collapse-all left with the bar, #982).
let groupsHidden = true;

// The session id the pane currently shows, or null — session-overlay.js
// uses it so a switch back to Chat never reloads the same session.
export function chatPaneSession() {
  return view ? view.session.session_id : null;
}

function isTurn(e) {
  return (e.kind === 'user' || e.kind === 'assistant') && !e.sidechain;
}

// Bare URLs become links everywhere transcript text is shown — reply
// prose (after markdown, so a markdown link is left alone), prompts, tool
// bodies. DOM-walking, never string-replacing HTML: text nodes outside an
// existing <a> are split around each match into real anchor elements, so
// nothing a transcript contains can ever be interpreted as markup.
const URL_RE = /https?:\/\/[^\s<>"'`]+/g;
const TRAILING_PUNCT_RE = /[.,;:!?'")\]]+$/;

function linkify(root) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: function (node) {
      return node.parentElement && node.parentElement.closest('a')
        ? NodeFilter.FILTER_REJECT
        : NodeFilter.FILTER_ACCEPT;
    },
  });
  const nodes = [];
  let n;
  while ((n = walker.nextNode())) {
    if (URL_RE.test(n.nodeValue)) nodes.push(n);
    URL_RE.lastIndex = 0;
  }
  nodes.forEach(function (textNode) {
    const text = textNode.nodeValue;
    const frag = document.createDocumentFragment();
    let last = 0;
    let m;
    URL_RE.lastIndex = 0;
    while ((m = URL_RE.exec(text))) {
      let url = m[0];
      const trail = url.match(TRAILING_PUNCT_RE);
      if (trail) url = url.slice(0, -trail[0].length);
      if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
      const a = document.createElement('a');
      a.href = url;
      a.textContent = url;
      a.target = '_blank';
      a.rel = 'noopener';
      frag.appendChild(a);
      last = m.index + url.length;
    }
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    textNode.parentNode.replaceChild(frag, textNode);
  });
}

function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function meta(label, ts) {
  const el = document.createElement('div');
  el.className = 'tr-meta';
  const when = fmtTime(ts);
  el.textContent = when ? label + ' · ' + when : label;
  return el;
}

function pre(text, truncated) {
  const el = document.createElement('pre');
  el.className = 'tr-pre';
  el.textContent = text || '';
  linkify(el);
  if (truncated) {
    const mark = document.createElement('span');
    mark.className = 'tr-trunc';
    mark.textContent = '\n(truncated)';
    el.appendChild(mark);
  }
  return el;
}

function firstLine(text, max) {
  const line = String(text || '').split('\n')[0].trim();
  return line.length > max ? line.slice(0, max) + '…' : line;
}

function copyLabel(kind) {
  return kind === 'user' ? 'Prompt' : 'Reply';
}

// Tap the copy glyph on a user/assistant card: writes the clipboard
// synchronously inside the tap gesture — iOS requires this, an `await`
// ahead of the first write loses the gesture and the copy silently fails on
// the one device this feature is for — with whatever text the card already
// has. A capped entry then fetches the uncapped one (#985's ``/transcript/
// entry`` route) and *visibly* upgrades the clipboard with a second toast:
// never a silent rewrite, since a paste in the gap between the two would
// hand back truncated text with no reason to suspect it, and the clipboard
// changing again afterwards would be worse.
async function copyTurn(e) {
  const label = copyLabel(e.kind);
  try {
    await navigator.clipboard.writeText(e.text || '');
  } catch (exc) {
    toast('Clipboard unavailable — copy manually', 'error');
    return;
  }
  if (!e.truncated) {
    toast(label + ' copied', 'good', { icon: 'copy' });
    return;
  }
  toast(label + ' copied — loading the full text…', '', { icon: 'copy' });
  const sid = view ? view.session.session_id : null;
  const fail = function () {
    toast(label + ' copy is truncated — the full text didn’t load', 'bad', { icon: 'copy' });
  };
  if (!sid || e.offset == null) {
    fail();
    return;
  }
  let body;
  try {
    const tt = await ensureTerminalToken();
    body = await jsonApi(
      '/api/claude-code/sessions/' + encodeURIComponent(sid) +
        '/transcript/entry?offset=' + encodeURIComponent(e.offset),
      { headers: authHeaders({ terminalToken: tt }) }
    );
  } catch (exc) {
    fail();
    return;
  }
  if (!body || !body.available) {
    fail();
    return;
  }
  try {
    await navigator.clipboard.writeText(body.text);
  } catch (exc) {
    fail();
    return;
  }
  toast('Full ' + label.toLowerCase() + ' copied', 'good', { icon: 'copy' });
}

// The newest assistant entry's text for read-aloud in Chat mode / a detached
// session (#988) — the same reply provider terminal-readaloud.js otherwise
// reads from the live xterm buffer. Parallels copyTurn()'s upgrade: the
// loaded page's text first, then the uncapped /transcript/entry fetch when
// that page capped it, so a long reply is never read out silently shortened.
// '' when no assistant turn has loaded yet.
export async function lastAssistantEntryFullText() {
  if (!view || !view.entries) return '';
  let e = null;
  for (let i = view.entries.length - 1; i >= 0; i--) {
    if (isTurn(view.entries[i]) && view.entries[i].kind === 'assistant') {
      e = view.entries[i];
      break;
    }
  }
  if (!e) return '';
  if (!e.truncated || e.offset == null) return e.text || '';
  const sid = view.session.session_id;
  try {
    const tt = await ensureTerminalToken();
    const body = await jsonApi(
      '/api/claude-code/sessions/' + encodeURIComponent(sid) +
        '/transcript/entry?offset=' + encodeURIComponent(e.offset),
      { headers: authHeaders({ terminalToken: tt }) }
    );
    if (body && body.available) return body.text || '';
  } catch (exc) { /* fall through to the capped text already loaded */ }
  return e.text || '';
}

// A turn: a collapsible card, open by default (or per the bar's toggle),
// whose summary is the meta line plus — only while closed — the first
// line of the text. The user's own prompt is plain text (pre-wrap); the
// agent's reply goes through the same escape-first markdown renderer the
// Life OS doc browser uses — never raw HTML from a transcript. The copy
// glyph sits before the chevron and stops the tap from reaching the
// <summary> (which would otherwise toggle the card on any click inside it,
// same guard as every other interactive control living in one — dom-utils.js,
// jobs.js, …).
function renderTurn(e) {
  const li = document.createElement('li');
  li.className = 'tr-turn-item';
  const d = document.createElement('details');
  d.className = 'tr-turn tr-' + e.kind;
  d.open = true;
  tagKey(d, e);
  const s = document.createElement('summary');
  s.className = 'tr-turn-summary';
  s.appendChild(meta(e.kind === 'user' ? 'You' : 'Agent', e.timestamp));
  const hint = document.createElement('span');
  hint.className = 'tr-turn-hint';
  hint.textContent = firstLine(e.text, 80);
  s.appendChild(hint);
  const copyBtn = document.createElement('button');
  copyBtn.type = 'button';
  copyBtn.className = 'tr-turn-copy hit-target';
  copyBtn.setAttribute('aria-label', 'Copy ' + copyLabel(e.kind).toLowerCase());
  copyBtn.innerHTML = icon('copy');
  copyBtn.addEventListener('click', function (ev) {
    ev.preventDefault();
    ev.stopPropagation();
    copyTurn(e);
  });
  s.appendChild(copyBtn);
  const chev = document.createElement('span');
  chev.className = 'tr-turn-chevron';
  chev.setAttribute('aria-hidden', 'true');
  chev.textContent = '›';
  s.appendChild(chev);
  d.appendChild(s);
  const body = document.createElement('div');
  if (e.kind === 'assistant') {
    body.className = 'tr-text tr-md';
    body.innerHTML = renderMarkdown(e.text || '');
  } else {
    body.className = 'tr-text';
    body.textContent = e.text || '';
  }
  linkify(body);
  d.appendChild(body);
  if (e.truncated) {
    const mark = document.createElement('div');
    mark.className = 'tr-trunc';
    mark.textContent = '(truncated — open the terminal for the rest)';
    d.appendChild(mark);
  }
  li.appendChild(d);
  return li;
}

function syncGroups() {
  els.transcriptList.classList.toggle('tr-hide-groups', groupsHidden);
}

// ⋮ menu (terminal-bar.js) — the chat-only "Show / Hide tool calls" item.
export function groupsAreHidden() {
  return groupsHidden;
}

export function toggleGroups() {
  groupsHidden = !groupsHidden;
  syncGroups();
}

// ⋮ menu — "Reload transcript": the newest page again, from scratch.
//
// Kept after #1050 made refresh automatic, deliberately. Live refresh is a
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

// One folded item inside a run group — its own <details>, so a single
// tool call can be opened without expanding its siblings.
function renderItem(e, toolErrors) {
  const d = document.createElement('details');
  d.className = 'tr-item tr-item-' + e.kind;
  tagKey(d, e);
  // A failed call is marked on the row itself, so it reads as failed while
  // the group is still folded open at one item — never as a banner over the
  // whole turn (#1020).
  if (e.error === true) d.classList.add('tr-item-failed');
  const s = document.createElement('summary');
  s.innerHTML = icon(KIND_ICON[e.kind] || 'plug');
  const name = document.createElement('span');
  name.className = 'tr-item-name';
  const hint = document.createElement('span');
  hint.className = 'tr-item-hint';
  if (e.kind === 'tool_call') {
    name.textContent = e.name || 'tool';
    hint.textContent = e.summary || '';
  } else if (e.kind === 'tool_result') {
    name.textContent = 'result';
    hint.textContent = firstLine(e.text, 80);
  } else if (e.kind === 'thinking') {
    name.textContent = 'thinking';
    hint.textContent = firstLine(e.text, 80);
  } else if (e.sidechain) {
    name.textContent = 'sub-agent';
    hint.textContent = firstLine(e.text, 80);
  } else {
    name.textContent = e.label || 'system';
    hint.textContent = firstLine(e.text, 80);
  }
  s.appendChild(name);
  if (e.error === true) {
    const chip = document.createElement('span');
    chip.className = 'tr-fail-chip';
    chip.textContent = 'failed';
    s.appendChild(chip);
  }
  s.appendChild(hint);
  d.appendChild(s);
  const body = document.createElement('div');
  body.className = 'tr-item-body';
  if (e.kind === 'tool_call') {
    if (e.summary) body.appendChild(pre(e.summary, false));
    if (e.result != null) {
      body.appendChild(pre(e.result, e.result_truncated));
    } else {
      const none = document.createElement('div');
      none.className = 'tr-trunc';
      none.textContent = 'no result on this page';
      body.appendChild(none);
    }
  } else {
    body.appendChild(pre(e.text, e.truncated));
  }
  // The honest third state: this harness cannot say whether the call
  // failed. Said here, in the body, rather than as a marker on every row —
  // it is the answer to "did this work?", and that question is asked at the
  // moment the card is opened, not while it is folded.
  const note = toolOutcomeNote(e, toolErrors);
  if (note) {
    const unknown = document.createElement('div');
    unknown.className = 'tr-outcome-unknown';
    unknown.textContent = note;
    body.appendChild(unknown);
  }
  d.appendChild(body);
  return d;
}

function runLabel(run) {
  const n = { tool: 0, thinking: 0, system: 0 };
  run.forEach(function (e) {
    if (e.kind === 'tool_call' || e.kind === 'tool_result') n.tool += 1;
    else if (e.kind === 'thinking') n.thinking += 1;
    else n.system += 1;
  });
  const parts = [];
  if (n.tool) parts.push(n.tool + (n.tool === 1 ? ' tool call' : ' tool calls'));
  if (n.thinking) parts.push(n.thinking + ' thinking');
  if (n.system) parts.push(n.system + ' system');
  return parts.join(' · ');
}

function failedCount(run) {
  return run.reduce(function (n, e) { return n + (e.error === true ? 1 : 0); }, 0);
}

// A run of consecutive folded entries → one vendored disclosure card
// (closed), so an autonomous stretch collapses to a single line.
function renderRun(run, toolErrors) {
  const li = document.createElement('li');
  li.className = 'tr-run';
  // The entries this card holds, kept on the node so a later tick can merge
  // more into it and recompute the summary (#1050).
  li._trRun = run.slice();
  const d = document.createElement('details');
  d.className = 'card card--collapsible tr-group';
  tagKey(d, run[0]);
  d.innerHTML =
    '<summary class="collapse-summary">' +
      '<span class="collapse-main">' + icon('terminal') +
        '<h3 class="collapse-title"></h3>' +
        '<span class="collapse-count"></span>' +
      '</span>' +
      '<span class="collapse-chevron" aria-hidden="true">›</span>' +
    '</summary>' +
    '<div class="collapse-body tr-group-body"></div>';
  d.querySelector('.collapse-title').textContent = runLabel(run);
  // A failed call has to be visible while the group is still closed, and the
  // title ellipses at phone width — so the count is its own non-shrinking
  // element between title and item count rather than more title text.
  const failed = failedCount(run);
  if (failed) {
    const chip = document.createElement('span');
    chip.className = 'tr-fail-count';
    chip.textContent = failed + ' failed';
    d.querySelector('.collapse-main').insertBefore(chip, d.querySelector('.collapse-count'));
  }
  d.querySelector('.collapse-count').textContent = run.length + (run.length === 1 ? ' item' : ' items');
  const body = d.querySelector('.tr-group-body');
  run.forEach(function (e) { body.appendChild(renderItem(e, toolErrors)); });
  li.appendChild(d);
  return li;
}

// --- live refresh rendering (#1050) ---------------------------------------
//
// Every disclosure in a live region is tagged with the byte offset of the
// entry it renders, so an open card can be found again after the region is
// rebuilt. Offsets are stable identities: an entry keeps the offset of the
// line it started at for as long as that file is not rewritten.

function tagKey(el, entry) {
  if (entry && entry.offset != null) el.dataset.trKey = String(entry.offset);
}

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

// The summary line of a run card, recomputed from the entries it now holds —
// called on every merge, so a group that grew keeps an honest count.
function syncRunSummary(li) {
  const run = li._trRun || [];
  const d = li.querySelector('.tr-group');
  d.querySelector('.collapse-title').textContent = runLabel(run);
  d.querySelector('.collapse-count').textContent =
    run.length + (run.length === 1 ? ' item' : ' items');
  const failed = failedCount(run);
  let chip = d.querySelector('.tr-fail-count');
  if (failed && !chip) {
    chip = document.createElement('span');
    chip.className = 'tr-fail-count';
    d.querySelector('.collapse-main').insertBefore(chip, d.querySelector('.collapse-count'));
  }
  if (chip) {
    if (failed) chip.textContent = failed + ' failed';
    else chip.remove();
  }
}

// Append settled entries to the end of the list. Consecutive folded entries
// merge into the trailing run card when there is one: without this an
// autonomous stretch would fragment into one "1 tool call" card per tick
// instead of the single foldable group the pane is built around.
function appendSettled(entries, toolErrors) {
  entries.forEach(function (e) {
    const last = els.transcriptList.lastElementChild;
    if (!isTurn(e) && last && last.classList.contains('tr-run')) {
      const item = renderItem(e, toolErrors);
      tagKey(item, e);
      last.querySelector('.tr-group-body').appendChild(item);
      last._trRun.push(e);
      syncRunSummary(last);
      return;
    }
    els.transcriptList.appendChild(renderEntries([e], toolErrors));
  });
}

// Replace the provisional tail. `open` carries the disclosure state of
// whatever was there before, including cards that have since settled.
function renderPending(entries, toolErrors, open) {
  const frag = renderEntries(entries, toolErrors);
  const nodes = Array.prototype.slice.call(frag.children);
  nodes.forEach(function (li) { li.dataset.trPending = '1'; });
  restoreOpen(frag, open);
  els.transcriptList.appendChild(frag);
  return nodes;
}

// Drop the provisional tail, handing back what was open inside it.
function clearPending() {
  const nodes = (view && view.pendingNodes) || [];
  const open = harvestOpen(nodes);
  nodes.forEach(function (li) { li.remove(); });
  if (view) view.pendingNodes = [];
  return open;
}

function atBottom() {
  const box = els.transcriptBody;
  return box.scrollHeight - box.scrollTop - box.clientHeight <= STICK_PX;
}

// Apply one tick's worth of new content. The reader's position is the point:
// following the conversation keeps following it, and having scrolled up into
// history stays exactly put.
function applyLive(settled, pending, toolErrors) {
  const stick = atBottom();
  const open = clearPending();
  if (settled.length) {
    appendSettled(settled, toolErrors);
    restoreOpen(els.transcriptList, open);
    view.settled = view.settled.concat(settled);
  }
  view.pending = pending;
  view.pendingNodes = pending.length ? renderPending(pending, toolErrors, open) : [];
  view.entries = view.settled.concat(view.pending);
  if (stick) els.transcriptBody.scrollTop = els.transcriptBody.scrollHeight;
}

// Exported for the Life OS conversation viewer (#1119), which mounts this
// same renderer over a parsed capture rather than growing a second one
// (#979). Turn cards and run groups carry no reference to the live `view`,
// except a truncated turn's copy upgrade, and a capture's turns are never
// truncated.
export function renderEntries(entries, toolErrors) {
  const frag = document.createDocumentFragment();
  let run = [];
  function flush() {
    if (run.length) frag.appendChild(renderRun(run, toolErrors));
    run = [];
  }
  entries.forEach(function (e) {
    if (isTurn(e)) {
      flush();
      frag.appendChild(renderTurn(e));
    } else {
      run.push(e);
    }
  });
  flush();
  return frag;
}

function showState(text) {
  if (!els.transcriptState) return;
  els.transcriptState.textContent = text;
  els.transcriptState.hidden = false;
}

function hideState() {
  if (els.transcriptState) els.transcriptState.hidden = true;
}

// --- the live tick (#1050) -------------------------------------------------

// Is this view allowed to fetch right now? Every condition is re-checked on
// every tick rather than trusted from when the timer was set, because all
// three can change without this module being told (the overlay closes, the
// phone locks, the session exits).
function liveAllowed() {
  return !!view &&
    !view.ended &&
    !!els.terminalOverlay &&
    els.terminalOverlay.dataset.mode === 'chat' &&
    document.visibilityState === 'visible';
}

function stopLiveTimer() {
  if (view && view.liveTimer) {
    window.clearTimeout(view.liveTimer);
    view.liveTimer = null;
  }
}

function scheduleLive(delay) {
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
  if (!liveAllowed()) {
    stopLiveTimer();
    return;
  }
  // `ticking` as well as the timer: a tick clears its own timer before
  // awaiting, so without this a visibilitychange (or the post-send nudge)
  // landing mid-request would start a second one alongside it.
  if (!view.liveTimer && !view.ticking) liveTick();
}

// The source went unavailable. Say which, and stop only for the one reason
// that is actually terminal.
//
// `session_not_found` means the session exited: latch off, because nothing
// will bring it back and polling a dead session forever is the thing the
// acceptance criterion forbids. Every other reason is a condition that can
// clear on its own — most sharply `no_transcript`, which a *live* Claude
// session reports for as long as its hook row is deleted and the filesystem
// fallback can't name the file (#1023) — so those keep ticking, backed off,
// and recover without the reader having to tap Reload. Treating a transient
// unknown as an ending would be the same mistake in reverse as treating an
// unresolved check as a pass.
function liveUnavailable(reason) {
  showState(REASON_COPY[reason] || 'Transcript unavailable');
  view.reasonShown = true;
  if (reason === 'session_not_found') {
    view.ended = true;   // the list stays on screen: what was read is still worth reading
    stopLiveTimer();
    return;
  }
  view.backoff = Math.min(LIVE_BACKOFF_MAX_MS, (view.backoff || LIVE_POLL_MS) * 2);
  scheduleLive(view.backoff);
}

async function liveTick() {
  if (!view || !liveAllowed()) return;
  const target = view;
  const seq = view.seq;
  target.liveTimer = null;
  target.ticking = true;
  const q = new URLSearchParams({ after: String(target.tail) });
  if (target.size != null) q.set('size', String(target.size));
  let body;
  try {
    body = await jsonApi(
      '/api/claude-code/sessions/' + encodeURIComponent(target.session.session_id) +
        '/transcript?' + q.toString()
    );
  } catch (exc) {
    target.ticking = false;
    if (view !== target || view.seq !== seq) return;
    // Quietly, and slower each time: a tick is background work the reader
    // did not ask for, so a failing one must not toast over the pane the way
    // a tapped Reload does.
    target.backoff = Math.min(
      LIVE_BACKOFF_MAX_MS, (target.backoff || LIVE_POLL_MS) * 2
    );
    scheduleLive(target.backoff);
    return;
  }
  target.ticking = false;
  if (view !== target || view.seq !== seq) return;
  target.backoff = 0;
  if (!body.available) {
    liveUnavailable(body.reason);
    return;
  }
  if (body.reset) {
    // The file was rotated, or this view fell further behind than one
    // request may read. Either way the newest turns are what it wants.
    loadNewest();
    return;
  }
  // A tick that got an answer clears any reason line an earlier failed one
  // left on screen, so a condition that cleared by itself looks like it.
  if (target.reasonShown) {
    target.reasonShown = false;
    if (view.entries && view.entries.length) hideState();
  }
  if (body.changed) {
    target.tail = body.tail;
    target.size = body.size;
    applyLive(body.entries || [], body.pending || [], body.tool_errors || target.toolErrors);
  } else if (body.size != null) {
    target.size = body.size;
  }
  scheduleLive(LIVE_POLL_MS);
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
  view.ended = false;
  view.backoff = 0;
  view.reasonShown = false;
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
    showState(REASON_COPY[body.reason] || 'Transcript unavailable');
    view.ended = true;
    return;
  }
  // The page is one complete list, as it has always been. #1050 adds `tail`
  // — the offset where its newest, still-growing message begins — and keys
  // every entry from there on, so the live region can be told apart locally
  // and re-rendered later without disturbing anything above it.
  const all = body.entries || [];
  const cut = all.findIndex(function (e) {
    return e.offset != null && body.tail != null && e.offset >= body.tail;
  });
  const entries = cut < 0 ? all : all.slice(0, cut);
  const pending = cut < 0 ? [] : all.slice(cut);
  // Everything currently shown, kept for lastAssistantEntryFullText() (#988).
  // The live tick maintains it too, so read-aloud follows the conversation
  // instead of reading whatever was newest when the pane opened.
  view.settled = entries;
  view.pending = pending;
  view.entries = entries.concat(pending);
  view.tail = body.tail != null ? body.tail : 0;
  view.size = body.size;
  if (!view.entries.length && body.next_cursor == null) {
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
  els.transcriptList.appendChild(renderEntries(entries, view.toolErrors));
  view.pendingNodes = pending.length
    ? renderPending(pending, view.toolErrors, null)
    : [];
  view.cursor = body.next_cursor;
  els.transcriptOlder.hidden = view.cursor == null;
  els.transcriptBody.scrollTop = els.transcriptBody.scrollHeight;
  scheduleLive(LIVE_POLL_MS);
}

// The line a Load older that found no turn leaves behind (#1120), so the
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

async function loadOlder() {
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
  if (older.length) {
    els.transcriptList.insertBefore(
      renderEntries(older, toolErrors || view.toolErrors || 'reported'),
      els.transcriptList.firstChild,
    );
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
  // Keep what was on screen where it was: grow scrollTop by exactly the
  // height the older page added above it.
  box.scrollTop = topBefore + (box.scrollHeight - heightBefore);
}

// iOS shrinks the visual viewport for the software keyboard but not the
// layout viewport, so the fixed inset:0 overlay would keep the composer
// under the keyboard (the terminal's #135). Same fix as the terminal pane:
// pin the overlay to the visual viewport while the keyboard is up, and
// release it to the CSS when it hides or the pane closes. Only while the
// chat pane owns the overlay — in Terminal mode terminal.js's applySize
// does the pinning, and the two must never fight over the same styles.
export function pinChatToKeyboard() {
  const o = els.terminalOverlay;
  if (!o) return;
  const chatShowing = !!view && o.dataset.mode === 'chat';
  const vp = window.visualViewport;
  const kbH = (chatShowing && vp) ? keyboardOverlayHeight(window.innerHeight, vp.height) : null;
  if (kbH != null) {
    o.style.height = kbH + 'px';
    o.style.bottom = 'auto';
    o.style.top = Math.round(vp.offsetTop || 0) + 'px';
  } else if (chatShowing || !view) {
    // Release only what this pane may own: with Terminal showing, the
    // terminal's own pin (if any) stands.
    o.style.height = '';
    o.style.bottom = '';
    o.style.top = '';
  }
}

// ➤ Send from Chat (#983): the /input route for either kind. Resolves true to
// clear the draft; a failed send (502 not ingested / console failed, 409
// exited, 501 stale host) toasts and resolves false so the text stays to
// retry. A send that lands after the pane moved to another session toasts
// but leaves that session's composer and list alone.
async function sendFromChat(text) {
  if (!view) return false;
  const target = view;
  let verdict;
  try {
    verdict = await sendSessionMessage(target.session.session_id, text);
  } catch (exc) {
    apiFailToast('Send failed', exc);
    return false;
  }
  const outcome = sendOutcome(verdict);
  toast(outcome.text, outcome.kind, { icon: 'send-horizontal' });
  if (view !== target) return false;
  window.clearTimeout(target.refreshTimer);
  target.refreshTimer = window.setTimeout(function () {
    // One extra tick, not a reload: the sent turn arrives as an append like
    // any other, so the pane keeps its scroll position and open cards
    // (#1050). Brought forward rather than waiting out the current interval,
    // and still subject to the same gate — a send followed by a switch to
    // Terminal fetches nothing.
    if (view === target) scheduleLive(0);
  }, SENT_REFRESH_MS);
  return true;
}

function uploadFromChat(file) {
  return uploadSessionFile(view ? view.session.session_id : null, file);
}

// The live terminal socket of the session Chat is showing, if one is open.
// Opening straight into Chat connects nothing (#982), so the keys have no
// socket until Terminal has been shown once.
function liveTerminalFor(s) {
  const t = state.terminal;
  if (!t || t.sid !== s.session_id || !t.ws || t.ws.readyState !== WebSocket.OPEN) {
    return null;
  }
  return t;
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
  if (view) {
    window.clearTimeout(view.refreshTimer);
    stopLiveTimer();
  }
  view = {
    session: s, cursor: null, loading: false, seq: 0, refreshTimer: null,
    entries: null, toolErrors: 'reported',
    // Live refresh (#1050): `tail`/`size` are the forward cursor, `pending*`
    // the provisional tail currently rendered, `ended` latches when the
    // session is gone so no timer is ever rescheduled for it.
    tail: 0, size: null, liveTimer: null, ticking: false, backoff: 0,
    ended: false, reasonShown: false,
    settled: [], pending: [], pendingNodes: [],
  };
  groupsHidden = true;
  syncGroups();
  bindComposer(s);
  els.chatNote.hidden = s.kind !== 'remote';
  loadNewest();
}

export function closeChatPane() {
  if (view) {
    view.seq += 1;  // any in-flight page lands nowhere
    window.clearTimeout(view.refreshTimer);
    stopLiveTimer();  // a closed overlay fetches nothing (#1050)
  }
  view = null;
  if (!els.chatPane) return;
  els.transcriptList.innerHTML = '';
  els.chatNote.hidden = true;
  if (chatComposer) chatComposer.reset();
  pinChatToKeyboard();
  hideState();
}

export function wireChatPane() {
  if (!els.chatPane) return;
  syncGroups();
  els.transcriptOlder.addEventListener('click', function () { loadOlder(); });
  chatComposer = mountComposer(els.chatComposeBar, {
    placeholder: 'Message',
    send: sendFromChat,
    upload: uploadFromChat,
    keys: null,
    // Starting to talk silences any in-flight read-aloud (#190).
    onDictationStart: stopReading,
    // Chat is the surface a desktop keyboard actually types into, so it is
    // the one that takes the Ctrl/Cmd+Enter send (#1072). Plain Enter stays
    // a newline for everyone, phone included.
    sendOnModEnter: true,
  });
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', pinChatToKeyboard);
    window.visualViewport.addEventListener('scroll', pinChatToKeyboard);
  }
  // A backgrounded tab or a locked phone stops refreshing entirely; coming
  // back does one catch-up fetch, not a replay of every tick it missed
  // (#1050). One listener for the page, not one per opened session.
  document.addEventListener('visibilitychange', syncLiveRefresh);
  // Scrolling to the very top pulls the next older page in without a tap.
  els.transcriptBody.addEventListener('scroll', function () {
    if (view && view.cursor != null && !view.loading && els.transcriptBody.scrollTop <= 0) {
      loadOlder();
    }
  });
}
