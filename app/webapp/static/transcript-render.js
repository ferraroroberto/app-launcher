/* Transcript entry renderer (#1629, split out of session-transcript.js).
 *
 * Turns a transcript's entries into list nodes: prompt bubbles, agent turns
 * with their replies, step lines and the items under them, sent-file cards,
 * per-step diffs and file chips. Two mounts use it (#979): the session
 * overlay's Chat pane (session-transcript.js, which owns paging, live
 * refresh, the outbox cursor and the composer) and the Life OS conversation
 * viewer (life-os-viewer.js), which mounts a parsed capture through
 * `renderEntries`.
 *
 * Stateless about the Chat pane: whatever a node needs from the open session
 * comes in as a parameter. `opts.session` is that session (its id asks the
 * image, file, diff and entry routes; its `project_dir` shortens paths), or
 * null in the viewer, which has no session to ask; `opts.live` is the step
 * in progress, if any. The one state kept here is the media cache, owned by
 * whichever session `resetBlobs` last named.
 *
 * The question and plan cards are transcript-decisions.js's; their entry
 * points are what `appendEntries` calls into.
 */

import { state } from './state.js';
import { api, authHeaders, jsonApi, toast } from './api.js';
import { renderMarkdown } from './markdown.js';
import { openImageLightbox } from './lightbox.js';
import { ensureTerminalToken, terminalJsonApi } from './webauthn.js';
import { icon } from './_vendored/icons/icons.js';
import { brandIcon } from './dom-utils.js';
import { renderHunks } from './diff-view.js';
import { fileKey } from './edited-card.js';
import { isPlan, isQuestion, renderPlan, renderQuestion } from './transcript-decisions.js';

// Agents whose native history the server-side reader understands — the
// same set as the endpoint's flavour map (app/webapp/routers/
// session_transcript.py). Decides Chat availability without a probe; the
// pane still shows the server's own reason line if it disagrees. An absent
// agent field (a ?session= deep link's bare {session_id, name}) defaults
// to Claude, as the endpoint does.
export const TRANSCRIPT_AGENTS = ['claude', 'codex', 'grok', 'pi', 'antigravity', 'copilot'];

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

