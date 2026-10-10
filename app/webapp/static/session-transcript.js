/* Session transcript — the Chat pane of the session overlay (#953, #982).
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
 * paste. That card, the ExitPlanMode card (#1151) and the plan-picker panel
 * that answers a waiting plan from the terminal's own screen live in
 * transcript-decisions.js (#1309), which reads and mutates this module's
 * `view`; this module owns paging, live refresh, turn/run rendering and the
 * image route.
 *
 * Since #982 this is one pane of #terminalOverlay, not its own overlay:
 * session-overlay.js opens/closes it and flips the overlay's data-mode;
 * the bar's ⋮ menu (terminal-bar.js) carries the pane's Hide-steps and
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
 *     run of folded entries merges into the trailing step line rather than
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
import { detachedSendRefused, sendOutcome, sendSessionMessage, sessionTitle } from './sessions.js';
import { keyboardOverlayHeight } from './terminal.js';
import { mountComposer } from './composer.js';
import { uploadSessionFile } from './terminal-compose.js';
import { openImageLightbox } from './lightbox.js';
import { stopReading } from './terminal-readaloud.js';
import { voiceDictationAvailable } from './voice.js';
import { ensureTerminalToken, terminalJsonApi } from './webauthn.js';
import { icon } from './_vendored/icons/icons.js';
import { brandIcon } from './dom-utils.js';
import { renderHunks } from './diff-view.js';
import { fileKey, foldEdits, renderEditedCard } from './edited-card.js';
import { openSessionChanges } from './changes-overlay.js';
import { mountScrollerPill, scrollerIsAway } from './latest-pill.js';
import { closeResumeCard, openResumeCard, wireResumeCard } from './chat-resume.js';
import {
  CHAT_ANSWERING,
  clearPicker,
  isPlan,
  isQuestion,
  pickerAllowed,
  pollPicker,
  renderPlan,
  renderQuestion,
  syncDecisionCards,
} from './transcript-decisions.js';

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
export const SENT_REFRESH_MS = 3000;
// A forced read's "No new messages" stays this long (#1292).
const NEWER_NOTE_MS = 2000;
// The activity line's elapsed counter redraws this often (#1387). A local
// repaint of what the last read said, not a request.
const ACTIVITY_TICK_MS = 1000;
// How far a finger must drag past an edge before it counts as a pull, well
// beyond a tap's jitter and short of a deliberate scroll (#1292).
const PULL_PX = 64;

// The chat pane's composer handle (composer.js), mounted once by
// wireChatPane() and re-bound to the open session by openChatPane().
let chatComposer = null;

// One line per server-side reason, so "nothing there" never reads like
// "couldn't read it" (and vice versa).
const REASON_COPY = {
  session_not_found: 'This session is no longer running',
  // Not an ending (#1308): the session list could not be read, so nothing is
  // known about the session; the live tick keeps asking, backed off.
  session_host_unreachable: 'Can’t reach the session host — retrying',
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

export function toolOutcomeNote(e, toolErrors) {
  if (e.error === true) return '';
  if (e.kind !== 'tool_call' && e.kind !== 'tool_result') return '';
  return TOOL_ERRORS_NOTE[toolErrors] || '';
}

const KIND_ICON = {
  tool_call: 'zap',
  tool_result: 'terminal',
  thinking: 'sparkle',
  system: 'plug',
  user: 'messages-square',
  assistant: 'messages-square',
};

// A step item's verb glyph (#1476): what the call did — run, read, edit,
// search — rather than one terminal glyph for every tool.
const VERB_ICON = {
  ran: 'terminal',
  read: 'book-open',
  edited: 'pencil',
  wrote: 'pencil',
  deleted: 'trash-2',
};

function itemGlyph(e) {
  if (e.kind === 'tool_call') {
    const verb = e.action && e.action.verb;
    if (VERB_ICON[verb]) return VERB_ICON[verb];
    if (/grep|glob|search|find/i.test(e.name || '')) return 'search';
  }
  return KIND_ICON[e.kind] || 'plug';
}

// null when the pane is closed, else the session it shows plus the cursor
// for the next older page. `seq` guards a slow response from a previous
// open/refresh landing in a newer view.
//
// Exported as a live binding (#1309) so transcript-decisions.js's cards and
// plan-picker panel — which read and mutate its fields, never reassign the
// binding itself — see the same object this module does; only openChatPane
// / closeChatPane ever assign `view =` a new value.
export let view = null;
// Whether the step lines (each run of tool calls, thinking and system
// entries) are hidden from the list. Shown by default since #1476: one quiet
// line per run, still folded, so a turn that edited four files says so; the
// ⋮ menu's "Hide steps" leaves a plain user ↔ agent exchange. Turns
// themselves render open and collapse one at a time on their own summary
// (the bar-wide collapse-all left with the bar, #982).
let groupsHidden = false;

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

export function linkify(root) {
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

export function meta(label, ts) {
  const el = document.createElement('div');
  el.className = 'tr-meta';
  const when = fmtTime(ts);
  el.textContent = when ? label + ' · ' + when : label;
  return el;
}

export function pre(text, truncated) {
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

// A file as "folder / name" pieces (#1476): the folder relative to the open
// session's project when the path sits under it (or was recorded relative),
// else just its parent folder. Never the whole absolute path, which wrapped
// to three lines at 390px; callers keep that on `title`.
function fileParts(path) {
  const full = String(path || '').replace(/\\/g, '/');
  const base = baseName(full);
  const parent = full.slice(0, full.length - base.length).replace(/\/+$/, '');
  if (!/^([a-zA-Z]:)?\//.test(full)) return { dir: parent, base: base };
  const root = String((view && view.session.project_dir) || '').replace(/\\/g, '/').replace(/\/+$/, '');
  if (root && parent.toLowerCase().startsWith(root.toLowerCase())) {
    const rel = parent.slice(root.length);
    if (!rel || rel.charAt(0) === '/') return { dir: rel.replace(/^\/+/, ''), base: base };
  }
  return { dir: baseName(parent), base: base };
}

// An edit's line counts, "+4 −0" (a true minus sign, as the Claude Code app),
// coloured like the Changed files panel's. Its own element, so a step line's
// ellipsis can never cut it (#1476).
function deltaEl(added, removed, className) {
  const el = document.createElement('span');
  el.className = className;
  const add = document.createElement('span');
  add.className = 'chg-add';
  add.textContent = '+' + (added || 0);
  const del = document.createElement('span');
  del.className = 'chg-del';
  del.textContent = '−' + (removed || 0);
  el.append(add, ' ', del);
  return el;
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

// A turn's fragments, copied as they read: one blank line between them.
function joinTexts(texts) {
  return texts.join('\n\n');
}

// Tap the copy glyph on a prompt or an agent turn: writes the clipboard
// synchronously inside the tap gesture — iOS requires this, an `await`
// ahead of the first write loses the gesture and the copy silently fails on
// the one device this feature is for — with whatever text the turn already
// has. `entries` is the prompt, or every reply fragment of the turn (#1475).
// A capped fragment then fetches the uncapped one (#985's ``/transcript/
// entry`` route) and *visibly* upgrades the clipboard with a second toast:
// never a silent rewrite, since a paste in the gap between the two would
// hand back truncated text with no reason to suspect it, and the clipboard
// changing again afterwards would be worse.
async function copyTurn(entries, kind) {
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
  const sid = view ? view.session.session_id : null;
  const fail = function () {
    toast(label + ' copy is truncated — the full text didn’t load', 'bad', { icon: 'copy' });
  };
  if (!sid || capped.some(function (e) { return e.offset == null; })) {
    fail();
    return;
  }
  let texts;
  try {
    texts = await Promise.all(entries.map(async function (e) {
      if (!e.truncated) return e.text || '';
      const body = await terminalJsonApi(
        '/api/claude-code/sessions/' + encodeURIComponent(sid) +
          '/transcript/entry?offset=' + encodeURIComponent(e.offset)
      );
      if (!body || !body.available) throw new Error('entry unavailable');
      return body.text;
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
    const body = await terminalJsonApi(
      '/api/claude-code/sessions/' + encodeURIComponent(sid) +
        '/transcript/entry?offset=' + encodeURIComponent(e.offset)
    );
    if (body && body.available) return body.text || '';
  } catch (exc) { /* fall through to the capped text already loaded */ }
  return e.text || '';
}

