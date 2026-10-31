/* Transcript entry renderer.
 *
 * Turns a transcript's entries into list nodes: prompt bubbles, agent turns
 * with their replies, step lines and the items under them, sent-file cards,
 * per-step diffs and file chips. Two mounts use it: the session
 * overlay's Chat pane (session-transcript.js, which owns paging, live
 * refresh, the outbox cursor and the composer) and the Life OS conversation
 * viewer (life-os-viewer.js), which mounts a parsed capture through
 * `renderEntries`.
 *
 * Stateless about the Chat pane: whatever a node needs from the open session
 * comes in as a parameter. `opts.session` is that session (its id asks the
 * image, file, diff and entry routes; its `project_dir` shortens paths), or
 * null in the viewer, which has no session to ask; `opts.live` is the step
 * in progress, if any.
 *
 * This file draws the turns. Beside it: transcript-text.js (the small
 * building blocks every part shares), transcript-steps.js (step lines, the
 * items under them, an opened step's card), transcript-media.js (image
 * thumbnails and sent-file cards, and the media cache) and
 * transcript-chips.js (file chips in a reply). The question and plan cards
 * are transcript-decisions.js's; their entry points are what
 * `appendEntries` calls into.
 */

import { state } from '../state.js';
import { jsonApi, sessionRoute, toast } from '../api.js';
import { renderMarkdown } from '../markdown.js';
import { terminalJsonApi } from '../webauthn.js';
import { icon } from '../_vendored/icons/icons.js';
import { brandIcon } from '../dom-utils.js';
import { isPlan, isQuestion, renderPlan, renderQuestion } from './transcript-decisions.js';
import { chevron, firstLine, fmtTime, linkify } from './transcript-text.js';
import { isSentFile, renderSentFiles, thumbs } from './transcript-media.js';
import { renderItem, renderRun, syncRunSummary, tagKey } from './transcript-steps.js';

// Agents whose native history the server-side reader understands — the
// same set as the endpoint's flavour map (app/webapp/routers/
// session_transcript.py). Decides Chat availability without a probe; the
// pane still shows the server's own reason line if it disagrees. An absent
// agent field (a ?session= deep link's bare {session_id, name}) defaults
// to Claude, as the endpoint does.
export const TRANSCRIPT_AGENTS = ['claude', 'codex', 'grok', 'pi', 'antigravity', 'copilot'];

export function isTurn(e) {
  return (e.kind === 'user' || e.kind === 'assistant') && !e.sidechain;
}

// --- links open in the PC's browser --------------------------------
//
// A transcript link is a plain target=_blank anchor, so it opens in the
// browser hosting the page — on the PC that is Edge (the tray's default
// browser, or a session's mirror window). When this page runs on the PC
// itself, the tap goes to the webapp instead, which opens it in Chrome. A
// laptop or phone over the tailnet keeps the anchor: the PC's browser would
// open in front of nobody. Any failure falls back to the anchor.

export function onThePc() {
  return !!state.isMirrorWindow ||
    !!(state.status && state.status.terminal && state.status.terminal.reason === 'loopback');
}

export function openLinksOnThePc(root) {
  if (!root || root._trLinks) return;
  root._trLinks = true;
  root.addEventListener('click', function (ev) {
    const a = ev.target.closest && ev.target.closest('a[href]');
    if (!a || !root.contains(a) || !onThePc()) return;
    if (!/^https?:/i.test(a.href)) return;
    if (ev.defaultPrevented || ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
    ev.preventDefault();
    jsonApi('/api/open-url', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: a.href }),
    }).catch(function (exc) {
      console.warn('open on the PC failed, opening here', exc);
      window.open(a.href, '_blank', 'noopener');
    });
  });
}

function copyLabel(kind) {
  return kind === 'user' ? 'Prompt' : 'Reply';
}

// A turn's fragments, copied as they read: one blank line between them.
function joinTexts(texts) {
  return texts.join('\n\n');
}

