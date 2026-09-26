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
 * Claude Code's AskUserQuestion renders as a question card (#1149), never
 * folded. The pending one answers through its own route (/answer), not
 * /input: the picker needs raw keystrokes, and /input frames text as a
 * paste — see the card's section below.
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
import { api, apiFailToast, authHeaders, jsonApi, toast } from './api.js';
import { renderMarkdown } from './markdown.js';
import { detachedSendRefused, sendOutcome, sendSessionMessage } from './sessions.js';
import { keyboardOverlayHeight } from './terminal.js';
import { mountComposer } from './composer.js';
import { uploadSessionFile } from './terminal-compose.js';
import { openImageLightbox } from './system-map.js';
import { stopReading } from './terminal-readaloud.js';
import { voiceDictationAvailable } from './voice.js';
import { ensureTerminalToken } from './webauthn.js';
import { icon } from './_vendored/icons/icons.js';
import { mountScrollerPill, scrollerIsAway } from './latest-pill.js';

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

// The last segment of a POSIX or Windows path.
function baseName(path) {
  const parts = String(path || '').split(/[\\/]/);
  return parts[parts.length - 1] || String(path || '');
}

// An edit's line counts, "+4 −0" (a true minus sign, as the Claude Code app).
function lineDelta(a) {
  return '+' + (a.added || 0) + ' \u2212' + (a.removed || 0);
}

function term(el) {
  el.classList.add('tr-term');
  return el;
}

// --- image thumbnails (#1265) ---------------------------------------------
//
// An entry lists where its images are ({offset, n}); the bytes come from the
// image route, fetched through the authenticated API (an <img src> can carry
// neither the bearer nor the passkey token) once the thumbnail scrolls into
// view, and kept as object URLs for as long as this session's Chat is open —
// so a re-render or a live tick never fetches the same image twice. Only the
// Chat pane has a session to ask; the Life OS viewer's captures hold no image
// data and keep their `[image]` text.

const thumbUrls = new Map();
let thumbObserver = null;

function imageUrl(sid, ref) {
  return '/api/claude-code/sessions/' + encodeURIComponent(sid) +
    '/transcript/image?offset=' + ref.offset + '&n=' + ref.n;
}

async function loadThumb(btn) {
  const sid = btn.dataset.sid;
  const url = imageUrl(sid, btn._trImage);
  let objectUrl = thumbUrls.get(url);
  if (!objectUrl) {
    try {
      const tt = await ensureTerminalToken();
      const res = await api(url, { headers: authHeaders({ terminalToken: tt }) });
      if (!res.ok) throw new Error('status ' + res.status);
      objectUrl = URL.createObjectURL(await res.blob());
    } catch (exc) {
      console.warn('transcript image failed', exc);
      btn.classList.add('tr-thumb-failed');
      return;
    }
    // A view that closed meanwhile has already dropped its URLs.
    if (!view || view.session.session_id !== sid) {
      URL.revokeObjectURL(objectUrl);
      return;
    }
    thumbUrls.set(url, objectUrl);
  }
  btn.querySelector('img').src = objectUrl;
  btn.disabled = false;
}

function observeThumb(btn) {
  if (!('IntersectionObserver' in window)) {
    loadThumb(btn);
    return;
  }
  if (!thumbObserver) {
    thumbObserver = new IntersectionObserver(function (seen) {
      seen.forEach(function (hit) {
        if (!hit.isIntersecting) return;
        thumbObserver.unobserve(hit.target);
        loadThumb(hit.target);
      });
    }, { rootMargin: '200px' });
  }
  thumbObserver.observe(btn);
}

// Small lazy thumbnails for an entry's images; a tap opens the full size in
// the app's image overlay. Null outside the Chat pane (no session to ask).
function thumbs(refs) {
  if (!refs || !refs.length || !view) return null;
  const wrap = document.createElement('div');
  wrap.className = 'tr-thumbs';
  refs.forEach(function (ref, i) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'tr-thumb';
    btn.disabled = true;
    btn.dataset.sid = view.session.session_id;
    btn._trImage = ref;
    btn.setAttribute('aria-label', 'Open image ' + (i + 1) + ' full size');
    const img = document.createElement('img');
    img.alt = 'Image ' + (i + 1);
    img.loading = 'lazy';
    img.decoding = 'async';
    btn.appendChild(img);
    btn.addEventListener('click', function (ev) {
      ev.stopPropagation();
      if (img.src) openImageLightbox(img.src, img.alt);
    });
    wrap.appendChild(btn);
    observeThumb(btn);
  });
  return wrap;
}

function dropThumbs() {
  thumbUrls.forEach(function (objectUrl) { URL.revokeObjectURL(objectUrl); });
  thumbUrls.clear();
}

// --- links open in the PC's browser (#1274) --------------------------------
//
// A transcript link is a plain target=_blank anchor, so it opens in the
// browser hosting the page — on the PC that is Edge (the tray's default
// browser, or a session's mirror window). When this page runs on the PC
// itself, the tap goes to the webapp instead, which opens it in Chrome. A
// laptop or phone over the tailnet keeps the anchor: the PC's browser would
// open in front of nobody. Any failure falls back to the anchor.

