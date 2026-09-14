/* Session transcript overlay (issue #953).
 *
 * The whole conversation of one live Coding session, chat-style: typed
 * user prompts and assistant replies expanded, everything else — tool calls
 * with their results, thinking, harness plumbing, sub-agent traffic —
 * folded per run into one disclosure that expands item by item. Backed by
 * the paginated `/api/claude-code/sessions/{sid}/transcript` endpoint
 * (Tailscale + passkey gated, like the Board drawer's /exchange): the
 * newest page loads first, older pages prepend on "Load older" or when the
 * list is scrolled to its top. Read-only — the terminal stays the input
 * surface.
 */

import { els } from './state.js';
import { apiFailToast, jsonApi, toast } from './api.js';
import { renderMarkdown } from './life-os.js';
import { sessionTitle } from './sessions.js';
import { icon } from './_vendored/icons/icons.js';

// Conversation turns (user + assistant entries) per page — ~20 exchanges.
const PAGE_LIMIT = 40;

// One line per server-side reason, so "nothing there" never reads like
// "couldn't read it" (and vice versa).
const REASON_COPY = {
  session_not_found: 'This session is no longer running',
  unsupported_agent: 'Transcript not supported for this agent yet',
  no_transcript: 'No transcript found for this session',
  read_failed: 'Couldn’t read the transcript',
};

const KIND_ICON = {
  tool_call: 'terminal',
  tool_result: 'terminal',
  thinking: 'sparkle',
  system: 'plug',
  user: 'messages-square',
  assistant: 'messages-square',
};

// null when the overlay is closed, else the session it shows plus the
// cursor for the next older page. `seq` guards a slow response from a
// previous open/refresh landing in a newer view.
let view = null;
// Whether user + agent turns render open. The bar's toggle flips every
// turn on screen and sets the default for pages loaded afterwards; tool
// groups are never touched by it — they stay closed until tapped.
let turnsOpen = true;
// Whether the folded tool-call / system groups are hidden from the list.
// Hidden by default: the view opens as a plain user ↔ agent exchange; the
// bar's eye toggle reveals the groups, still folded, between the turns.
let groupsHidden = true;

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