// Tap the copy glyph on a prompt or an agent turn: writes the clipboard
// synchronously inside the tap gesture — iOS requires this, an `await`
// ahead of the first write loses the gesture and the copy silently fails on
// the one device this feature is for — with whatever text the turn already
// has. `entries` is the prompt, or every reply fragment of the turn.
// A capped fragment then fetches the uncapped one (the ``/transcript/
// entry`` route) and *visibly* upgrades the clipboard with a second toast:
// never a silent rewrite, since a paste in the gap between the two would
// hand back truncated text with no reason to suspect it, and the clipboard
// changing again afterwards would be worse. `sid` is the session the turn
// came from (null: none to ask, so a capped copy says it stayed capped).
async function copyTurn(entries, kind, sid) {
  const label = copyLabel(kind);
  try {
    await navigator.clipboard.writeText(joinTexts(entries.map(function (e) { return e.text || ''; })));
  } catch (exc) {
    toast('Clipboard unavailable — copy manually', 'error');
    return;
  }
  const capped = entries.filter(function (e) { return e.truncated; });
  if (!capped.length) {
    toast(label + ' copied', 'good', { icon: 'copy' });
    return;
  }
  toast(label + ' copied — loading the full text…', '', { icon: 'copy' });
  const fail = function () {
    toast(label + ' copy is truncated — the full text didn’t load', 'error', { icon: 'copy' });
  };
  if (!sid || capped.some(function (e) { return e.offset == null; })) {
    fail();
    return;
  }
  let texts;
  try {
    texts = await Promise.all(entries.map(async function (e) {
      if (!e.truncated) return e.text || '';
      return (await fetchEntryText(sid, e)).text;
    }));
  } catch (exc) {
    fail();
    return;
  }
  try {
    await navigator.clipboard.writeText(joinTexts(texts));
  } catch (exc) {
    fail();
    return;
  }
  toast('Full ' + label.toLowerCase() + ' copied', 'good', { icon: 'copy' });
}

// --- turns -----------------------------------------------------------
//
// A turn reads as one piece: the user's prompt is a right-aligned bubble, and
// everything the agent does until the next prompt (each reply fragment, the
// step lines between them, a question or plan card) sits in ONE block under
// one header: the agent's mark and name, the time, copy for the whole turn,
// and collapse — never one card per text fragment, each with its own
// "Agent · time" and copy. The prompt is plain text
// (pre-wrap); a reply goes through the same escape-first markdown renderer
// the Life OS doc browser uses — never raw HTML from a transcript.

// --- the rest of a capped turn ------------------------------------------
//
// A very long prompt or reply arrives capped. Where there is a way to ask for
// the rest (a session, and the offset the ``/transcript/entry`` route
// reads) the note is a "Show the rest" button that swaps the block's text for
// the full one in place; otherwise it stays the plain note. One helper for
// every capped block, so the prompt, the reply and the plan card (whose text
// that route cannot serve) share their wording and their failure.
//
// What was fetched is remembered, keyed by session and offset, so the live
// tick that rebuilds the provisional tail and a Reload transcript draw the
// block expanded again. The capped text it replaced is kept alongside and must
// still match: an offset is only an identity while the source file is not
// rewritten, and a stale full text on a different entry would be worse than
// the note.

export const TRUNC_COPY = '(truncated — open the terminal for the rest)';
const TRUNC_REST = 'Show the rest';
const TRUNC_FAILED = 'Couldn’t load the rest — try again';
const EXPANDED_MAX = 24;
const expandedEntries = new Map();

function expandedHit(e, sid) {
  if (!sid || !e || e.offset == null) return null;
  const hit = expandedEntries.get(sid + ':' + e.offset);
  return hit && hit.capped === e.text ? hit : null;
}

// The text a block draws: the full text once fetched, else the entry's own.
export function shownText(e, sid) {
  const hit = expandedHit(e, sid);
  return hit ? hit.full : e.text || '';
}

function rememberExpanded(e, sid, body) {
  const key = sid + ':' + e.offset;
  expandedEntries.delete(key);
  expandedEntries.set(key, { capped: e.text, full: body.text || '', truncated: !!body.truncated });
  if (expandedEntries.size > EXPANDED_MAX) expandedEntries.delete(expandedEntries.keys().next().value);
}

function plainTruncNote() {
  const note = document.createElement('div');
  note.className = 'tr-trunc';
  note.textContent = TRUNC_COPY;
  return note;
}

// The one-entry fetch behind both "Show the rest" and a capped copy. Throws
// when the entry is unavailable (a rotated source, a bad offset).
async function fetchEntryText(sid, e) {
  const body = await terminalJsonApi(
    sessionRoute(sid, '/transcript/entry?offset=' + encodeURIComponent(e.offset))
  );
  if (!body || !body.available) throw new Error('entry unavailable');
  return body;
}