function onThePc() {
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
  hint.textContent = firstLine(e.text, 80) || (e.images && e.images.length ? 'Image' : '');
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
  const shots = thumbs(e.images);
  if (shots) d.appendChild(shots);
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
    // Plain words where the server knows the tool (#1266): the command's
    // first line, the edited file's name with its +N −M, or the file read.
    const a = e.action;
    if (a && a.verb === 'ran') {
      name.textContent = firstLine(a.command, 80);
    } else if (a && (a.verb === 'edited' || a.verb === 'wrote')) {
      name.textContent = baseName(a.path);
      hint.textContent = lineDelta(a);
    } else if (a && a.verb === 'read') {
      name.textContent = baseName(a.path);
    } else {
      name.textContent = e.name || 'tool';
      hint.textContent = e.summary || '';
    }
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
    // A command opens as a terminal: `$ command`, then its output (#1266).
    const ran = e.action && e.action.verb === 'ran';
    if (ran) body.appendChild(term(pre('$ ' + e.action.command, false)));
    else if (e.summary) body.appendChild(pre(e.summary, false));
    if (e.result != null) {
      const out = pre(e.result, e.result_truncated);
      body.appendChild(ran ? term(out) : out);
      const shots = thumbs(e.result_images);
      if (shots) body.appendChild(shots);
    } else {
      const none = document.createElement('div');
      none.className = 'tr-trunc';
      none.textContent = 'no result on this page';
      body.appendChild(none);
    }
  } else {
    body.appendChild(pre(e.text, e.truncated));
    const shots = thumbs(e.images);
    if (shots) body.appendChild(shots);
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

// --- AskUserQuestion card (#1149) -----------------------------------------
//
// Claude Code's multiple-choice prompt renders as a card of its own, never
// folded into a tool-call run: it is the agent talking to the user, so it
// stays visible with tool calls hidden, like a turn. The server forwards the
// call's questions (and, once answered, `answers`: question → label, labels
// joined by ", " for a multi-select, or the typed text) — see
// src/ask_user_question.py.
//
// The card is interactive only in the Chat pane (`answering` passed to
// renderEntries), only for the session's current pending question — the
// newest one with no result and nothing after it but plumbing — and only
// while that holds at the moment of the tap. The server checks again before
// it types a key. Every other card, and every card the Life OS viewer
// renders, is history: no live control at all.

const ASK_NOT_WAITING = 'This question is no longer waiting for an answer';

// The marker renderEntries takes from the Chat pane (and only from it).
const CHAT_ANSWERING = { chat: true };

export function isQuestion(e) {
  return !!e && e.kind === 'tool_call' && e.name === 'AskUserQuestion' && !e.sidechain &&
    Array.isArray(e.questions) && e.questions.length > 0;
}

// The call's outcome: its paired result, or — when the call settled on an
// earlier tick than its result — a standalone tool_result carrying its id.
// null while nothing has come back.
function questionOutcome(e, entries) {
  if (e.result != null) {
    return { answers: e.answers || null, error: e.error === true };
  }
  const r = (entries || []).find(function (x) {
    return x.kind === 'tool_result' && x.tool_use_id && x.tool_use_id === e.call_id;
  });
  return r ? { answers: r.answers || null, error: r.error === true } : null;
}

// One question's answer out of the `answers` map. Keyed by question text;
// a lone question takes the lone value even if the texts drifted.
function answerFor(q, answers, total) {
  if (!answers) return null;
  if (Object.prototype.hasOwnProperty.call(answers, q.question)) return answers[q.question];
  const values = Object.keys(answers).map(function (k) { return answers[k]; });
  return total === 1 && values.length === 1 ? values[0] : null;
}

function labelPicked(q, answer, label) {
  if (answer == null) return false;
  if (!q.multiSelect) return answer === label;
  return (', ' + answer + ', ').indexOf(', ' + label + ', ') !== -1;
}

// One card per call. `answering` is the Chat pane's hook; without it (the
// Life OS viewer) the card never grows a live control.
function renderQuestion(e, toolErrors, answering) {
  const li = document.createElement('li');
  li.className = 'tr-ask-item';
  li._trAsk = e;
  li._trAnswering = answering || null;
  if (e.call_id) li.dataset.callId = e.call_id;
  const card = document.createElement('div');
  card.className = 'tr-ask';
  tagKey(card, e);
  if (e.error === true) card.classList.add('tr-item-failed');
  const head = document.createElement('div');
  head.className = 'tr-ask-head';
  head.innerHTML = icon('messages-square');
  head.appendChild(meta('Agent asks', e.timestamp));
  if (e.error === true) {
    const chip = document.createElement('span');
    chip.className = 'tr-fail-chip';
    chip.textContent = 'failed';
    head.appendChild(chip);
  }
  card.appendChild(head);
  const simple = e.questions.length === 1 && !e.questions[0].multiSelect;
  e.questions.forEach(function (q, qi) {
    const sec = document.createElement('section');
    sec.className = 'tr-ask-q';
    sec.dataset.q = String(qi);
    if (q.header) {
      const chip = document.createElement('span');
      chip.className = 'tr-ask-header';
      chip.textContent = q.header;
      sec.appendChild(chip);
    }
    const text = document.createElement('p');
    text.className = 'tr-ask-question';
    text.textContent = q.question || '';
    sec.appendChild(text);
    if (q.multiSelect) {
      const hint = document.createElement('p');
      hint.className = 'tr-ask-hint';
      hint.textContent = 'Pick any number';
      sec.appendChild(hint);
    }
    const list = document.createElement('div');
    list.className = 'tr-ask-options';
    list.setAttribute('role', 'group');
    list.setAttribute('aria-label', q.header || q.question || 'Options');
    q.options.forEach(function (opt, oi) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'tr-ask-opt';
      b.dataset.n = String(oi + 1);
      b.disabled = true;
      const num = document.createElement('span');
      num.className = 'tr-ask-num';
      num.textContent = String(oi + 1);
      const body = document.createElement('span');
      body.className = 'tr-ask-opt-body';
      const label = document.createElement('span');
      label.className = 'tr-ask-label';
      label.textContent = opt.label;
      body.appendChild(label);
      if (opt.description) {
        const desc = document.createElement('span');
        desc.className = 'tr-ask-desc';
        desc.textContent = opt.description;
        body.appendChild(desc);
      }
      const mark = document.createElement('span');
      mark.className = 'tr-ask-mark';
      mark.setAttribute('aria-hidden', 'true');
      mark.innerHTML = icon('circle-check');
      b.appendChild(num);
      b.appendChild(body);
      b.appendChild(mark);
      b.addEventListener('click', function () { onOptionTap(li, qi, oi + 1); });
      list.appendChild(b);
    });
    sec.appendChild(list);
    // "Type something" — single-select only: typing into a multi-select's
    // text row was not probed (src/ask_user_question.py).
    if (!q.multiSelect) {
      const other = document.createElement('div');
      other.className = 'tr-ask-other';
      other.hidden = true;
      const input = document.createElement('input');
      input.type = 'text';
      input.className = 'tr-ask-input';
      input.placeholder = 'Or type an answer';
      input.setAttribute('aria-label', 'Type an answer to: ' + (q.question || 'the question'));
      input.maxLength = 500;
      input.addEventListener('input', function () { onTextInput(li, qi, input.value); });
      other.appendChild(input);
      if (simple) {
        const send = document.createElement('button');
        send.type = 'button';
        send.className = 'button-tint tr-ask-send';
        send.textContent = 'Send';
        send.disabled = true;
        send.addEventListener('click', function () {
          const t = input.value.trim();
          if (t) submitAnswer(li, [{ text: t }]);
        });
        input.addEventListener('keydown', function (ev) {
          if (ev.key === 'Enter' && input.value.trim()) {
            ev.preventDefault();
            submitAnswer(li, [{ text: input.value.trim() }]);
          }
        });
        other.appendChild(send);
      }
      sec.appendChild(other);
    }
    const typed = document.createElement('p');
    typed.className = 'tr-ask-typed';
    typed.hidden = true;
    sec.appendChild(typed);
    card.appendChild(sec);
  });
  const status = document.createElement('p');
  status.className = 'tr-ask-status';
  status.setAttribute('role', 'status');
  card.appendChild(status);
  if (!simple) {
    const submit = document.createElement('button');
    submit.type = 'button';
    submit.className = 'button-primary tr-ask-submit';
    submit.textContent = 'Submit answers';
    submit.hidden = true;
    submit.addEventListener('click', function () {
      const draft = askDraft(li);
      if (draftComplete(li._trAsk, draft)) submitAnswer(li, draft);
    });
    card.appendChild(submit);
  }
  const note = toolOutcomeNote(e, toolErrors);
  if (note) {
    const unknown = document.createElement('div');
    unknown.className = 'tr-outcome-unknown';
    unknown.textContent = note;
    card.appendChild(unknown);
  }
  li.appendChild(card);
  // A first paint from the entry alone, so a card is right even where
  // nothing ever calls syncDecisionCards() (the Life OS viewer).
  const out = questionOutcome(e, null);
  paintQuestion(li, out ? (out.answers ? 'answered' : 'closed') : 'history', out);
  return li;
}