// A turn: a collapsible card, open by default (or per the bar's toggle),
// whose summary is the meta line plus — only while closed — the first
// line of the text. The user's own prompt is plain text (pre-wrap); the
// agent's reply goes through the same escape-first markdown renderer the
// Life OS doc browser uses — never raw HTML from a transcript.
function renderTurn(e) {
  const li = document.createElement('li');
  li.className = 'tr-turn-item';
  const d = document.createElement('details');
  d.className = 'tr-turn tr-' + e.kind;
  d.open = turnsOpen;
  const s = document.createElement('summary');
  s.className = 'tr-turn-summary';
  s.appendChild(meta(e.kind === 'user' ? 'You' : 'Agent', e.timestamp));
  const hint = document.createElement('span');
  hint.className = 'tr-turn-hint';
  hint.textContent = firstLine(e.text, 80);
  s.appendChild(hint);
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

function syncToggleAll() {
  const btn = els.transcriptToggleAll;
  if (!btn) return;
  const label = turnsOpen ? 'Collapse all turns' : 'Expand all turns';
  btn.innerHTML = icon(turnsOpen ? 'chevrons-down-up' : 'chevrons-up-down');
  btn.title = label;
  btn.setAttribute('aria-label', label);
  btn.setAttribute('aria-pressed', turnsOpen ? 'false' : 'true');
}

function toggleAllTurns() {
  turnsOpen = !turnsOpen;
  els.transcriptList.querySelectorAll('details.tr-turn').forEach(function (d) {
    d.open = turnsOpen;
  });
  syncToggleAll();
}

function syncToggleGroups() {
  const btn = els.transcriptToggleGroups;
  if (!btn) return;
  const label = groupsHidden ? 'Show tool calls and system entries' : 'Hide tool calls and system entries';
  btn.innerHTML = icon(groupsHidden ? 'eye-off' : 'eye');
  btn.title = label;
  btn.setAttribute('aria-label', label);
  btn.setAttribute('aria-pressed', groupsHidden ? 'true' : 'false');
  els.transcriptList.classList.toggle('tr-hide-groups', groupsHidden);
}

function toggleGroups() {
  groupsHidden = !groupsHidden;
  syncToggleGroups();
}

// One folded item inside a run group — its own <details>, so a single
// tool call can be opened without expanding its siblings.
function renderItem(e) {
  const d = document.createElement('details');
  d.className = 'tr-item tr-item-' + e.kind;
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

// A run of consecutive folded entries → one vendored disclosure card
// (closed), so an autonomous stretch collapses to a single line.
function renderRun(run) {
  const li = document.createElement('li');
  li.className = 'tr-run';
  const d = document.createElement('details');
  d.className = 'card card--collapsible tr-group';
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
  d.querySelector('.collapse-count').textContent = run.length + (run.length === 1 ? ' item' : ' items');
  const body = d.querySelector('.tr-group-body');
  run.forEach(function (e) { body.appendChild(renderItem(e)); });
  li.appendChild(d);
  return li;
}

function renderEntries(entries) {
  const frag = document.createDocumentFragment();
  let run = [];
  function flush() {
    if (run.length) frag.appendChild(renderRun(run));
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
  els.transcriptList.innerHTML = '';
  els.transcriptOlder.hidden = true;
  view.cursor = null;
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
    return;
  }
  const entries = body.entries || [];
  if (!entries.length && body.next_cursor == null) {
    showState('Nothing in the transcript yet');
    return;
  }
  hideState();
  els.transcriptList.appendChild(renderEntries(entries));
  view.cursor = body.next_cursor;
  els.transcriptOlder.hidden = view.cursor == null;
  els.transcriptBody.scrollTop = els.transcriptBody.scrollHeight;
}

async function loadOlder() {
  if (!view || view.loading || view.cursor == null) return;
  const seq = view.seq;
  view.loading = true;
  els.transcriptOlder.disabled = true;
  const box = els.transcriptBody;
  const heightBefore = box.scrollHeight;
  const topBefore = box.scrollTop;
  let body;
  try {
    body = await fetchPage(view.cursor);
  } catch (exc) {
    if (view && view.seq === seq) apiFailToast('Load older failed', exc);
    return;
  } finally {
    if (view && view.seq === seq) {
      view.loading = false;
      els.transcriptOlder.disabled = false;
    }
  }
  if (!view || view.seq !== seq) return;
  if (!body.available) {
    toast(REASON_COPY[body.reason] || 'Transcript unavailable', 'bad');
    view.cursor = null;
    els.transcriptOlder.hidden = true;
    return;
  }
  els.transcriptList.insertBefore(renderEntries(body.entries || []), els.transcriptList.firstChild);
  view.cursor = body.next_cursor;
  els.transcriptOlder.hidden = view.cursor == null;
  // Keep what was on screen where it was: grow scrollTop by exactly the
  // height the older page added above it.
  box.scrollTop = topBefore + (box.scrollHeight - heightBefore);
}

export function openTranscript(s) {
  if (!els.transcriptOverlay) return;
  view = { session: s, cursor: null, loading: false, seq: 0 };
  turnsOpen = true;
  groupsHidden = true;
  syncToggleAll();
  syncToggleGroups();
  els.transcriptTitle.textContent = sessionTitle(s);
  els.transcriptOverlay.hidden = false;
  loadNewest();
}

export function closeTranscript() {
  if (view) view.seq += 1;  // any in-flight page lands nowhere
  view = null;
  if (!els.transcriptOverlay) return;
  els.transcriptOverlay.hidden = true;
  els.transcriptList.innerHTML = '';
  hideState();
}

export function wireTranscript() {
  if (!els.transcriptOverlay) return;
  els.transcriptClose.addEventListener('click', closeTranscript);
  els.transcriptRefresh.addEventListener('click', function () { loadNewest(); });
  els.transcriptToggleAll.addEventListener('click', toggleAllTurns);
  els.transcriptToggleGroups.addEventListener('click', toggleGroups);
  syncToggleAll();
  syncToggleGroups();
  els.transcriptOlder.addEventListener('click', function () { loadOlder(); });
  // Scrolling to the very top pulls the next older page in without a tap.
  els.transcriptBody.addEventListener('scroll', function () {
    if (view && view.cursor != null && !view.loading && els.transcriptBody.scrollTop <= 0) {
      loadOlder();
    }
  });
}
