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
 */

import { els, state } from './state.js';
import { apiFailToast, authHeaders, jsonApi, toast } from './api.js';
import { renderMarkdown } from './life-os.js';
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
const TRANSCRIPT_AGENTS = ['claude', 'codex', 'grok'];

export function hasTranscriptReader(s) {
  const agent = String((s && s.agent) || 'claude').toLowerCase();
  return TRANSCRIPT_AGENTS.indexOf(agent) !== -1;
}

// Conversation turns (user + assistant entries) per page — ~20 exchanges.
const PAGE_LIMIT = 40;

// After a send, reload the newest page once this long later so the sent
// turn shows up — the agent appends it to its history file only after it
// has consumed the input, which nothing here can observe. Chat has no
// periodic refresh (#982), so this is the only automatic reload.
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
  // The newest page's entries, kept for lastAssistantEntryFullText() (#988) —
  // the newest page always holds the most recent assistant turn, so an older
  // page prepended later never needs to touch this.
  view.entries = entries;
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
    if (view === target) loadNewest();
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
  if (view) window.clearTimeout(view.refreshTimer);
  view = { session: s, cursor: null, loading: false, seq: 0, refreshTimer: null, entries: null };
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
  });
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