// The live card's in-progress picks, one slot per question, kept on the
// view so a live-refresh rebuild of the card doesn't drop them.
function askDraft(li) {
  const e = li._trAsk;
  if (!view) return [];
  view.askDrafts = view.askDrafts || {};
  if (!view.askDrafts[e.call_id]) {
    view.askDrafts[e.call_id] = e.questions.map(function () { return null; });
  }
  return view.askDrafts[e.call_id];
}

function draftComplete(e, draft) {
  return e.questions.every(function (q, i) {
    const a = draft[i];
    if (!a) return false;
    if (q.multiSelect) return Array.isArray(a.options) && a.options.length > 0;
    return a.option != null || !!(a.text && a.text.trim());
  });
}

function onOptionTap(li, qi, n) {
  if (li.dataset.mode !== 'live') return;
  const e = li._trAsk;
  if (e.questions.length === 1 && !e.questions[0].multiSelect) {
    submitAnswer(li, [{ option: n }]);
    return;
  }
  const draft = askDraft(li);
  if (e.questions[qi].multiSelect) {
    const picks = (draft[qi] && draft[qi].options) ? draft[qi].options.slice() : [];
    const at = picks.indexOf(n);
    if (at === -1) picks.push(n);
    else picks.splice(at, 1);
    draft[qi] = picks.length ? { options: picks } : null;
  } else {
    draft[qi] = { option: n };
    const input = li.querySelector('.tr-ask-q[data-q="' + qi + '"] .tr-ask-input');
    if (input) input.value = '';
  }
  paintDraft(li);
}

function onTextInput(li, qi, value) {
  if (li.dataset.mode !== 'live') return;
  const e = li._trAsk;
  const send = li.querySelector('.tr-ask-send');
  if (send) send.disabled = !value.trim();
  if (e.questions.length === 1 && !e.questions[0].multiSelect) return;
  const draft = askDraft(li);
  if (value.trim()) draft[qi] = { text: value };
  else if (draft[qi] && draft[qi].text != null) draft[qi] = null;
  paintDraft(li);
}