export function isTurn(e) {
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

export function fmtTime(ts) {
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

export function pre(text, truncated, className) {
  const el = document.createElement('pre');
  el.className = className || 'tr-pre';
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
// to three lines at 390px; callers keep that on `title`. `session` is the
// open session (null: no project to be relative to).
export function fileParts(path, session) {
  const full = String(path || '').replace(/\\/g, '/');
  const base = baseName(full);
  const parent = full.slice(0, full.length - base.length).replace(/\/+$/, '');
  if (!/^([a-zA-Z]:)?\//.test(full)) return { dir: parent, base: base };
  const root = String((session && session.project_dir) || '').replace(/\\/g, '/').replace(/\/+$/, '');
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
// --- an opened step (#1532) ----------------------------------------------
//
// A card on the code-listing frame: a small header (the tool, the file as
// "folder / name", a recorded exit status), then the call's input and its
// output as separate blocks. A command reads `$ command` with light shell
// highlighting. Escape-first: the command is cut into tokens and every token
// is a text node or a span's textContent, never markup, so a command holding
// `<script>` reads as exactly that.

// One token per match, in order, and every character lands in one: the last
// alternative takes whatever the others don't, so the tokens always rejoin
// to the command.
const SHELL_TOKEN_RE = /\s+|'[^']*'?|"(?:\\.|[^"\\])*"?|\d*>>?(?:&\d+)?|&&|\|\||\$\(|[|;&<()]|\$\{[^}]*\}?|\$(?:env:)?\w+|[^\s'"|;&<>()$]+|[\s\S]/g;
// After these the next word is a command again.
const SHELL_CMD_AFTER = new Set(['&&', '||', '|', ';', '&', '$(', '(']);

function shellTokens(command) {
  const out = [];
  let expectCmd = true;
  const re = new RegExp(SHELL_TOKEN_RE.source, 'g');
  let m;
  while ((m = re.exec(command))) {
    const t = m[0];
    let kind = '';
    if (/^\s/.test(t)) {
      if (t.indexOf('\n') !== -1) expectCmd = true;
    } else if (t.charAt(0) === '\'' || t.charAt(0) === '"') {
      kind = 'str';
      expectCmd = false;
    } else if (SHELL_CMD_AFTER.has(t)) {
      kind = 'op';
      expectCmd = true;
    } else if (/^\d*>|^[<)]$/.test(t)) {
      kind = 'op';
    } else if (t.charAt(0) === '$' && t.length > 1) {
      kind = 'var';
      expectCmd = false;
    } else if (expectCmd && !/^\w+=/.test(t)) {
      kind = 'cmd';
      expectCmd = false;
    } else if (t.charAt(0) === '-' && t.length > 1) {
      kind = 'flag';
    }
    out.push([kind, t]);
  }
  return out;
}

function cmdBlock(command) {
  const el = document.createElement('pre');
  el.className = 'tr-cmd';
  const prompt = document.createElement('span');
  prompt.className = 'tr-cmd-prompt';
  prompt.setAttribute('aria-hidden', 'true');
  prompt.textContent = '$ ';
  const text = document.createElement('span');
  text.className = 'tr-cmd-text';
  shellTokens(command).forEach(function (tok) {
    if (!tok[0]) {
      text.append(tok[1]);
      return;
    }
    const span = document.createElement('span');
    span.className = 'sh-' + tok[0];
    span.textContent = tok[1];
    text.appendChild(span);
  });
  el.append(prompt, text);
  return el;
}

// Claude opens a failed command's output with "Exit code N" (every failed
// Bash result on the dev box did); no other harness records one. The code
// moves to the header, where it reads quietly, instead of leading the output.
const EXIT_LINE_RE = /^Exit code (\d+)[ \t]*(?:\r?\n|$)/;

function stepCard(e, session) {
  const a = e.action;
  const ran = !!(a && a.verb === 'ran');
  const card = document.createElement('div');
  card.className = 'tr-cmd-card';
  const head = document.createElement('div');
  head.className = 'tr-cmd-head';
  const tool = document.createElement('span');
  tool.className = 'tr-cmd-tool';
  tool.textContent = e.name || 'tool';
  head.appendChild(tool);
  if (a && a.path) {
    // "folder / name" (#1476), never the whole absolute path; that stays on hover.
    const f = fileParts(a.path, session);
    const where = document.createElement('span');
    where.className = 'tr-cmd-path';
    where.title = a.path;
    where.textContent = f.dir ? f.dir + ' / ' + f.base : f.base;
    head.appendChild(where);
  }
  let result = e.result;
  const exit = ran && e.error === true && result != null ? EXIT_LINE_RE.exec(result) : null;
  if (exit) {
    result = result.slice(exit[0].length);
    const code = document.createElement('span');
    code.className = 'tr-cmd-exit';
    code.textContent = 'exit ' + exit[1];
    head.appendChild(code);
  }
  card.appendChild(head);
  if (ran) card.appendChild(cmdBlock(a.command));
  else if (!(a && a.path) && e.summary) card.appendChild(pre(e.summary, false, 'tr-cmd tr-cmd-plain'));
  if (result != null) {
    card.appendChild(pre(result, e.result_truncated, 'tr-out'));
  } else {
    const none = document.createElement('div');
    none.className = 'tr-trunc tr-out-none';
    none.textContent = 'no result on this page';
    card.appendChild(none);
  }
  return card;
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
// The session the cached object URLs belong to (null: none). Only
// `resetBlobs` changes it, as the Chat pane opens and closes, so a fetch that
// lands after the pane moved on drops its URL instead of caching it.
let blobOwner = null;

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
    if (blobOwner !== sid) {
      URL.revokeObjectURL(objectUrl);
      return;
    }
    thumbUrls.set(url, objectUrl);
  }
  btn.querySelector('img').src = objectUrl;
  btn.disabled = false;
}

// Run `load(el)` once `el` scrolls near the viewport: a thumbnail's bytes, or
// a sent file's (#1530).
function observeLazy(el, load) {
  el._trLoad = load;
  if (!('IntersectionObserver' in window)) {
    load(el);
    return;
  }
  if (!thumbObserver) {
    thumbObserver = new IntersectionObserver(function (seen) {
      seen.forEach(function (hit) {
        if (!hit.isIntersecting) return;
        thumbObserver.unobserve(hit.target);
        hit.target._trLoad(hit.target);
      });
    }, { rootMargin: '200px' });
  }
  thumbObserver.observe(el);
}

// Small lazy thumbnails for an entry's images; a tap opens the full size in
// the app's image overlay. Null outside the Chat pane (no `session` to ask).
function thumbs(refs, session) {
  if (!refs || !refs.length || !session) return null;
  const wrap = document.createElement('div');
  wrap.className = 'tr-thumbs';
  refs.forEach(function (ref, i) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'tr-thumb';
    btn.disabled = true;
    btn.dataset.sid = session.session_id;
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
    observeLazy(btn, loadThumb);
  });
  return wrap;
}

// Drop every cached object URL and hand the cache to session `sid` (null:
// none) — the Chat pane's open and close.
export function resetBlobs(sid) {
  blobOwner = sid;
  thumbUrls.forEach(function (objectUrl) { URL.revokeObjectURL(objectUrl); });
  thumbUrls.clear();
  fileBlobs.forEach(function (got) { URL.revokeObjectURL(got.objectUrl); });
  fileBlobs.clear();
}

// --- files the agent sent (#1530) -------------------------------------------
//
// Two sources, one card. An outbox entry (any harness: the agent posted the
// file to its session's outbox, and the transcript responses carry the list)
// becomes a `{kind: 'file'}` entry merged into the conversation by time; a
// Claude SendUserFile call is a tool call with `sent_files` refs. Either way
// the bytes come from a passkey-gated route, fetched like a thumbnail once
// the card scrolls near, and the card shows an image or a PDF inline, a text
// file's opening lines, or the server's reason for refusing it. Every name
// and caption goes in as text, never markup.

const fileBlobs = new Map();
const FILE_PREVIEW_CHARS = 3000;

function sessionPath(sid) {
  return '/api/claude-code/sessions/' + encodeURIComponent(sid);
}

function sentFileUrl(sid, ref) {
  return sessionPath(sid) + '/transcript/file?offset=' + ref.offset + '&n=' + ref.n;
}

function outboxFileUrl(sid, id) {
  return sessionPath(sid) + '/outbox/' + encodeURIComponent(id);
}

function isSentFile(e) {
  return e.kind === 'file' ||
    (e.kind === 'tool_call' && !e.sidechain && Array.isArray(e.sent_files) && e.sent_files.length > 0);
}

// The type a card's object URL carries, from the kind the card was built for
// rather than the response header: the Open link and the PDF frame are
// same-origin blob URLs that keep none of the route's nosniff/sandbox
// headers, so nothing the server sends may turn one into a page.
function blobType(kind, served) {
  if (kind === 'image') return /^image\/(png|jpeg|gif|webp)$/.test(served) ? served : 'application/octet-stream';
  if (kind === 'pdf') return 'application/pdf';
  if (kind === 'archive') return 'application/octet-stream';
  return 'text/plain;charset=utf-8';
}

// The bytes behind a card, once per URL while this session's Chat is open.
// Throws an Error whose message is the server's own reason for a refusal.
async function fetchFileBlob(sid, url, kind) {
  const got = fileBlobs.get(url);
  if (got) return got;
  const tt = await ensureTerminalToken();
  const res = await api(url, { headers: authHeaders({ terminalToken: tt }) });
  if (!res.ok) {
    let message = 'couldn’t load it (status ' + res.status + ')';
    try {
      const body = await res.json();
      if (body && body.detail && body.detail.message) message = body.detail.message;
    } catch (_exc) { /* not JSON: keep the status */ }
    throw new Error(message);
  }
  const raw = await res.blob();
  const blob = new Blob([raw], { type: blobType(kind, raw.type) });
  const entry = { blob: blob, objectUrl: URL.createObjectURL(blob) };
  // A view that closed meanwhile has already dropped its URLs.
  if (blobOwner !== sid) {
    URL.revokeObjectURL(entry.objectUrl);
    throw new Error('the chat closed');
  }
  fileBlobs.set(url, entry);
  return entry;
}

function fmtBytes(n) {
  if (typeof n !== 'number') return '';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return Math.round(n / 1024) + ' KB';
  return (Math.round(n / (1024 * 102.4)) / 10) + ' MB';
}

function fileNote(card, text) {
  const note = document.createElement('div');
  note.className = 'tr-file-note';
  note.textContent = text;
  card.appendChild(note);
}

async function loadFileCard(card) {
  let got;
  try {
    got = await fetchFileBlob(card.dataset.sid, card._trUrl, card.dataset.kind);
  } catch (exc) {
    if (!card.isConnected) return;
    card.classList.add('tr-file-failed');
    fileNote(card, 'Not shown: ' + exc.message);
    return;
  }
  if (!card.isConnected) return;
  const open = card.querySelector('.tr-file-open');
  open.href = got.objectUrl;
  open.hidden = false;
  const kind = card.dataset.kind;
  const name = card._trName;
  if (kind === 'image') {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'tr-file-image';
    btn.setAttribute('aria-label', 'Open ' + name + ' full size');
    const img = document.createElement('img');
    img.alt = name;
    img.src = got.objectUrl;
    btn.appendChild(img);
    btn.addEventListener('click', function (ev) {
      ev.stopPropagation();
      openImageLightbox(got.objectUrl, name);
    });
    card.appendChild(btn);
  } else if (kind === 'archive') {
    // Download-only: the bytes are never rendered. The head's Download link
    // is the whole card, and it is ready before the tap (see fileCard).
  } else if (kind === 'pdf') {
    const frame = document.createElement('iframe');
    frame.className = 'tr-file-pdf';
    frame.title = name;
    frame.src = got.objectUrl;
    card.appendChild(frame);
  } else {
    const text = await got.blob.text();
    if (!card.isConnected) return;
    const cut = text.length > FILE_PREVIEW_CHARS;
    const preview = document.createElement('pre');
    preview.className = 'tr-file-preview';
    preview.textContent = cut ? text.slice(0, FILE_PREVIEW_CHARS) : text;
    card.appendChild(preview);
    if (cut) fileNote(card, 'Preview cut short — Open shows the whole file');
  }
}

// One file's card: its name, size and an Open link (live once the bytes are
// in; Download for an archive), then the content. A null `kind` is a type the server will refuse; the
// card still asks, so it shows the server's own reason. No `url` (the Life OS
// viewer, which has no session to ask) leaves the head alone. `sid` is the
// session the url asks.
function fileCard(name, kind, size, url, sid) {
  const card = document.createElement('div');
  card.className = 'tr-file-card';
  card.dataset.kind = kind || '';
  card._trName = name;
  card._trUrl = url;
  const head = document.createElement('div');
  head.className = 'tr-file-head';
  const nameEl = document.createElement('span');
  nameEl.className = 'tr-file-name';
  nameEl.textContent = name;
  nameEl.title = name;
  head.appendChild(nameEl);
  const sizeText = fmtBytes(size);
  if (sizeText) {
    const sizeEl = document.createElement('span');
    sizeEl.className = 'tr-file-size';
    sizeEl.textContent = sizeText;
    head.appendChild(sizeEl);
  }
  const open = document.createElement('a');
  open.className = 'tr-file-open';
  if (kind === 'archive') {
    // iOS drops a download started after an await, so the bytes are fetched
    // ahead (the lazy loader, as the card scrolls near) and the tap only
    // follows a ready blob: link. `download` is a property, never markup.
    open.textContent = 'Download';
    open.download = name;
  } else {
    open.textContent = 'Open';
    open.target = '_blank';
  }
  open.rel = 'noopener';
  open.hidden = true;
  head.appendChild(open);
  card.appendChild(head);
  if (sid && url) {
    card.dataset.sid = sid;
    observeLazy(card, loadFileCard);
  }
  return card;
}

// The turn part for one send: the caption, then a card per file. `session`
// serves the bytes (null: none).
function renderSentFiles(e, session) {
  const li = document.createElement('li');
  li.className = 'tr-file-item';
  const sid = session ? session.session_id : null;
  const files = e.kind === 'file'
    ? [{ name: e.file.name, kind: e.file.kind, size: e.file.size, url: sid && outboxFileUrl(sid, e.file.id) }]
    : e.sent_files.map(function (ref) {
      return { name: ref.name, kind: ref.kind, size: null, url: sid && sentFileUrl(sid, ref) };
    });
  const caption = e.kind === 'file' ? e.file.caption : e.caption;
  if (caption) {
    const cap = document.createElement('div');
    cap.className = 'tr-file-caption';
    cap.textContent = caption;
    li.appendChild(cap);
  }
  files.forEach(function (f) {
    li.appendChild(fileCard(f.name || 'file', f.kind || null, f.size, f.url || null, sid));
  });
  return li;
}

// --- links open in the PC's browser (#1274) --------------------------------
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
// has. `entries` is the prompt, or every reply fragment of the turn (#1475).
// A capped fragment then fetches the uncapped one (#985's ``/transcript/
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

// --- the rest of a capped turn (#1745) ------------------------------------------
//
// A very long prompt or reply arrives capped. Where there is a way to ask for
// the rest (a session, and the offset #985's ``/transcript/entry`` route
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
    '/api/claude-code/sessions/' + encodeURIComponent(sid) +
      '/transcript/entry?offset=' + encodeURIComponent(e.offset)
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
// lets the turn grow an Edited N files card once it is over (#1477). `sid`
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
// part follows it (#1476). `live` is the step in progress, if any.
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
// keeps it: the work still has a trace (#1477).
export function markSilent(li) {
  const quiet = !turnReplies(li).length && !li._trEdited &&
    !li._trParts.querySelector(':scope > .tr-ask-item, :scope > .tr-plan-item, :scope > .tr-file-item');
  li.classList.toggle('tr-turn-silent', quiet);
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
// against and never runs it. Which files count as edited, and what a tap
// opens, are the Chat pane's (session-transcript.js syncFileChips).

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

// `orig` is the node the chip stands in for, put back when the chips are redone.
// A tap calls `onOpen(chip, file)`.
function fileChip(hit, orig, onOpen) {
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
  chip.addEventListener('click', function () { onOpen(chip, hit.file); });
  return chip;
}

// Text a bare path may sit in: prose, never a link, a code span or listing,
// a fenced block's header or a control.
function chipText(node) {
  const p = node.parentElement;
  return !!p && !p.closest('a, code, pre, button, .md-block');
}

// Chip every name in reply `body` that resolves to one of `files` (as
// edited-card.js's foldEdits names them).
export function chipFiles(body, files, onOpen) {
  body.querySelectorAll('code').forEach(function (code) {
    if (code.closest('pre, a, button')) return;
    const hit = resolveFile(code.textContent, files, false);
    if (hit) code.replaceWith(fileChip(hit, code, onOpen));
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
      frag.appendChild(fileChip(hit, document.createTextNode(run), onOpen));
      last = m.index + run.length;
    }
    if (!last) return;
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    textNode.parentNode.replaceChild(frag, textNode);
  });
}

export function unchipFiles(body) {
  body.querySelectorAll('.tr-file-chip').forEach(function (chip) { chip.replaceWith(chip._trOrig); });
  body.normalize();
}

// --- the step in progress (#1532) ------------------------------------------
//
// A step line whose newest call is still running shows a counter in its
// duration's place. Which call that is, and the tick that redraws it, are the
// Chat pane's (session-transcript.js liveStep / syncLiveStep).

// "1:05", "1:02:03": an elapsed counter, as the strip's activity line reads.
export function fmtElapsed(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  const pad = function (n) { return String(n).padStart(2, '0'); };
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return (h ? h + ':' + pad(m) : String(m)) + ':' + pad(s % 60);
}

function liveStepEl(e) {
  const el = document.createElement('span');
  el.className = 'tr-step-live';
  el._trEntry = e;
  el.innerHTML = icon('timer', 'tr-step-live-icon');
  const text = document.createElement('span');
  text.className = 'tr-step-live-text';
  text.textContent = fmtElapsed(Date.now() - Date.parse(e.timestamp));
  el.appendChild(text);
  return el;
}

// --- per-step diffs (#1349) -------------------------------------------------
//
// An edit/write call's `action.diff` carries the page's share of its diff
// (capped per step, server-side); the rest is one request away through the
// step's own ref ({offset, n}). A failed call carries no diff and keeps
// today's body, as does any call whose tool the server doesn't recognise.

// `bare` leaves the "folder / name" header off: the Changed files panel's
// This turn scope (#1477) draws a step under the file's own row. `session`
// shortens the path and serves the full diff (null: neither).
export function stepDiff(e, bare, session) {
  const a = e.action;
  if (!a || !a.diff || e.error === true || (a.verb !== 'edited' && a.verb !== 'wrote')) return null;
  const wrap = document.createElement('div');
  wrap.className = 'tr-diff-wrap';
  if (!bare) {
    // Headed "folder / name" (#1476); the whole path stays on hover.
    const path = document.createElement('div');
    path.className = 'tr-diff-path';
    path.title = a.path || '';
    const f = fileParts(a.path, session);
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
  if (a.diff.truncated) wrap.appendChild(fullDiffControl(wrap, a.diff, session ? session.session_id : null));
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
function fullDiffControl(wrap, diff, sid) {
  if (!sid || diff.offset == null) return truncNote('Diff truncated');
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
// single tool call can be opened without expanding its siblings. `opts` is
// appendEntries' (its toolErrors and session).
function renderItem(e, opts) {
  const d = document.createElement('details');
  d.className = 'tr-item tr-item-' + e.kind;
  tagKey(d, e, 'item');
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
    // A call that carries its own description (#1532) is named by it, its
    // command the hint.
    const a = e.action;
    if (a && a.verb === 'ran' && described(e)) {
      name.textContent = e.label;
      hint.textContent = firstLine(a.command, 80);
      hint.classList.add('tr-item-cmd');
    } else if (a && a.verb === 'ran') {
      name.textContent = firstLine(a.command, 80);
      name.classList.add('tr-item-cmd');
    } else if (a && (a.verb === 'edited' || a.verb === 'wrote' || a.verb === 'read')) {
      const f = fileParts(a.path, opts.session);
      name.textContent = f.base;
      hint.textContent = f.dir;
      s.title = a.path;
      if (a.verb !== 'read' && e.error !== true) delta = deltaEl(a.added, a.removed, 'tr-item-delta');
    } else {
      name.textContent = e.name || 'tool';
      hint.textContent = e.label || e.summary || '';
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
    // An edit opens as its diff (#1349) and nothing else: the tool's own
    // "file has been updated" line was a second box saying less (#1476).
    // Anything else opens as a card: header, input, output (#1532).
    const diff = stepDiff(e, false, opts.session);
    if (diff) {
      body.appendChild(diff);
    } else {
      body.appendChild(stepCard(e, opts.session));
      const shots = e.result != null ? thumbs(e.result_images, opts.session) : null;
      if (shots) body.appendChild(shots);
    }
  } else {
    body.appendChild(pre(e.text, e.truncated));
    const shots = thumbs(e.images, opts.session);
    if (shots) body.appendChild(shots);
  }
  // The honest third state: this harness cannot say whether the call
  // failed. Said here, in the body, rather than as a marker on every row —
  // it is the answer to "did this work?", and that question is asked at the
  // moment the card is opened, not while it is folded.
  const note = toolOutcomeNote(e, opts.toolErrors);
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

// Whether a call carries its own description (#1532): the server's `label`
// is the description when the harness recorded one, else the summary.
function described(e) {
  return !!e.label && e.label !== e.summary;
}

// One step in plain words (#1532): its description, else what it did
// ("Ran pytest -q", "Edited main.js"), else the tool and its summary.
function stepText(e) {
  if (described(e)) return e.label;
  const a = e.action;
  if (a && a.verb === 'ran') return firstLine(a.command, 80);
  if (a && a.path && VERB_ICON[a.verb]) {
    return a.verb.charAt(0).toUpperCase() + a.verb.slice(1) + ' ' + baseName(a.path);
  }
  const what = firstLine(e.label || e.summary, 80);
  return (e.name || 'tool') + (what ? ' ' + what : '');
}

// The newest tool call a run holds, or null.
function latestCall(run) {
  for (let i = run.length - 1; i >= 0; i -= 1) {
    if (run[i].kind === 'tool_call') return run[i];
  }
  return null;
}

// The label (#1532): the newest step in plain words, then, for a run of
// more than one entry, the counts in muted secondary text. A run with no
// tool call keeps the counts alone. Counts: verbs muted, what they acted on
// in the text colour while they lead the line.
function fillStepLabel(el, parts, latest) {
  el.textContent = '';
  let counts = el;
  if (latest) {
    const now = document.createElement('span');
    now.className = 'tr-step-now';
    now.textContent = stepText(latest);
    el.appendChild(now);
    if (!parts.length) return;
    counts = document.createElement('span');
    counts.className = 'tr-step-counts';
    counts.append(' · ');
    el.appendChild(counts);
  }
  parts.forEach(function (p, i) {
    if (i) counts.append(', ');
    const verb = i === 0 ? p[0].charAt(0).toUpperCase() + p[0].slice(1) : p[0];
    if (verb) counts.append(verb + ' ');
    const what = document.createElement('b');
    what.textContent = p[1];
    counts.appendChild(what);
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
function renderRun(run, opts) {
  const li = document.createElement('li');
  li.className = 'tr-run';
  // The entries this line holds, kept on the node so a later tick can merge
  // more into it and recompute the summary (#1050).
  li._trRun = run.slice();
  const d = document.createElement('details');
  d.className = 'tr-step';
  tagKey(d, run[0], 'step');
  const s = document.createElement('summary');
  s.className = 'tr-step-line';
  s.innerHTML = icon('chevron-right', 'tr-step-chevron');
  const label = document.createElement('span');
  label.className = 'tr-step-label';
  s.appendChild(label);
  d.appendChild(s);
  const body = document.createElement('div');
  body.className = 'tr-step-items';
  run.forEach(function (e) { body.appendChild(renderItem(e, opts)); });
  d.appendChild(body);
  li.appendChild(d);
  syncRunSummary(li, opts.live);
  return li;
}

// --- live refresh rendering (#1050) ---------------------------------------
//
// Every disclosure in a live region is tagged with the byte offset of the
// entry it renders, so an open card can be found again after the region is
// rebuilt. Offsets are stable identities: an entry keeps the offset of the
// line it started at for as long as that file is not rewritten.
//
// The key is namespaced by what kind of disclosure it is (#1531): a turn that
// opens with a tool call, its step line and the call's own item all render
// the same entry, so a bare offset let a folded step line's state be restored
// onto the open turn and a new reply arrived folded.

export function tagKey(el, entry, kind) {
  if (entry && entry.offset != null) el.dataset.trKey = kind + ':' + entry.offset;
}

// A step line, recomputed from the entries it now holds and from what
// follows it — called on every merge and every turn-head sync, so a run
// that grew keeps an honest count and its duration ends at the reply that
// arrived after it. The trailing parts (total +N −M, "N failed", duration)
// are rebuilt in that order; a failure has to read while the line is
// closed, and none of them may be cut by the label's ellipsis. `live` is
// the step in progress (null: none).
export function syncRunSummary(li, live) {
  const run = li._trRun || [];
  const line = li.querySelector('.tr-step-line');
  const sum = runSummary(run);
  const latest = latestCall(run);
  fillStepLabel(line.querySelector('.tr-step-label'), latest && run.length < 2 ? [] : sum.parts, latest);
  line.querySelectorAll(':scope > .tr-step-delta, :scope > .tr-fail-count, :scope > .tr-step-dur, :scope > .tr-step-live')
    .forEach(function (n) { n.remove(); });
  if (sum.total) line.appendChild(deltaEl(sum.total.added, sum.total.removed, 'tr-step-delta'));
  const failed = failedCount(run);
  if (failed) {
    const chip = document.createElement('span');
    chip.className = 'tr-fail-count';
    chip.textContent = failed + ' failed';
    line.appendChild(chip);
  }
  // The step in progress counts up in the duration's place (#1532).
  if (live && run.indexOf(live) !== -1) {
    line.appendChild(liveStepEl(live));
    return;
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

// The Life OS conversation viewer's mount (#1119): this same renderer over a
// parsed capture rather than a second one (#979). It has no session, so
// nothing here asks a route: no thumbnails, no file bytes, no full diff, and
// a truncated turn's copy upgrade has nowhere to go (a capture's turns are
// never truncated).
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
