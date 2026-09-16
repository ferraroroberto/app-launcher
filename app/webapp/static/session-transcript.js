/* Session transcript — the Chat pane of the session overlay (#953, #982).
 *
 * The whole conversation of one live Coding session, chat-style: typed
 * user prompts and assistant replies expanded, everything else — tool calls
 * with their results, thinking, harness plumbing, sub-agent traffic —
 * folded per run into one disclosure that expands item by item. Backed by
 * the paginated `/api/claude-code/sessions/{sid}/transcript` endpoint
 * (Tailscale + passkey gated, like the Board drawer's /exchange): the
 * newest page loads first, older pages prepend on "Load older" or when the
 * list is scrolled to its top. Read-only for a full-control session — the
 * terminal pane stays its input surface. A detached session whose agent
 * takes console input gets a composer docked under the list (#975), sending
 * through the same helpers as the gear menu's Send dialog (#967).
 *
 * Since #982 this is one pane of #terminalOverlay, not its own overlay:
 * session-overlay.js opens/closes it and flips the overlay's data-mode;
 * the bar's ⋮ menu (terminal-bar.js) carries the pane's Show-tool-calls and
 * Reload actions. Switching panes never reloads: the pages and fold state
 * loaded here survive a trip through Terminal mode.
 */

import { els } from './state.js';
import { apiFailToast, jsonApi, toast } from './api.js';
import { renderMarkdown } from './life-os.js';
import { canSendToDetached, sendOutcomeText, sendSessionMessage } from './sessions.js';
import { keyboardOverlayHeight } from './terminal.js';
import { icon } from './_vendored/icons/icons.js';

// Agents whose native history the server-side reader understands — the
// same pair as the endpoint's flavour map (app/webapp/routers/
// session_transcript.py). Decides Chat availability without a probe; the
// pane still shows the server's own reason line if it disagrees. An absent
// agent field (a ?session= deep link's bare {session_id, name}) defaults
// to Claude, as the endpoint does.
const TRANSCRIPT_AGENTS = ['claude', 'codex'];

export function hasTranscriptReader(s) {
  const agent = String((s && s.agent) || 'claude').toLowerCase();
  return TRANSCRIPT_AGENTS.indexOf(agent) !== -1;
}

// Conversation turns (user + assistant entries) per page — ~20 exchanges.
const PAGE_LIMIT = 40;

// After a send, reload the newest page once this long later so the sent
// turn shows up — the agent appends it to its history file only after the
// console has consumed the keystrokes, which nothing here can observe.
const SENT_REFRESH_MS = 3000;
// The composer grows with its text up to this many lines, then scrolls.
const COMPOSE_MAX_ROWS = 5;

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
  d.open = true;
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

// ⋮ menu — "Reload transcript": the newest page again (the only refresh
// path besides the post-send one; a mode switch deliberately never reloads).
export function reloadNewest() {
  if (view) loadNewest();
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

function growComposeInput() {
  const ta = els.transcriptComposeInput;
  ta.style.height = 'auto';
  const lineHeight = parseFloat(getComputedStyle(ta).lineHeight) || 20;
  ta.style.height = Math.min(ta.scrollHeight, COMPOSE_MAX_ROWS * lineHeight + 22) + 'px';
}

function resetCompose() {
  els.transcriptComposeInput.value = '';
  els.transcriptComposeInput.style.height = '';
  els.transcriptComposeSend.disabled = false;
}

async function sendFromTranscript(ev) {
  ev.preventDefault();
  if (!view || view.sending) return;
  const text = els.transcriptComposeInput.value.trim();
  if (!text) return;
  const target = view;
  target.sending = true;
  els.transcriptComposeSend.disabled = true;
  let verdict;
  try {
    verdict = await sendSessionMessage(target.session.session_id, text);
  } catch (exc) {
    // The text stays in the box to retry — a failed send (501 stale host,
    // 502 unattached console) is never reported as sent.
    apiFailToast('Send failed', exc);
    return;
  } finally {
    target.sending = false;
    if (view === target) els.transcriptComposeSend.disabled = false;
  }
  toast(sendOutcomeText(verdict), '', { icon: 'send-horizontal' });
  // Closed, or reopened on another session, while the send was in flight:
  // that view's composer and list aren't ours to touch.
  if (view !== target) return;
  resetCompose();
  window.clearTimeout(target.refreshTimer);
  target.refreshTimer = window.setTimeout(function () {
    if (view === target) loadNewest();
  }, SENT_REFRESH_MS);
}

// Load `s` into the chat pane. The overlay shell (visibility, title, body
// lock) and the data-mode flip are session-overlay.js's; this only owns the
// pane's content. The inline "Detached session — no terminal" note explains
// the disabled Terminal segment (mockup screen 6).
export function openChatPane(s) {
  if (!els.chatPane) return;
  if (view) window.clearTimeout(view.refreshTimer);
  view = { session: s, cursor: null, loading: false, seq: 0, sending: false, refreshTimer: null };
  groupsHidden = true;
  syncGroups();
  resetCompose();
  els.chatNote.hidden = s.kind !== 'remote';
  els.transcriptCompose.hidden = !canSendToDetached(s);
  loadNewest();
}

export function closeChatPane() {
  if (view) {
    view.seq += 1;  // any in-flight page lands nowhere
    window.clearTimeout(view.refreshTimer);
  }
  view = null;
  if (!els.chatPane) return;
  els.transcriptList.innerHTML = '';
  els.transcriptCompose.hidden = true;
  els.chatNote.hidden = true;
  resetCompose();
  pinChatToKeyboard();
  hideState();
}

export function wireChatPane() {
  if (!els.chatPane) return;
  syncGroups();
  els.transcriptOlder.addEventListener('click', function () { loadOlder(); });
  els.transcriptCompose.addEventListener('submit', sendFromTranscript);
  els.transcriptComposeInput.addEventListener('input', growComposeInput);
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', pinChatToKeyboard);
    window.visualViewport.addEventListener('scroll', pinChatToKeyboard);
  }
  // Scrolling to the very top pulls the next older page in without a tap.
  els.transcriptBody.addEventListener('scroll', function () {
    if (view && view.cursor != null && !view.loading && els.transcriptBody.scrollTop <= 0) {
      loadOlder();
    }
  });
}