// Reflect the draft on a live card: pressed options, typed text, Submit.
function paintDraft(li) {
  const e = li._trAsk;
  if (e.questions.length === 1 && !e.questions[0].multiSelect) {
    // A tap sends, so there is no draft — only Send follows the text field.
    const input = li.querySelector('.tr-ask-input');
    const send = li.querySelector('.tr-ask-send');
    if (send) send.disabled = !(input && input.value.trim());
    return;
  }
  const draft = askDraft(li);
  e.questions.forEach(function (q, qi) {
    const a = draft[qi];
    li.querySelectorAll('.tr-ask-q[data-q="' + qi + '"] .tr-ask-opt').forEach(function (b) {
      const n = Number(b.dataset.n);
      const on = !!a && (q.multiSelect ? (a.options || []).indexOf(n) !== -1 : a.option === n);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    const input = li.querySelector('.tr-ask-q[data-q="' + qi + '"] .tr-ask-input');
    if (input && a && a.text != null && input.value !== a.text) input.value = a.text;
  });
  const submit = li.querySelector('.tr-ask-submit');
  if (submit) submit.disabled = !draftComplete(e, draft);
}

const ASK_STATUS = {
  live: '',
  sent: 'Answer sent: waiting for the agent to take it',
  closed: 'Not answered: the question was dismissed',
  stale: ASK_NOT_WAITING,
  history: 'No answer recorded here',
  console: 'Answer it in the PC console: sending is off for this agent',
};

// Put one card into `mode`. Idempotent per mode, so the live tick can call
// it on every card without disturbing a half-made pick.
function paintQuestion(li, mode, out, statusText) {
  if (li.dataset.mode === mode && !statusText) return;
  li.dataset.mode = mode;
  const e = li._trAsk;
  const live = mode === 'live';
  const simple = e.questions.length === 1 && !e.questions[0].multiSelect;
  const answers = out && out.answers;
  e.questions.forEach(function (q, qi) {
    const sec = li.querySelector('.tr-ask-q[data-q="' + qi + '"]');
    const answer = answerFor(q, answers, e.questions.length);
    let matched = false;
    sec.querySelectorAll('.tr-ask-opt').forEach(function (b) {
      b.disabled = !live;
      const picked = mode === 'answered' && labelPicked(q, answer, q.options[Number(b.dataset.n) - 1].label);
      matched = matched || picked;
      b.classList.toggle('tr-ask-opt--picked', picked);
      // Toggle semantics only where a tap selects rather than sends.
      if (live && !simple) b.setAttribute('aria-pressed', 'false');
      else b.removeAttribute('aria-pressed');
    });
    const other = sec.querySelector('.tr-ask-other');
    if (other) other.hidden = !live;
    const typed = sec.querySelector('.tr-ask-typed');
    const freeText = mode === 'answered' && answer && !matched;
    typed.hidden = !freeText;
    typed.textContent = freeText ? 'Typed answer: “' + answer + '”' : '';
  });
  const submit = li.querySelector('.tr-ask-submit');
  if (submit) submit.hidden = !live;
  const status = li.querySelector('.tr-ask-status');
  let text = statusText != null ? statusText : ASK_STATUS[mode];
  if (mode === 'answered') text = 'Answered';
  if (live) text = simple ? 'Tap an answer to send it to the agent' : 'Pick an answer for each question, then submit';
  status.textContent = text || '';
  if (live) paintDraft(li);
}

// The call id of the session's pending decision (a question, #1149, or a
// plan, #1151), or null: the newest such call in what is loaded, still
// without a result, with no turn, tool call or thinking after it (a later
// one means the agent moved on). Results and harness plumbing after it don't
// count — they are not the agent continuing.
function currentDecisionId() {
  if (!view || view.ended || !view.entries) return null;
  const list = view.entries;
  for (let i = list.length - 1; i >= 0; i--) {
    const e = list[i];
    if (e.sidechain || e.kind === 'system' || e.kind === 'tool_result') continue;
    if (isDecisionCard(e)) return questionOutcome(e, list) ? null : (e.call_id || null);
    return null;
  }
  return null;
}

// Re-derive every card's mode from what is loaded — after a page load, a
// live tick, a Load older, and a send.
function syncDecisionCards() {
  if (!view) return;
  const liveId = currentDecisionId();
  const s = view.session;
  els.transcriptList.querySelectorAll('.tr-ask-item').forEach(function (li) {
    const e = li._trAsk;
    const out = questionOutcome(e, view.entries);
    if (out) {
      if (view.askSent) delete view.askSent[e.call_id];
      paintQuestion(li, out.answers ? 'answered' : 'closed', out);
      return;
    }
    const sent = view.askSent && view.askSent[e.call_id];
    if (sent) {
      paintQuestion(li, 'sent', null, sent === true ? null : sent);
      return;
    }
    const refused = view.askRefused && view.askRefused[e.call_id];
    if (!li._trAnswering || !e.call_id || e.call_id !== liveId || refused) {
      paintQuestion(li, li._trAnswering ? 'stale' : 'history', null);
      return;
    }
    paintQuestion(li, detachedSendRefused(s) ? 'console' : 'live', null);
  });
  els.transcriptList.querySelectorAll('.tr-plan-item').forEach(function (li) {
    const e = li._trPlan;
    const out = planOutcome(e, view.entries);
    if (out) paintPlan(li, out.outcome, out);
    else if (li._trAnswering && e.call_id && e.call_id === liveId) {
      paintPlan(li, 'waiting', null, planWhere(s));
    } else paintPlan(li, li._trAnswering ? 'stale' : 'history', null);
  });
}

// Send the picks. Re-checks "still the pending question" at the tap — the
// live refresh can trail reality by a tick — then leaves the card locked in
// "sent" until the agent's result lands and closes it.
async function submitAnswer(li, answers) {
  if (!view || li.dataset.mode !== 'live') return;
  const target = view;
  const e = li._trAsk;
  if (currentDecisionId() !== e.call_id) {
    toast(ASK_NOT_WAITING, 'bad');
    syncDecisionCards();
    return;
  }
  target.askSent = target.askSent || {};
  target.askSent[e.call_id] = true;
  syncDecisionCards();
  try {
    const tt = await ensureTerminalToken();
    await jsonApi(
      '/api/claude-code/sessions/' + encodeURIComponent(target.session.session_id) + '/answer',
      {
        method: 'POST',
        headers: authHeaders({ terminalToken: tt, contentType: 'application/json' }),
        body: JSON.stringify({ tool_use_id: e.call_id, answers: answers }),
      }
    );
  } catch (exc) {
    if (exc && exc.status === 502 && /partly sent/.test(exc.message || '')) {
      // Some keys went in: the picker is mid-answer, so the card must not
      // offer a retry that would type over it. It says where to look.
      target.askSent[e.call_id] = exc.message;
      toast(exc.message, 'error');
    } else {
      delete target.askSent[e.call_id];
      if (exc && exc.status === 409) {
        toast(exc.message || ASK_NOT_WAITING, 'bad');
        target.askRefused = target.askRefused || {};
        target.askRefused[e.call_id] = true;
      } else {
        apiFailToast('Answer failed', exc);
      }
    }
    if (view === target) syncDecisionCards();
    return;
  }
  toast('Answer sent', 'good', { icon: 'send-horizontal' });
  if (view !== target) return;
  window.clearTimeout(target.refreshTimer);
  target.refreshTimer = window.setTimeout(function () {
    if (view === target) scheduleLive(0);
  }, SENT_REFRESH_MS);
}

// --- ExitPlanMode card (#1151) --------------------------------------------
//
// The plan the agent wants approved, through the same escape-first markdown
// renderer as a reply, and how it was answered: approved (and whether it was
// edited first), sent back with the user's feedback, declined, or never
// shown (the agent called the tool outside plan mode). The server reads
// those off the result (src/plan_review.py).
//
// The card itself is read-only. Claude Code can hold the pending call back
// from the transcript until it is answered (measured: 31 s with the picker
// up and the call not on disk), so the transcript cannot say "this plan is
// waiting" reliably, and the picker's first option changes with the
// session's permission mode ("auto-accept edits" vs "switch to BYPASS
// PERMISSIONS"). Answering is the plan panel's below, which reads the
// terminal's screen instead; a waiting card says where to answer.

export function isPlan(e) {
  return !!e && e.kind === 'tool_call' && e.name === 'ExitPlanMode' && !e.sidechain &&
    typeof e.plan === 'string';
}

// A card of its own beside the turns — never folded into a tool-call run.
export function isDecisionCard(e) {
  return isQuestion(e) || isPlan(e);
}

function planFields(x) {
  return {
    outcome: x.plan_outcome || (x.error === true ? 'declined' : 'answered'),
    feedback: x.plan_feedback || '',
    edited: x.plan_edited === true,
  };
}

// The plan's answer: its paired result, or a standalone one carrying its id
// (the call settled on an earlier tick). null while nothing has come back.
function planOutcome(e, entries) {
  if (e.result != null) return planFields(e);
  const r = (entries || []).find(function (x) {
    return x.kind === 'tool_result' && x.tool_use_id && x.tool_use_id === e.call_id;
  });
  return r ? planFields(r) : null;
}

const PLAN_STATUS = {
  approved: 'Approved',
  sent_back: 'Sent back with feedback',
  declined: 'Not approved',
  not_shown: 'Never shown: the agent was not in plan mode',
  answered: 'Answered',
  stale: 'No longer waiting for an answer',
  history: 'No answer recorded here',
};

function renderPlan(e, toolErrors, answering) {
  const li = document.createElement('li');
  li.className = 'tr-plan-item';
  li._trPlan = e;
  li._trAnswering = answering || null;
  if (e.call_id) li.dataset.callId = e.call_id;
  const card = document.createElement('div');
  card.className = 'tr-ask tr-plan';
  tagKey(card, e);
  const head = document.createElement('div');
  head.className = 'tr-ask-head';
  head.innerHTML = icon('file-text');
  head.appendChild(meta('Plan for approval', e.timestamp));
  card.appendChild(head);
  const body = document.createElement('div');
  body.className = 'tr-md tr-plan-body';
  body.innerHTML = renderMarkdown(e.plan || '');
  linkify(body);
  card.appendChild(body);
  if (e.plan_truncated) {
    const mark = document.createElement('div');
    mark.className = 'tr-trunc';
    mark.textContent = '(truncated — open the terminal for the rest)';
    card.appendChild(mark);
  }
  const feedback = document.createElement('p');
  feedback.className = 'tr-ask-typed tr-plan-feedback';
  feedback.hidden = true;
  card.appendChild(feedback);
  const status = document.createElement('p');
  status.className = 'tr-ask-status';
  status.setAttribute('role', 'status');
  card.appendChild(status);
  const note = toolOutcomeNote(e, toolErrors);
  if (note) {
    const unknown = document.createElement('div');
    unknown.className = 'tr-outcome-unknown';
    unknown.textContent = note;
    card.appendChild(unknown);
  }
  li.appendChild(card);
  // First paint from the entry alone (the Life OS viewer never syncs).
  const out = planOutcome(e, null);
  paintPlan(li, out ? out.outcome : 'history', out);
  return li;
}

// `where` says where to answer a waiting plan.
function paintPlan(li, mode, out, where) {
  li.dataset.mode = mode;
  const card = li.querySelector('.tr-plan');
  // Only a real tool error is a failure: a declined or sent-back plan is
  // the user's answer, even though the harness records it as an error.
  const failed = mode === 'not_shown';
  card.classList.toggle('tr-item-failed', failed);
  let chip = card.querySelector('.tr-ask-head .tr-fail-chip');
  if (failed && !chip) {
    chip = document.createElement('span');
    chip.className = 'tr-fail-chip';
    chip.textContent = 'failed';
    card.querySelector('.tr-ask-head').appendChild(chip);
  } else if (!failed && chip) {
    chip.remove();
  }
  const feedback = card.querySelector('.tr-plan-feedback');
  const said = mode === 'sent_back' && out && out.feedback;
  feedback.hidden = !said;
  feedback.textContent = said ? 'Your feedback: “' + out.feedback + '”' : '';
  let text = PLAN_STATUS[mode] || '';
  if (mode === 'approved' && out && out.edited) text = 'Approved after your edits';
  if (mode === 'waiting') text = 'Waiting for your answer: ' + (where || 'answer it in the terminal');
  card.querySelector('.tr-ask-status').textContent = text;
}

// --- The plan panel: answering from the terminal's screen (#1151) ---------
//
// The server reads the plan picker off the terminal (src/plan_picker.py):
// the session's PTY capture rendered at the PTY's own size. While it is up,
// the panel below the transcript shows the options exactly as the screen
// lists them, and a tap sends the digit and the label the reader saw. The
// server reads the screen again before typing and refuses (409) if that
// digit no longer carries that label, so a tap can never approve into a
// permission mode the reader didn't see. Full-control Claude sessions only:
// a detached one has no screen here, and its card says to use the console.
//
// Polled with the live tick, so it stops with it (overlay closed, phone
// locked, session gone).

// How long a sent answer keeps the panel locked while the screen still
// shows the same picker. Past it the keys evidently didn't take, and the
// panel offers the options again (the server re-checks every tap anyway).
const PICKER_SENT_HOLD_MS = 10000;

function pickerAllowed(s) {
  return !!s && String(s.agent || 'claude').toLowerCase() === 'claude' && s.kind !== 'remote';
}

function planWhere(s) {
  if (s && s.kind === 'remote') return 'answer it in the PC console';
  if (view && view.picker) return 'answer it below';
  return 'Chat can’t see the plan picker on the terminal, so answer it there';
}

// The pending plan card the transcript already shows, if any: the panel
// then leaves the plan out rather than showing it twice.
function pendingPlanShown() {
  const id = currentDecisionId();
  return !!id && (view.entries || []).some(function (e) { return isPlan(e) && e.call_id === id; });
}

async function pollPicker() {
  if (!view || view.pickerBusy || !pickerAllowed(view.session) || !liveAllowed()) return;
  const target = view;
  target.pickerBusy = true;
  let body = null;
  try {
    body = await jsonApi(
      '/api/claude-code/sessions/' + encodeURIComponent(target.session.session_id) + '/plan-picker'
    );
  } catch (exc) {
    // Quietly, like a failed live tick: the next one asks again. Until a
    // read succeeds nothing is confirmed, so nothing is offered.
    body = null;
  }
  target.pickerBusy = false;
  if (view !== target) return;
  applyPicker(body && body.showing ? body : null);
}

function applyPicker(p) {
  const box = els.transcriptPlanLive;
  if (!box || !view) return;
  view.picker = p;
  const sig = p ? JSON.stringify([p.options, p.cursor, p.answerable, p.plan]) : '';
  const held = view.pickerSent && Date.now() - view.pickerSent < PICKER_SENT_HOLD_MS;
  if (sig === view.pickerSig && (held || !view.pickerSent)) {
    syncDecisionCards();
    return;
  }
  view.pickerSig = sig;
  view.pickerSent = null;
  const stick = atBottom();
  box.innerHTML = '';
  box.hidden = !p;
  if (p) box.appendChild(renderPicker(p));
  syncDecisionCards();
  if (stick) els.transcriptBody.scrollTop = els.transcriptBody.scrollHeight;
}

function clearPicker() {
  const box = els.transcriptPlanLive;
  if (box) {
    box.innerHTML = '';
    box.hidden = true;
  }
}

function renderPicker(p) {
  const card = document.createElement('div');
  card.className = 'tr-ask tr-plan tr-plan-live-card';
  const head = document.createElement('div');
  head.className = 'tr-ask-head';
  head.innerHTML = icon('file-text');
  head.appendChild(meta('Plan waiting for your answer', null));
  card.appendChild(head);
  if (p.plan && !pendingPlanShown()) {
    if (p.plan_source === 'file') {
      const body = document.createElement('div');
      body.className = 'tr-md tr-plan-body';
      body.innerHTML = renderMarkdown(p.plan);
      linkify(body);
      card.appendChild(body);
    } else {
      // As the terminal shows it: wrapped to the terminal's width.
      const body = pre(p.plan, false);
      body.classList.add('tr-plan-body');
      card.appendChild(body);
    }
    if (p.plan_truncated) {
      const mark = document.createElement('div');
      mark.className = 'tr-trunc';
      mark.textContent = '(truncated — open the terminal for the rest)';
      card.appendChild(mark);
    }
  }
  const list = document.createElement('div');
  list.className = 'tr-ask-options';
  list.setAttribute('role', 'group');
  list.setAttribute('aria-label', 'Answer the plan');
  p.options.forEach(function (o) {
    if (o.kind === 'feedback') {
      list.appendChild(renderPickerFeedback(p, o));
      return;
    }
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'tr-ask-opt';
    b.dataset.n = String(o.n);
    b.disabled = !(p.answerable && o.kind === 'approve');
    const num = document.createElement('span');
    num.className = 'tr-ask-num';
    num.textContent = String(o.n);
    const body = document.createElement('span');
    body.className = 'tr-ask-opt-body';
    const label = document.createElement('span');
    label.className = 'tr-ask-label';
    label.textContent = o.label;
    body.appendChild(label);
    b.appendChild(num);
    b.appendChild(body);
    b.addEventListener('click', function () { sendPlanAnswer(o, null); });
    list.appendChild(b);
  });
  card.appendChild(list);
  const status = document.createElement('p');
  status.className = 'tr-ask-status';
  status.setAttribute('role', 'status');
  status.textContent = p.answerable
    ? 'Read from the terminal just now. A tap sends that answer to Claude.'
    : 'The terminal is in the middle of an answer: finish it there.';
  card.appendChild(status);
  return card;
}

// "Tell Claude what to change": its number and label like the others, and
// the feedback typed here rather than on the terminal.
function renderPickerFeedback(p, o) {
  const sec = document.createElement('div');
  sec.className = 'tr-plan-feedback-row';
  const row = document.createElement('div');
  row.className = 'tr-ask-opt';
  const num = document.createElement('span');
  num.className = 'tr-ask-num';
  num.textContent = String(o.n);
  const label = document.createElement('span');
  label.className = 'tr-ask-label';
  label.textContent = o.label;
  row.appendChild(num);
  row.appendChild(label);
  sec.appendChild(row);
  const other = document.createElement('div');
  other.className = 'tr-ask-other';
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'tr-ask-input';
  input.placeholder = 'What should change?';
  input.setAttribute('aria-label', o.label);
  input.maxLength = 500;
  input.disabled = !p.answerable;
  const send = document.createElement('button');
  send.type = 'button';
  send.className = 'button-tint tr-ask-send';
  send.textContent = 'Send back';
  send.disabled = true;
  const go = function () {
    const t = input.value.trim();
    if (t && p.answerable) sendPlanAnswer(o, t);
  };
  input.addEventListener('input', function () { send.disabled = !input.value.trim() || !p.answerable; });
  input.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter' && input.value.trim()) {
      ev.preventDefault();
      go();
    }
  });
  send.addEventListener('click', go);
  other.appendChild(input);
  other.appendChild(send);
  sec.appendChild(other);
  return sec;
}