// The note under a capped block, or null when there is nothing to say (already
// expanded in full). `opts` is `{ entry, sid, paint }`: `paint(fullText)`
// redraws the block's text, and without it, a session or an offset the note
// stays plain. No `opts` at all is the plain note.
export function truncControl(opts) {
  const { entry, sid, paint } = opts || {};
  const hit = expandedHit(entry, sid);
  if (hit) return hit.truncated ? plainTruncNote() : null;
  if (!paint || !sid || !entry || entry.offset == null) return plainTruncNote();
  const note = document.createElement('div');
  note.className = 'tr-trunc';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'button-ghost tr-trunc-more';
  btn.textContent = TRUNC_REST;
  const status = document.createElement('div');
  status.className = 'tr-trunc-status';
  status.setAttribute('role', 'status');
  btn.addEventListener('click', async function (ev) {
    ev.preventDefault();
    ev.stopPropagation();
    btn.disabled = true;
    btn.textContent = 'Loading…';
    status.textContent = '';
    let body = null;
    try {
      body = await fetchEntryText(sid, entry);
    } catch (exc) {
      body = null;
    }
    if (!body) {
      btn.disabled = false;
      btn.textContent = TRUNC_REST;
      status.textContent = TRUNC_FAILED;
      return;
    }
    rememberExpanded(entry, sid, body);
    paint(body.text || '');
    if (body.truncated) note.replaceWith(plainTruncNote());
    else note.remove();
  });
  note.append(btn, status);
  return note;
}

// The agent's short name and brand mark. Every agent with a transcript
// reader has a mark in the brand sprite (index.html); the ids are its
// lower-case names. A mount that doesn't know the agent says "Agent".
function agentName(agent) {
  if (TRANSCRIPT_AGENTS.indexOf(agent) === -1) return 'Agent';
  return agent.charAt(0).toUpperCase() + agent.slice(1);
}

function agentMark(agent) {
  return TRANSCRIPT_AGENTS.indexOf(agent) === -1
    ? icon('messages-square')
    : brandIcon(agent, 'tr-turn-brand');
}

// The copy glyph. It stops the tap from reaching a <summary> it sits in,
// which would otherwise toggle the turn (same guard as every other control
// living in one — dom-utils.js, jobs.js, …). `entriesOf` is asked at tap
// time, so a turn that grew since it rendered copies what it holds now.
// `sid` is the session it came from, for a capped copy's upgrade.
function copyButton(kind, entriesOf, sid) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'icon-button tr-turn-copy';
  btn.setAttribute('aria-label', 'Copy ' + copyLabel(kind).toLowerCase());
  btn.innerHTML = icon('copy');
  btn.addEventListener('click', function (ev) {
    ev.preventDefault();
    ev.stopPropagation();
    copyTurn(entriesOf(), kind, sid);
  });
  return btn;
}

// A collapsed turn's one line: the text's first line, or "Image" for an image alone.
function textHint(e) {
  return firstLine(e.text, 80) || (e.images && e.images.length ? 'Image' : '');
}

// The prompt bubble. Open, it is the bubble with the time, copy and a fold
// chevron under it; folded, the <summary> takes its place as a one-line
// bubble that a tap opens again. The summary is hidden while open rather than
// carrying the controls, so the bubble has no header row.
function renderPrompt(e, session) {
  const li = document.createElement('li');
  li.className = 'tr-turn-item tr-prompt-item';
  const d = document.createElement('details');
  d.className = 'tr-turn tr-user';
  d.open = true;
  tagKey(d, e, 'prompt');
  const s = document.createElement('summary');
  s.className = 'tr-bubble tr-prompt-folded';
  const hint = document.createElement('span');
  hint.className = 'tr-turn-hint';
  hint.textContent = textHint(e);
  s.appendChild(hint);
  s.appendChild(chevron());
  d.appendChild(s);
  const bubble = document.createElement('div');
  bubble.className = 'tr-bubble';
  const sid = session ? session.session_id : null;
  const text = document.createElement('div');
  text.className = 'tr-text';
  text.textContent = shownText(e, sid);
  linkify(text);
  bubble.appendChild(text);
  const shots = thumbs(e.images, session);
  if (shots) bubble.appendChild(shots);
  const more = e.truncated ? truncControl({
    entry: e, sid: sid,
    paint: function (full) {
      text.textContent = full;
      linkify(text);
    },
  }) : null;
  if (more) bubble.appendChild(more);
  d.appendChild(bubble);
  const foot = document.createElement('div');
  foot.className = 'tr-prompt-foot';
  const when = document.createElement('span');
  when.className = 'tr-meta';
  when.textContent = fmtTime(e.timestamp);
  when.hidden = !when.textContent;
  foot.appendChild(when);
  foot.appendChild(copyButton('user', function () { return [e]; }, sid));
  const fold = document.createElement('button');
  fold.type = 'button';
  fold.className = 'icon-button tr-prompt-fold';
  fold.setAttribute('aria-label', 'Collapse prompt');
  fold.appendChild(chevron());
  fold.addEventListener('click', function () { d.open = false; });
  foot.appendChild(fold);
  d.appendChild(foot);
  li.appendChild(d);
  return li;
}