// --- turns (#1475) -----------------------------------------------------------
//
// A turn reads as one piece: the user's prompt is a right-aligned bubble, and
// everything the agent does until the next prompt (each reply fragment, the
// step lines between them, a question or plan card) sits in ONE block under
// one header: the agent's mark and name, the time, copy for the whole turn,
// and collapse. It used to be one card per text fragment, each with its own
// "Agent · time" and copy (#1472's diagnosis 1). The prompt is plain text
// (pre-wrap); a reply goes through the same escape-first markdown renderer
// the Life OS doc browser uses — never raw HTML from a transcript.

const TRUNC_COPY = '(truncated — open the terminal for the rest)';

function truncMark() {
  const mark = document.createElement('div');
  mark.className = 'tr-trunc';
  mark.textContent = TRUNC_COPY;
  return mark;
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
function copyButton(kind, entriesOf) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'icon-button tr-turn-copy';
  btn.setAttribute('aria-label', 'Copy ' + copyLabel(kind).toLowerCase());
  btn.innerHTML = icon('copy');
  btn.addEventListener('click', function (ev) {
    ev.preventDefault();
    ev.stopPropagation();
    copyTurn(entriesOf(), kind);
  });
  return btn;
}

function chevron() {
  const chev = document.createElement('span');
  chev.className = 'tr-turn-chevron';
  chev.setAttribute('aria-hidden', 'true');
  chev.textContent = '›';
  return chev;
}

// A collapsed turn's one line: the text's first line, or "Image" for an image alone.
function textHint(e) {
  return firstLine(e.text, 80) || (e.images && e.images.length ? 'Image' : '');
}