function lockPicker(text) {
  const box = els.transcriptPlanLive;
  if (!box) return;
  box.querySelectorAll('button, input').forEach(function (el) { el.disabled = true; });
  const status = box.querySelector('.tr-ask-status');
  if (status) status.textContent = text;
}

async function sendPlanAnswer(o, feedback) {
  if (!view || !view.picker || view.pickerSent) return;
  const target = view;
  target.pickerSent = Date.now();
  lockPicker('Sending…');
  try {
    const tt = await ensureTerminalToken();
    await jsonApi(
      '/api/claude-code/sessions/' + encodeURIComponent(target.session.session_id) + '/plan-answer',
      {
        method: 'POST',
        headers: authHeaders({ terminalToken: tt, contentType: 'application/json' }),
        body: JSON.stringify({ option: o.n, label: o.label, feedback: feedback }),
      }
    );
  } catch (exc) {
    if (view !== target) return;
    if (exc && exc.status === 502 && /partly sent/.test(exc.message || '')) {
      // Some keys went in: the picker is mid-answer, so no retry is offered
      // until the screen changes. The message says where to look.
      lockPicker(exc.message);
      toast(exc.message, 'error');
      return;
    }
    if (exc && exc.status === 409) toast(exc.message || 'The plan is no longer waiting', 'bad');
    else apiFailToast('Answer failed', exc);
    // Nothing was typed: re-render from a fresh read of the screen.
    target.pickerSent = null;
    target.pickerSig = null;
    pollPicker();
    return;
  }
  toast('Answer sent to the terminal', 'good', { icon: 'send-horizontal' });
  if (view !== target) return;
  lockPicker(feedback ? 'Sent back: waiting for Claude to revise the plan' : 'Sent: waiting for Claude');
  window.clearTimeout(target.refreshTimer);
  target.refreshTimer = window.setTimeout(function () {
    if (view === target) scheduleLive(0);
  }, SENT_REFRESH_MS);
}

