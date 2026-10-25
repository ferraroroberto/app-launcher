/* Images and sent files in a transcript: lazy thumbnails, and a card per
 * file the agent sent. The one state the renderer keeps is here: the media
 * cache, owned by whichever session `resetBlobs` last named.
 */

import { api, authHeaders } from '../api.js';
import { fillHtmlCard, openImageLightbox } from '../lightbox.js';
import { ensureTerminalToken } from '../webauthn.js';

// --- image thumbnails ---------------------------------------------
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
// a sent file's.
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
export function thumbs(refs, session) {
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

// --- files the agent sent -------------------------------------------
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

export function isSentFile(e) {
  return e.kind === 'file' ||
    (e.kind === 'tool_call' && !e.sidechain && Array.isArray(e.sent_files) && e.sent_files.length > 0);
}

// The type a card's object URL carries, from the card's kind, never the response
// header: the Open link and the PDF frame are same-origin blob URLs without the
// route's nosniff/sandbox headers, so nothing the server sends may make one a
// page. HTML stays opaque bytes: it renders only in lightbox.js's sandboxed frames.
function blobType(kind, served) {
  if (kind === 'image') return /^image\/(png|jpeg|gif|webp)$/.test(served) ? served : 'application/octet-stream';
  if (kind === 'pdf') return 'application/pdf';
  if (kind === 'archive' || kind === 'html') return 'application/octet-stream';
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
  const kind = card.dataset.kind;
  const name = card._trName;
  const open = card.querySelector('.tr-file-open');
  if (kind !== 'html') open.href = got.objectUrl;
  open.hidden = false;
  // An archive is download-only: its head link, ready before the tap (fileCard), is the card.
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
  } else if (kind === 'html') {
    await fillHtmlCard(card, got, name);
  } else if (kind === 'pdf') {
    const frame = document.createElement('iframe');
    frame.className = 'tr-file-pdf';
    frame.title = name;
    frame.src = got.objectUrl;
    card.appendChild(frame);
  } else if (kind !== 'archive') {
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
// in; Download for an archive; a button opening the viewer for HTML), then the
// content. A null `kind` is a type the server will refuse; the card still asks,
// so it shows the server's own reason. No `url` (the Life OS viewer, which has
// no session to ask) leaves the head alone. `sid` is the session the url asks.
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
  const open = document.createElement(kind === 'html' ? 'button' : 'a');
  open.className = 'tr-file-open';
  open.textContent = kind === 'archive' ? 'Download' : 'Open';
  if (kind === 'archive') {
    // iOS drops a download started after an await, so the lazy loader fetches the bytes
    // ahead and the tap only follows a ready blob: link. `download` is a property, never markup.
    open.download = name;
  } else if (kind === 'html') {
    open.type = 'button';
  } else {
    open.target = '_blank';
    open.rel = 'noopener';
  }
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
export function renderSentFiles(e, session) {
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