// The prompt bubble. Open, it is the bubble with the time, copy and a fold
// chevron under it; folded, the <summary> takes its place as a one-line
// bubble that a tap opens again. The summary is hidden while open rather than
// carrying the controls, so the bubble has no header row (#1472 mockup).
function renderPrompt(e) {
  const li = document.createElement('li');
  li.className = 'tr-turn-item tr-prompt-item';
  const d = document.createElement('details');
  d.className = 'tr-turn tr-user';
  d.open = true;
  tagKey(d, e);
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
  const text = document.createElement('div');
  text.className = 'tr-text';
  text.textContent = e.text || '';
  linkify(text);
  bubble.appendChild(text);
  const shots = thumbs(e.images);
  if (shots) bubble.appendChild(shots);
  if (e.truncated) bubble.appendChild(truncMark());
  d.appendChild(bubble);
  const foot = document.createElement('div');
  foot.className = 'tr-prompt-foot';
  const when = document.createElement('span');
  when.className = 'tr-meta';
  when.textContent = fmtTime(e.timestamp);
  when.hidden = !when.textContent;
  foot.appendChild(when);
  foot.appendChild(copyButton('user', function () { return [e]; }));
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
function renderReply(e) {
  const li = document.createElement('li');
  li.className = 'tr-reply';
  li._trEntry = e;
  const body = document.createElement('div');
  body.className = 'tr-text tr-md';
  body.innerHTML = renderMarkdown(e.text || '');
  linkify(body);
  li.appendChild(body);
  const shots = thumbs(e.images);
  if (shots) li.appendChild(shots);
  if (e.truncated) li.appendChild(truncMark());
  return li;
}

function isAgentTurn(node) {
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
// lets the turn grow an Edited N files card once it is over (#1477).
function renderAgentTurn(first, agent, edited) {
  const li = document.createElement('li');
  li.className = 'tr-turn-item tr-agent-item';
  li._trFirst = first;
  li._trEditable = !!edited;
  const d = document.createElement('details');
  d.className = 'tr-turn tr-assistant';
  d.open = true;
  tagKey(d, first);
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
  const copyBtn = copyButton('assistant', function () { return turnReplies(li); });
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
// part follows it (#1476).
function syncTurnHead(li) {
  li._trParts.querySelectorAll(':scope > .tr-run').forEach(syncRunSummary);
  const replies = turnReplies(li);
  const time = fmtTime(li._trFirst && li._trFirst.timestamp);
  li._trWhen.textContent = time ? '· ' + time : '';
  let hint = replies.length ? textHint(replies[0]) : '';
  if (!hint) {
    const title = li._trParts.querySelector('.tr-step-label');
    hint = title ? title.textContent : '';
  }
  li._trHint.textContent = hint;
  li._trCopy.hidden = !replies.length;
  markSilent(li);
}

// With the steps hidden, a turn with no reply, no decision card and no Edited
// card would be a header over nothing, so it hides with them. The Edited card
// keeps it: the work still has a trace (#1477).
function markSilent(li) {
  const quiet = !turnReplies(li).length && !li._trEdited &&
    !li._trParts.querySelector(':scope > .tr-ask-item, :scope > .tr-plan-item');
  li.classList.toggle('tr-turn-silent', quiet);
}

// --- the Edited N files card (#1477) -------------------------------------------
//
// It appears once the turn is over (decision 4 of #1472): a prompt follows it,
// or it is the newest turn and the activity line does not say the agent is
// working. An activity the server could not establish (null) counts as over:
// the card only summarises loaded steps, so showing it early misleads less
// than hiding it from a turn that has ended. It sits after the turn's parts,
// inside the turn's disclosure, so it collapses with the turn.

// Every step entry the turn holds, in order.
function turnSteps(li) {
  const out = [];
  li._trParts.querySelectorAll(':scope > .tr-run').forEach(function (run) {
    Array.prototype.push.apply(out, run._trRun || []);
  });
  return out;
}

function turnOver(li) {
  if (li.nextElementSibling) return true;
  return !(view && view.activity && view.activity.working);
}

// The turn opens the list and older pages exist, so its first steps may sit
// on one of them.
function turnStartMissing(li) {
  return !li.previousElementSibling && !!view && view.cursor != null;
}

// The files the turn edited, each named as the card names it (`base`, `dir`
// relative to the session's project).
function turnFiles(li) {
  const files = foldEdits(turnSteps(li));
  files.forEach(function (f) {
    const parts = fileParts(f.path);
    f.base = parts.base;
    f.dir = parts.dir;
  });
  return files;
}

// A card tap: the Changed files panel on this turn (#1477), focused on
// `focus` (a card row's file) or on none (the header). The turn's files go
// with it, named as the card names them, and how to draw one of its steps.
function openTurnChanges(files, partial, focus) {
  if (!view) return;
  const s = view.session;
  openSessionChanges(s, sessionTitle(s), {
    turn: { files: files, partial: partial, renderStep: function (e) { return stepDiff(e, true); } },
    focus: focus ? focus.path : null,
  });
}

// Rebuilt only when what it shows changed: a live tick lands every few
// seconds, and rebuilding an unchanged card would break a tap in flight
// (the #680 lesson). True when it did change.
function syncEditedCard(li) {
  if (!li._trEditable) return false;
  const files = turnOver(li) ? turnFiles(li) : [];
  const partial = files.length > 0 && turnStartMissing(li);
  const expanded = !!li._trEditedAll;
  const sig = files.length
    ? JSON.stringify([files.map(function (f) { return [f.key, f.status, f.additions, f.deletions]; }), partial, expanded])
    : '';
  if (sig === (li._trEditedSig || '')) return false;
  li._trEditedSig = sig;
  if (li._trEdited) li._trEdited.remove();
  li._trEdited = null;
  if (files.length) {
    li._trEdited = renderEditedCard(files, {
      expanded: expanded,
      partial: partial,
      onOpen: function (file) { openTurnChanges(files, partial, file); },
      onMore: function () {
        li._trEditedAll = true;
        syncEditedCard(li);
      },
      onLoadOlder: function () { loadOlder(); },
    });
    li.querySelector('.tr-turn').appendChild(li._trEdited);
  }
  markSilent(li);
  return true;
}

// Every turn's card, after anything that can change one: new steps, a prompt
// arriving after a turn, the activity line, an older page joining the first.
// True when any card changed.
function syncEditedCards() {
  if (!view) return false;
  let changed = false;
  els.transcriptList.querySelectorAll(':scope > .tr-agent-item').forEach(function (li) {
    if (syncEditedCard(li)) changed = true;
  });
  return changed;
}

// --- file chips (#1478) --------------------------------------------------------
//
// A reply that names a file this session edited, in a code span or as a bare
// path in its prose, gets a file chip there: the code chip in the accent, a
// file glyph and the file's name, and a tap opens that file's diff in Changed
// files (decision 6 of #1472). Only an edited file links, and only when the
// name is unambiguous: the span must be the file's whole path or a trailing
// run of its folders and name (`styles.css`, `static/styles.css`, the full
// path) that no other edited file ends with. Everything else stays the plain
// code chip. A post-render DOM pass, like linkify(), so the markdown renderer
// stays string-only; the Life OS viewer has no session to open a panel
// against and never runs it.
//
// The edited set is folded from every step the page holds, so a file a later
// tick edits, or an older page shows was edited, links when it arrives. A
// reply's chips are undone and redone only when that set changes, never on a
// tick that brought no new file (the #680 lesson).

// A line reference after the name: ":12", ":12:3", ":12-20".
const LINE_REF_RE = /:\d+(?:[:-]\d+)?$/;
// A bare path's characters. Loose on purpose: only a run that resolves to an
// edited file becomes a chip.
const PATH_RUN_RE = /[\w.@~+\-/\\:]+/g;
const PATH_TRAIL_RE = /[.,;:!?]+$/;

// The edited file `text` names, with any line reference it carries, or null.
// `bare` (prose, not a code span) also asks it to look like a path, with a
// folder or an extension, so an extensionless file never links a plain word.
function resolveFile(text, files, bare) {
  let t = String(text || '').trim();
  if (!t || /\s/.test(t)) return null;
  const ref = t.match(LINE_REF_RE);
  if (ref) t = t.slice(0, -ref[0].length);
  if (bare && !/[\\/]/.test(t) && !/[^.]\.[a-z0-9]+$/i.test(t)) return null;
  const k = fileKey(t).replace(/^(\.\/)+/, '');
  if (!k) return null;
  const hits = files.filter(function (f) { return f.key === k || f.key.endsWith('/' + k); });
  return hits.length === 1 ? { file: hits[0], ref: ref ? ref[0] : '' } : null;
}

// A tap on a chip: if the chip's own turn edited the file, the panel on that
// turn, focused on it, as the turn's card row opens it (#1477); otherwise the
// whole session's list, focused on it.
function openFileChip(chip, file) {
  if (!view) return;
  const li = chip.closest('.tr-agent-item');
  const files = li ? turnFiles(li) : [];
  const own = files.find(function (f) { return f.key === file.key; });
  if (own) {
    openTurnChanges(files, turnStartMissing(li), own);
    return;
  }
  const s = view.session;
  openSessionChanges(s, sessionTitle(s), { focus: file.path });
}

// `orig` is the node the chip stands in for, put back when the chips are redone.
function fileChip(hit, orig) {
  const base = baseName(hit.file.path);
  const chip = document.createElement('button');
  chip.type = 'button';
  chip.className = 'tr-file-chip';
  chip.title = hit.file.path;
  chip.setAttribute('aria-label', 'Open the diff of ' + base);
  chip.dataset.path = hit.file.path;
  chip.innerHTML = icon('file-text');
  const name = document.createElement('span');
  name.className = 'tr-file-chip-name';
  name.textContent = base + hit.ref;
  chip.appendChild(name);
  chip._trOrig = orig;
  chip.addEventListener('click', function () { openFileChip(chip, hit.file); });
  return chip;
}

// Text a bare path may sit in: prose, never a link, a code span or listing,
// a fenced block's header or a control.
function chipText(node) {
  const p = node.parentElement;
  return !!p && !p.closest('a, code, pre, button, .md-block');
}

function chipFiles(body, files) {
  body.querySelectorAll('code').forEach(function (code) {
    if (code.closest('pre, a, button')) return;
    const hit = resolveFile(code.textContent, files, false);
    if (hit) code.replaceWith(fileChip(hit, code));
  });
  const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT, {
    acceptNode: function (node) { return chipText(node) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT; },
  });
  const nodes = [];
  let n;
  while ((n = walker.nextNode())) nodes.push(n);
  nodes.forEach(function (textNode) {
    const text = textNode.nodeValue;
    const frag = document.createDocumentFragment();
    let last = 0;
    let m;
    PATH_RUN_RE.lastIndex = 0;
    while ((m = PATH_RUN_RE.exec(text))) {
      const run = m[0].replace(PATH_TRAIL_RE, '');
      const hit = run && resolveFile(run, files, true);
      if (!hit) continue;
      if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
      frag.appendChild(fileChip(hit, document.createTextNode(run)));
      last = m.index + run.length;
    }
    if (!last) return;
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    textNode.parentNode.replaceChild(frag, textNode);
  });
}

function unchipFiles(body) {
  body.querySelectorAll('.tr-file-chip').forEach(function (chip) { chip.replaceWith(chip._trOrig); });
  body.normalize();
}

// Every reply in the Chat list, against the files the loaded steps edited.
function syncFileChips() {
  if (!view) return;
  const files = foldEdits(view.entries || []);
  const sig = files.map(function (f) { return f.key; }).sort().join('\n');
  els.transcriptList.querySelectorAll('.tr-reply > .tr-md').forEach(function (body) {
    if (body._trChipSig === sig) return;
    if (body._trChipSig != null) unchipFiles(body);
    body._trChipSig = sig;
    if (files.length) chipFiles(body, files);
  });
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
  tagKey(newer.querySelector('.tr-turn'), older._trFirst);
  older.remove();
  syncTurnHead(newer);
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

// ⋮ menu — "Load new" (#1292), and a pull up past the bottom of the list:
// one forward read now, the same one the live tick makes (#1050), so what
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

// A forced read's status, in the bottom strip (#1387; it floated over the
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

// --- the strip's live line (#1387) -----------------------------------------
//
// "[timer icon] 15:02 · 10 actions · Running Bash": the turn in progress, from
// the `activity` the transcript responses carry (src/session_transcript.py).
// Everything but the elapsed counter is the last read's word; the counter is
// redrawn here once a second from `since`. The timer is the vendored `i-timer`
// icon, never the emoji (#1394): iOS draws a colour stopwatch for it and
// desktop another glyph, and the emoji's fallback font metrics inflate the
// line box, which is what let the line sit low on the phone.

function fmtElapsed(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  const pad = function (n) { return String(n).padStart(2, '0'); };
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return (h ? h + ':' + pad(m) : String(m)) + ':' + pad(s % 60);
}

// The line, or null when nothing is mid-turn or the turn's start is unknown.
// `actions` is null for a harness that records no tool calls: omitted, never 0.
function activityLine(a) {
  if (!a || !a.working) return null;
  const since = new Date(a.since).getTime();
  if (Number.isNaN(since)) return null;
  const parts = [fmtElapsed(Date.now() - since)];
  if (a.actions != null) parts.push(a.actions + (a.actions === 1 ? ' action' : ' actions'));
  if (a.last) parts.push(a.last);
  return parts.join(' · ');
}

let activityTimer = null;

// The line's two children, built once: the timer icon (shown for the live
// line only) and the text, which alone is rewritten on every tick. The line
// is a centring flex row (styles.css), so neither child can lift or sink it.
function stripLineParts(el) {
  let text = el.querySelector('.terminal-activity-text');
  if (!text) {
    el.innerHTML = icon('timer', 'terminal-activity-icon');
    text = document.createElement('span');
    text.className = 'terminal-activity-text';
    el.appendChild(text);
  }
  return { icon: el.querySelector('.terminal-activity-icon'), text };
}

// Writes the strip's line: a forced read's note, else the activity line, and
// nothing outside Chat mode. Connection status outranks both in CSS (it hides
// this element while #terminalStatus shows). The counter's tick lives exactly
// as long as a live activity line does.
function renderStripLine() {
  const el = els.terminalActivity;
  if (!el) return;
  const note = view && view.note;
  const live = !note && view && liveAllowed() ? activityLine(view.activity) : null;
  const text = note || live;
  const parts = stripLineParts(el);
  // The ticking counter must not be announced every second; a note should be.
  el.setAttribute('aria-live', note ? 'polite' : 'off');
  parts.text.textContent = text || '';
  parts.icon.style.display = live ? '' : 'none';
  el.hidden = !text;
  if (live && !activityTimer) {
    activityTimer = window.setInterval(renderStripLine, ACTIVITY_TICK_MS);
  } else if (!live && activityTimer) {
    window.clearInterval(activityTimer);
    activityTimer = null;
  }
}

// --- per-step diffs (#1349) -------------------------------------------------
//
// An edit/write call's `action.diff` carries the page's share of its diff
// (capped per step, server-side); the rest is one request away through the
// step's own ref ({offset, n}). A failed call carries no diff and keeps
// today's body, as does any call whose tool the server doesn't recognise.

// `bare` leaves the "folder / name" header off: the Changed files panel's
// This turn scope (#1477) draws a step under the file's own row.
function stepDiff(e, bare) {
  const a = e.action;
  if (!a || !a.diff || e.error === true || (a.verb !== 'edited' && a.verb !== 'wrote')) return null;
  const wrap = document.createElement('div');
  wrap.className = 'tr-diff-wrap';
  if (!bare) {
    // Headed "folder / name" (#1476); the whole path stays on hover.
    const path = document.createElement('div');
    path.className = 'tr-diff-path';
    path.title = a.path || '';
    const f = fileParts(a.path);
    if (f.dir) {
      const dir = document.createElement('span');
      dir.className = 'tr-diff-dir';
      dir.textContent = f.dir;
      path.append(dir, ' / ');
    }
    const base = document.createElement('span');
    base.className = 'tr-diff-base';
    base.textContent = f.base;
    path.appendChild(base);
    wrap.appendChild(path);
  }
  wrap.appendChild(renderHunks(a.diff));
  if (a.diff.truncated) wrap.appendChild(fullDiffControl(wrap, a.diff));
  return wrap;
}

function truncNote(text) {
  const note = document.createElement('div');
  note.className = 'tr-trunc tr-diff-note';
  note.setAttribute('role', 'status');
  note.textContent = text;
  return note;
}

// "Show full diff" in Chat; outside it (the Life OS viewer has no session
// to ask) just the note that there is more.
function fullDiffControl(wrap, diff) {
  if (!view || diff.offset == null) return truncNote('Diff truncated');
  const sid = view.session.session_id;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'button-tint tr-diff-more';
  btn.textContent = 'Show full diff';
  btn.addEventListener('click', async function (ev) {
    ev.stopPropagation();
    btn.disabled = true;
    btn.textContent = 'Loading…';
    let body = null;
    try {
      body = await terminalJsonApi(
        '/api/claude-code/sessions/' + encodeURIComponent(sid) +
          '/transcript/diff?offset=' + encodeURIComponent(diff.offset) +
          '&n=' + encodeURIComponent(diff.n || 0)
      );
    } catch (exc) {
      body = null;
    }
    if (!body || !body.available || !body.diff) {
      btn.replaceWith(truncNote('Couldn’t load the full diff — showing the first part'));
      return;
    }
    wrap.querySelector('.tr-diff').replaceWith(renderHunks(body.diff));
    if (body.diff.truncated) btn.replaceWith(truncNote('Diff truncated at 200 KB'));
    else btn.remove();
  });
  return btn;
}

// One folded item hanging off a step line's rail — its own <details>, so a
// single tool call can be opened without expanding its siblings.
function renderItem(e, toolErrors) {
  const d = document.createElement('details');
  d.className = 'tr-item tr-item-' + e.kind;
  tagKey(d, e);
  // A failed call is marked on the row itself, so it reads as failed while
  // the group is still folded open at one item — never as a banner over the
  // whole turn (#1020).
  if (e.error === true) d.classList.add('tr-item-failed');
  const s = document.createElement('summary');
  s.innerHTML = icon(itemGlyph(e));
  const name = document.createElement('span');
  name.className = 'tr-item-name';
  const hint = document.createElement('span');
  hint.className = 'tr-item-hint';
  let delta = null;
  if (e.kind === 'tool_call') {
    // Plain words where the server knows the tool (#1266): the command's
    // first line, or the file's name with its folder as the hint (#1476)
    // and, for an edit, its +N −M.
    const a = e.action;
    if (a && a.verb === 'ran') {
      name.textContent = firstLine(a.command, 80);
      name.classList.add('tr-item-cmd');
    } else if (a && (a.verb === 'edited' || a.verb === 'wrote' || a.verb === 'read')) {
      const f = fileParts(a.path);
      name.textContent = f.base;
      hint.textContent = f.dir;
      s.title = a.path;
      if (a.verb !== 'read' && e.error !== true) delta = deltaEl(a.added, a.removed, 'tr-item-delta');
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
  // Name, then the hint taking what room is left; the failed chip and the
  // counts trail it, non-shrinking, so the ellipsis only ever cuts the hint.
  s.append(name, hint);
  if (e.error === true) {
    const chip = document.createElement('span');
    chip.className = 'tr-fail-chip';
    chip.textContent = 'failed';
    s.appendChild(chip);
  }
  if (delta) s.appendChild(delta);
  d.appendChild(s);
  const body = document.createElement('div');
  body.className = 'tr-item-body';
  if (e.kind === 'tool_call') {
    // A command opens as a terminal: `$ command`, then its output (#1266).
    // An edit opens as its diff (#1349) and nothing else: the tool's own
    // "file has been updated" line was a second box saying less (#1476).
    const ran = e.action && e.action.verb === 'ran';
    const diff = stepDiff(e);
    if (ran) body.appendChild(term(pre('$ ' + e.action.command, false)));
    else if (diff) body.appendChild(diff);
    else if (e.summary) body.appendChild(pre(e.summary, false));
    if (diff) {
      // The diff is the result.
    } else if (e.result != null) {
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


// A run in plain words (#1266, #1476): [verb, object] pairs that read
// "Edited main.js, ran 2 commands, read 3 files", plus the edits' total +N −M.
// The total is not label text: the step line gives it its own element,
// which the label's ellipsis can't reach. Built from each call's server-side
// `action`; a call without one is counted as before.
function runSummary(run) {
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
    } else if (a && (a.verb === 'edited' || a.verb === 'wrote') && e.error !== true) {
      // A failed edit changed nothing: it counts as a call, never toward the
      // +N −M, the rule the Changed files panel's totals follow.
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
  // Edits lead: they are what the trailing +N −M counts, and at phone width
  // the label's start is the part that survives the ellipsis.
  const parts = [];
  const edits = Object.keys(edited);
  let total = null;
  if (edits.length) {
    total = edits.reduce(function (s, path) {
      return { added: s.added + edited[path].added, removed: s.removed + edited[path].removed };
    }, { added: 0, removed: 0 });
    parts.push(['edited', edits.length === 1 ? baseName(edits[0]) : edits.length + ' files']);
  }
  if (ran) parts.push(['ran', ran === 1 ? 'a command' : ran + ' commands']);
  const reads = Object.keys(read);
  if (reads.length) parts.push(['read', reads.length === 1 ? baseName(reads[0]) : reads.length + ' files']);
  if (other) {
    const word = parts.length ? ' other tool call' : ' tool call';
    parts.push(['', other + word + (other === 1 ? '' : 's')]);
  }
  if (thinking) parts.push(['', thinking + ' thinking']);
  if (system) parts.push(['', system + ' system']);
  return { parts: parts, total: total };
}

// The label: verbs muted, what they acted on in the text colour.
function fillStepLabel(el, parts) {
  el.textContent = '';
  parts.forEach(function (p, i) {
    if (i) el.append(', ');
    const verb = i === 0 ? p[0].charAt(0).toUpperCase() + p[0].slice(1) : p[0];
    if (verb) el.append(verb + ' ');
    const what = document.createElement('b');
    what.textContent = p[1];
    el.appendChild(what);
  });
}

function failedCount(run) {
  return run.reduce(function (n, e) { return n + (e.error === true ? 1 : 0); }, 0);
}

// "14s", "1m 52s", "1h 5m"; empty under a second.
function fmtSpan(ms) {
  const s = Math.round(ms / 1000);
  if (!(s >= 1)) return '';
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60);
  if (m < 60) return m + 'm ' + (s % 60) + 's';
  return Math.floor(m / 60) + 'h ' + (m % 60) + 'm';
}

// How long a run took: from its first entry to the reply that follows it in
// the turn, or, with none yet, to its own last entry. Empty when either end
// has no usable timestamp.
function runSpan(li) {
  const run = li._trRun;
  const next = li.nextElementSibling;
  const end = next && next._trEntry ? next._trEntry : run[run.length - 1];
  return fmtSpan(Date.parse(end.timestamp) - Date.parse(run[0].timestamp));
}

// A run of consecutive folded entries → one quiet step line (#1476): a
// chevron, the label, then the parts no ellipsis may cut. Open, its items
// hang off a rail beneath it.
function renderRun(run, toolErrors) {
  const li = document.createElement('li');
  li.className = 'tr-run';
  // The entries this line holds, kept on the node so a later tick can merge
  // more into it and recompute the summary (#1050).
  li._trRun = run.slice();
  const d = document.createElement('details');
  d.className = 'tr-step';
  tagKey(d, run[0]);
  const s = document.createElement('summary');
  s.className = 'tr-step-line';
  s.innerHTML = icon('chevron-right', 'tr-step-chevron');
  const label = document.createElement('span');
  label.className = 'tr-step-label';
  s.appendChild(label);
  d.appendChild(s);
  const body = document.createElement('div');
  body.className = 'tr-step-items';
  run.forEach(function (e) { body.appendChild(renderItem(e, toolErrors)); });
  d.appendChild(body);
  li.appendChild(d);
  syncRunSummary(li);
  return li;
}

// --- live refresh rendering (#1050) ---------------------------------------
//
// Every disclosure in a live region is tagged with the byte offset of the
// entry it renders, so an open card can be found again after the region is
// rebuilt. Offsets are stable identities: an entry keeps the offset of the
// line it started at for as long as that file is not rewritten.

export function tagKey(el, entry) {
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

// A step line, recomputed from the entries it now holds and from what
// follows it — called on every merge and every turn-head sync, so a run
// that grew keeps an honest count and its duration ends at the reply that
// arrived after it. The trailing parts (total +N −M, "N failed", duration)
// are rebuilt in that order; a failure has to read while the line is
// closed, and none of them may be cut by the label's ellipsis.
function syncRunSummary(li) {
  const run = li._trRun || [];
  const line = li.querySelector('.tr-step-line');
  const sum = runSummary(run);
  fillStepLabel(line.querySelector('.tr-step-label'), sum.parts);
  line.querySelectorAll(':scope > .tr-step-delta, :scope > .tr-fail-count, :scope > .tr-step-dur')
    .forEach(function (n) { n.remove(); });
  if (sum.total) line.appendChild(deltaEl(sum.total.added, sum.total.removed, 'tr-step-delta'));
  const failed = failedCount(run);
  if (failed) {
    const chip = document.createElement('span');
    chip.className = 'tr-fail-count';
    chip.textContent = failed + ' failed';
    line.appendChild(chip);
  }
  const span = runSpan(li);
  if (span) {
    const dur = document.createElement('span');
    dur.className = 'tr-step-dur';
    dur.textContent = span;
    line.appendChild(dur);
  }
}

// The one place entries become list nodes (#1475): appends `entries` to
// `container` (the list itself, or a fragment), continuing whatever it ends
// with. A prompt starts a bubble; anything else joins the trailing agent
// turn, or opens one; a folded entry joins the turn's trailing step line, or
// opens one. So a page renders, a live tick appends (#1050) and the Life OS
// viewer mounts through the same rules, and an autonomous stretch arriving a
// tick at a time still folds into one turn and one run rather than a card
// per tick.
//
// `opts.pending` renders the provisional tail: its nodes are returned so the
// next tick can drop them, and it never merges into a settled step line (that
// would have to be unpicked). A turn it continues keeps its header; the parts
// it adds are what gets dropped.
function appendEntries(container, entries, opts) {
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
      const li = renderPrompt(e);
      container.appendChild(li);
      created.push(li);
      return;
    }
    let turn = container.lastElementChild;
    if (!isAgentTurn(turn)) {
      turn = renderAgentTurn(e, opts.agent, opts.edited);
      container.appendChild(turn);
      created.push(turn);
      ownTurns.add(turn);
    }
    touchedTurns.add(turn);
    if (isTurn(e)) {
      addPart(turn, renderReply(e));
    } else if (isQuestion(e)) {
      addPart(turn, renderQuestion(e, opts.toolErrors, opts.answering));
    } else if (isPlan(e)) {
      addPart(turn, renderPlan(e, opts.toolErrors, opts.answering));
    } else {
      const last = turn._trParts.lastElementChild;
      if (last && last.classList.contains('tr-run') && (!opts.pending || ownRuns.has(last))) {
        last.querySelector('.tr-step-items').appendChild(renderItem(e, opts.toolErrors));
        last._trRun.push(e);
      } else {
        const run = renderRun([e], opts.toolErrors);
        ownRuns.add(run);
        addPart(turn, run);
      }
    }
  });
  // A turn's head sync recomputes its step lines too.
  touchedTurns.forEach(syncTurnHead);
  return created;
}

// The Chat pane's agent: its turns carry that agent's mark and name. An
// absent field (a ?session= deep link's bare {session_id, name}) is Claude,
// as the endpoint assumes.
function chatOpts(toolErrors, pending) {
  return {
    toolErrors: toolErrors,
    answering: CHAT_ANSWERING,
    agent: String((view && view.session.agent) || 'claude').toLowerCase(),
    pending: !!pending,
    edited: true,
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
  hosts.forEach(syncTurnHead);
  if (view) view.pendingNodes = [];
  return open;
}

// "Following the conversation": within the shared pill's slack of the
// bottom (latest-pill.js). Inside it, new turns scroll into view; outside it
// the reader has deliberately scrolled up into history, is left exactly where
// they are, and the ↓ Latest pill (#1140) is their one tap back — which puts
// them inside it again, so sticking resumes.
export function atBottom() {
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
// (#979). Turns and run groups carry no reference to the live `view`,
// except a truncated turn's copy upgrade, and a capture's turns are never
// truncated.
//
// `answering` (#1149) is the Chat pane's own marker that a question card may
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
export function liveAllowed() {
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
// fallback can't name the file (#1023) — so those keep ticking, backed off,
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

// One forward-cursor read (#1050), the background tick's or a forced one
// (#1292). Resolves to what it found — 'changed', 'unchanged',
// 'unavailable', 'reset', 'failed', or 'stale' when the pane moved on — so
// a forced read can say it. A failed forced read toasts, as a tapped action
// does; a failed tick backs off quietly, as it always has.
async function forwardRead(target, manual) {
  const seq = target.seq;
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
  // keeps a reader who was following the conversation at the bottom (#1477).
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
  if (body.changed) {
    target.tail = body.tail;
    target.size = body.size;
    applyLive(body.entries || [], body.pending || [], body.tool_errors || target.toolErrors);
  } else if (body.size != null) {
    target.size = body.size;
  }
  syncFileChips();
  if (syncEditedCards() && stick) els.transcriptBody.scrollTop = els.transcriptBody.scrollHeight;
  // A tick that got an answer clears any reason line an earlier failed one
  // left on screen, so a condition that cleared by itself looks like it.
  // Checked after this tick's turns land: a view that opened unavailable
  // (#1300) has none until then.
  if (target.reasonShown) {
    target.reasonShown = false;
    if (view.entries && view.entries.length) hideState();
    else showState('Nothing in the transcript yet');
  }
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
    // after the first backed-off tick (#1300).
    liveUnavailable(body.reason);
    pollPicker();
    return;
  }
  view.activity = body.activity || null;
  renderStripLine();
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
  appendSettled(entries, view.toolErrors);
  view.pendingNodes = pending.length
    ? renderPending(pending, view.toolErrors, null)
    : [];
  syncDecisionCards();
  view.cursor = body.next_cursor;
  els.transcriptOlder.hidden = view.cursor == null;
  syncFileChips();
  syncEditedCards();
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
    const frag = document.createDocumentFragment();
    appendEntries(frag, older, chatOpts(toolErrors || view.toolErrors || 'reported', false));
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
  // `/resume` opens the searchable resume card (#1300) instead of Claude
  // Code's scrolling picker, where Chat can read the terminal's screen.
  if (text.trim() === '/resume' && pickerAllowed(view.session)) {
    openResumeCard('composer');
    return true;
  }
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

function uploadFromChat(file, signal, kind) {
  return uploadSessionFile(view ? view.session.session_id : null, file, signal, kind);
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
  closeResumeCard();
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
    // A forced read (#1292): the read in flight (either kind), whether a
    // forced one is running, and the status line's hide timer.
    inflight: null, forcing: false, newerTimer: null, note: null,
    // The turn in progress (#1387), as the last read described it.
    activity: null,
    settled: [], pending: [], pendingNodes: [],
    // The plan panel (#1151): the last screen read, its render signature,
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
  dropThumbs();
  if (view) {
    newerNote(null);
    view.seq += 1;  // any in-flight page lands nowhere
    window.clearTimeout(view.refreshTimer);
    stopLiveTimer();  // a closed overlay fetches nothing (#1050)
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
  wirePullGestures(els.transcriptBody);
}

// Pull gestures (#1292): a drag that starts and ends at one edge of the list
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