// A run's title in plain words (#1266): "Ran 2 commands, edited main.js
// +4 −0, read 3 files". Built from each call's server-side `action`; a call
// without one is counted as before, and a run with none reads as it did.
function runLabel(run) {
  let ran = 0;
  let other = 0;
  let thinking = 0;
  let system = 0;
  const edited = {};
  const read = {};
  run.forEach(function (e) {
    const a = e.kind === 'tool_call' ? e.action : null;
    if (a && a.verb === 'ran') {
      ran += 1;
    } else if (a && (a.verb === 'edited' || a.verb === 'wrote')) {
      const sum = edited[a.path] || (edited[a.path] = { added: 0, removed: 0 });
      sum.added += a.added || 0;
      sum.removed += a.removed || 0;
    } else if (a && a.verb === 'read') {
      read[a.path] = true;
    } else if (e.kind === 'tool_call' || e.kind === 'tool_result') {
      other += 1;
    } else if (e.kind === 'thinking') {
      thinking += 1;
    } else {
      system += 1;
    }
  });
  const parts = [];
  if (ran) parts.push(ran === 1 ? 'ran a command' : 'ran ' + ran + ' commands');
  const edits = Object.keys(edited);
  if (edits.length) {
    const total = edits.reduce(function (s, path) {
      return { added: s.added + edited[path].added, removed: s.removed + edited[path].removed };
    }, { added: 0, removed: 0 });
    parts.push('edited ' + (edits.length === 1 ? baseName(edits[0]) : edits.length + ' files') +
      ' ' + lineDelta(total));
  }
  const reads = Object.keys(read);
  if (reads.length) parts.push('read ' + (reads.length === 1 ? baseName(reads[0]) : reads.length + ' files'));
  if (other) {
    const word = parts.length ? ' other tool call' : ' tool call';
    parts.push(other + word + (other === 1 ? '' : 's'));
  }
  if (thinking) parts.push(thinking + ' thinking');
  if (system) parts.push(system + ' system');
  const label = parts.join(', ');
  return label.charAt(0).toUpperCase() + label.slice(1);
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
    if (!isTurn(e) && !isDecisionCard(e) && last && last.classList.contains('tr-run')) {
      const item = renderItem(e, toolErrors);
      tagKey(item, e);
      last.querySelector('.tr-group-body').appendChild(item);
      last._trRun.push(e);
      syncRunSummary(last);
      return;
    }
    els.transcriptList.appendChild(renderEntries([e], toolErrors, CHAT_ANSWERING));
  });
}