// One reply fragment inside an agent turn. Not a disclosure of its own: the
// turn collapses as a whole.
function renderReply(e, session) {
  const li = document.createElement('li');
  li.className = 'tr-reply';
  li._trEntry = e;
  const body = document.createElement('div');
  body.className = 'tr-text tr-md';
  const sid = session ? session.session_id : null;
  body.innerHTML = renderMarkdown(shownText(e, sid));
  linkify(body);
  li.appendChild(body);
  const shots = thumbs(e.images, session);
  if (shots) li.appendChild(shots);
  const more = e.truncated ? truncControl({
    entry: e, sid: sid,
    paint: function (full) {
      body.innerHTML = renderMarkdown(full);
      linkify(body);
      // The file chips went with the old markup; the next tick draws them again.
      body._trChipSig = null;
    },
  }) : null;
  if (more) li.appendChild(more);
  return li;
}

export function isAgentTurn(node) {
  return !!node && node.nodeType === 1 && node.classList.contains('tr-agent-item');
}

function turnReplies(li) {
  return Array.prototype.filter.call(li._trParts.children, function (n) {
    return n.classList.contains('tr-reply');
  }).map(function (n) { return n._trEntry; });
}

// The agent's turn: one <details> whose summary is the header and whose body
// is the ordered list of its parts. `first` is the entry it starts at — the
// header's time and the disclosure's key. `edited` (the Chat pane only)
// lets the turn grow an Edited N files card once it is over. `sid`
// is the session the turn came from, for its copy.
function renderAgentTurn(first, agent, edited, sid) {
  const li = document.createElement('li');
  li.className = 'tr-turn-item tr-agent-item';
  li._trFirst = first;
  li._trEditable = !!edited;
  const d = document.createElement('details');
  d.className = 'tr-turn tr-assistant';
  d.open = true;
  tagKey(d, first, 'turn');
  const s = document.createElement('summary');
  s.className = 'tr-turn-summary';
  const mark = document.createElement('span');
  mark.className = 'tr-turn-mark';
  mark.innerHTML = agentMark(agent);
  s.appendChild(mark);
  const who = document.createElement('span');
  who.className = 'tr-turn-who';
  who.textContent = agentName(agent);
  s.appendChild(who);
  const when = document.createElement('span');
  when.className = 'tr-meta';
  s.appendChild(when);
  const hint = document.createElement('span');
  hint.className = 'tr-turn-hint';
  s.appendChild(hint);
  const copyBtn = copyButton('assistant', function () { return turnReplies(li); }, sid);
  s.appendChild(copyBtn);
  s.appendChild(chevron());
  d.appendChild(s);
  const parts = document.createElement('ol');
  parts.className = 'tr-turn-parts';
  d.appendChild(parts);
  li.appendChild(d);
  li._trParts = parts;
  li._trWhen = when;
  li._trHint = hint;
  li._trCopy = copyBtn;
  return li;
}

// The header, recomputed from the parts the turn now holds — after every
// merge, a pending tail coming and going, or an older page joining it.
// A turn with no reply and no decision card is "silent": with the steps
// hidden it would be a header over nothing, so it hides with them. Its step
// lines are recomputed here too, since a run's duration ends at whatever
// part follows it. `live` is the step in progress, if any.
export function syncTurnHead(li, live) {
  li._trParts.querySelectorAll(':scope > .tr-run').forEach(function (run) { syncRunSummary(run, live); });
  const replies = turnReplies(li);
  const time = fmtTime(li._trFirst && li._trFirst.timestamp);
  li._trWhen.textContent = time ? '· ' + time : '';
  let hint = replies.length ? textHint(replies[0]) : '';
  if (!hint) {
    const title = li._trParts.querySelector('.tr-file-name, .tr-step-label');
    hint = title ? title.textContent : '';
  }
  li._trHint.textContent = hint;
  li._trCopy.hidden = !replies.length;
  markSilent(li);
}