// Replace the provisional tail. `open` carries the disclosure state of
// whatever was there before, including cards that have since settled.
function renderPending(entries, toolErrors, open) {
  const frag = renderEntries(entries, toolErrors, CHAT_ANSWERING);
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

// "Following the conversation": within the shared pill's slack of the
// bottom (latest-pill.js). Inside it, new turns scroll into view; outside it
// the reader has deliberately scrolled up into history, is left exactly where
// they are, and the ↓ Latest pill (#1140) is their one tap back — which puts
// them inside it again, so sticking resumes.
function atBottom() {
  return !scrollerIsAway(els.transcriptBody);
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
  syncDecisionCards();
  if (stick) els.transcriptBody.scrollTop = els.transcriptBody.scrollHeight;
}

// Exported for the Life OS conversation viewer (#1119), which mounts this
// same renderer over a parsed capture rather than growing a second one
// (#979). Turn cards and run groups carry no reference to the live `view`,
// except a truncated turn's copy upgrade, and a capture's turns are never
// truncated.
//
// `answering` (#1149) is the Chat pane's own marker that a question card may
// go live; the viewer passes nothing, so its cards stay read-only history.
export function renderEntries(entries, toolErrors, answering) {
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
    } else if (isQuestion(e)) {
      flush();
      frag.appendChild(renderQuestion(e, toolErrors, answering));
    } else if (isPlan(e)) {
      flush();
      frag.appendChild(renderPlan(e, toolErrors, answering));
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
  // Not awaited: the screen read is independent of the transcript, and a
  // session whose transcript is unavailable can still have a plan waiting.
  pollPicker();
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
  clearPicker();
  view.picker = null;
  view.pickerSig = null;
  view.pickerSent = null;
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
  els.transcriptList.appendChild(renderEntries(entries, view.toolErrors, CHAT_ANSWERING));
  view.pendingNodes = pending.length
    ? renderPending(pending, view.toolErrors, null)
    : [];
  syncDecisionCards();
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
      renderEntries(older, toolErrors || view.toolErrors || 'reported', CHAT_ANSWERING),
      els.transcriptList.firstChild,
    );
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
  dropThumbs();
  view = {
    session: s, cursor: null, loading: false, seq: 0, refreshTimer: null,
    entries: null, toolErrors: 'reported',
    // Live refresh (#1050): `tail`/`size` are the forward cursor, `pending*`
    // the provisional tail currently rendered, `ended` latches when the
    // session is gone so no timer is ever rescheduled for it.
    tail: 0, size: null, liveTimer: null, ticking: false, backoff: 0,
    ended: false, reasonShown: false,
    settled: [], pending: [], pendingNodes: [],
    // The plan panel (#1151): the last screen read, its render signature,
    // and when an answer was sent from it.
    picker: null, pickerSig: null, pickerSent: null, pickerBusy: false,
  };
  groupsHidden = true;
  syncGroups();
  bindComposer(s);
  els.chatNote.hidden = s.kind !== 'remote';
  loadNewest();
}

export function closeChatPane() {
  dropThumbs();
  if (view) {
    view.seq += 1;  // any in-flight page lands nowhere
    window.clearTimeout(view.refreshTimer);
    stopLiveTimer();  // a closed overlay fetches nothing (#1050)
  }
  view = null;
  if (!els.chatPane) return;
  els.transcriptList.innerHTML = '';
  clearPicker();
  els.chatNote.hidden = true;
  if (chatComposer) chatComposer.reset();
  pinChatToKeyboard();
  hideState();
}

export function wireChatPane() {
  if (!els.chatPane) return;
  syncGroups();
  openLinksOnThePc(els.transcriptList);
  els.transcriptOlder.addEventListener('click', function () { loadOlder(); });
  mountScrollerPill(els.chatLatest, els.transcriptBody);
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