// With the steps hidden, a turn with no reply, no decision card and no Edited
// card would be a header over nothing, so it hides with them. The Edited card
// keeps it: the work still has a trace.
export function markSilent(li) {
  const quiet = !turnReplies(li).length && !li._trEdited &&
    !li._trParts.querySelector(':scope > .tr-ask-item, :scope > .tr-plan-item, :scope > .tr-file-item');
  li.classList.toggle('tr-turn-silent', quiet);
}
// The one place entries become list nodes: appends `entries` to
// `container` (the list itself, or a fragment), continuing whatever it ends
// with. A prompt starts a bubble; anything else joins the trailing agent
// turn, or opens one; a folded entry joins the turn's trailing step line, or
// opens one. So a page renders, a live tick appends and the Life OS
// viewer mounts through the same rules, and an autonomous stretch arriving a
// tick at a time still folds into one turn and one run rather than a card
// per tick.
//
// `opts.pending` renders the provisional tail: its nodes are returned so the
// next tick can drop them, and it never merges into a settled step line (that
// would have to be unpicked). A turn it continues keeps its header; the parts
// it adds are what gets dropped.
//
// What the open session supplies comes in on `opts` too: `session` (null in
// the viewer) and `live`, the step in progress.
export function appendEntries(container, entries, opts) {
  const created = [];
  const ownTurns = new Set();
  const ownRuns = new Set();
  const touchedTurns = new Set();
  function addPart(turn, node) {
    turn._trParts.appendChild(node);
    if (!ownTurns.has(turn)) created.push(node);
  }
  entries.forEach(function (e) {
    if (isTurn(e) && e.kind === 'user') {
      const li = renderPrompt(e, opts.session);
      container.appendChild(li);
      created.push(li);
      return;
    }
    let turn = container.lastElementChild;
    if (!isAgentTurn(turn)) {
      turn = renderAgentTurn(e, opts.agent, opts.edited, opts.session ? opts.session.session_id : null);
      container.appendChild(turn);
      created.push(turn);
      ownTurns.add(turn);
    }
    touchedTurns.add(turn);
    if (isTurn(e)) {
      addPart(turn, renderReply(e, opts.session));
    } else if (isQuestion(e)) {
      addPart(turn, renderQuestion(e, opts.toolErrors, opts.answering));
    } else if (isPlan(e)) {
      addPart(turn, renderPlan(e, opts.toolErrors, opts.answering));
    } else if (isSentFile(e)) {
      addPart(turn, renderSentFiles(e, opts.session));
    } else {
      const last = turn._trParts.lastElementChild;
      if (last && last.classList.contains('tr-run') && (!opts.pending || ownRuns.has(last))) {
        last.querySelector('.tr-step-items').appendChild(renderItem(e, opts));
        last._trRun.push(e);
      } else {
        const run = renderRun([e], opts);
        ownRuns.add(run);
        addPart(turn, run);
      }
    }
  });
  // A turn's head sync recomputes its step lines too.
  touchedTurns.forEach(function (turn) { syncTurnHead(turn, opts.live); });
  return created;
}

// The Life OS conversation viewer's mount: this same renderer over a
// parsed capture rather than a second one. It has no session, so
// nothing here asks a route: no thumbnails, no file bytes, no full diff, and
// a truncated turn's copy upgrade has nowhere to go (a capture's turns are
// never truncated).
//
// `answering` is the Chat pane's own marker that a question card may
// go live; the viewer passes nothing, so its cards stay read-only history.
// `agent` names whose turns these are (an agent id); without one they read
// "Agent".
export function renderEntries(entries, toolErrors, answering, agent) {
  const frag = document.createDocumentFragment();
  appendEntries(frag, entries, {
    toolErrors: toolErrors, answering: answering,
    agent: agent ? String(agent).toLowerCase() : null, pending: false,
  });
  return frag;
}
