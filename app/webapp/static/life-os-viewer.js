/* Life OS conversation viewer (#1119): one captured conversation, read-only,
 * chat-style.
 *
 * Opened by the conversation sheet's "Read the conversation" (#1504), as a
 * tall sheet stacked over it: ✕, Escape and Done all come back to the sheet,
 * which holds every action (Resume, Rename, Copy link, Open raw, Delete).
 * An archived capture (life-os-archived.js) opens it straight from its row,
 * with Restore in Done's place.
 * The capture is parsed server-side (`/api/life-os/file/transcript`,
 * src/life_os_capture.py) into the entry shape the session overlay's Chat
 * pane already renders, and it is rendered by that pane's own renderer
 * (transcript-render.js, #1629): one transcript surface, two mounts (#979),
 * and no dependency on the Chat pane's state. There
 * is no composer: this is a finished conversation, reopened through Resume if
 * it is to continue. The capture reader keeps speaker turns only, so there
 * are no tool-call groups to show, and the old "Show tool calls" toggle is
 * gone with the ⋮ menu that held it.
 */

import { els } from './state.js';
import { jsonApi } from './api.js';
import { openLinksOnThePc, renderEntries } from './transcript-render.js';
import { icon } from './_vendored/icons/icons.js';
import { mountScrollerPill } from './latest-pill.js';

// null while closed, else { row, openRaw, url, primary }. `seq` drops a slow
// read that lands after the viewer moved on to another conversation.
let viewer = null;
let seq = 0;

// The canonical lifecycle block — glyph, one-line reason, at most one action
// (design.md "Async data & feedback"): never a blank pane.
function showState(glyph, message, action) {
  const host = els.lifeOsViewerState;
  host.innerHTML = '';
  host.hidden = false;
  const g = document.createElement('div');
  g.innerHTML = icon(glyph);
  host.appendChild(g);
  const text = document.createElement('div');
  text.textContent = message;
  host.appendChild(text);
  if (action) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'button-ghost lifeos-viewer-raw';
    btn.textContent = action.text;
    btn.addEventListener('click', action.onTap);
    host.appendChild(btn);
  }
}

function hideState() {
  els.lifeOsViewerState.hidden = true;
}

// No raw file to fall back to (an archived capture): no action.
function openRawAction() {
  if (!viewer || !viewer.openRaw) return null;
  return {
    text: 'Open raw',
    onTap: function () { if (viewer) viewer.openRaw(viewer.row); },
  };
}

async function load() {
  const mine = ++seq;
  const row = viewer.row;
  els.lifeOsViewerList.innerHTML = '';
  showState('hourglass', 'Reading conversation…');
  let body;
  try {
    body = await jsonApi(
      viewer.url || '/api/life-os/file/transcript?path=' + encodeURIComponent(row.path)
    );
  } catch (exc) {
    if (mine !== seq || !viewer) return;
    showState('triangle-alert', exc && exc.status === 403
      ? 'Conversations are Tailscale-only (and passkey-gated). Open the ' +
        'launcher over your Tailscale URL on an enrolled device.'
      : 'Could not load this conversation.', openRawAction());
    return;
  }
  if (mine !== seq || !viewer) return;
  if (!body.available) {
    // Unparseable (no speaker turns): an honest state plus the raw file,
    // never an empty conversation.
    showState('file-text',
      'This capture has no turns this view can read.', openRawAction());
    return;
  }
  hideState();
  // The capture's agent heads its turns (#1475), as the session's does in Chat.
  els.lifeOsViewerList.appendChild(
    renderEntries(body.entries || [], 'reported', null, body.agent || row.agent)
  );
  if (body.truncated) {
    const li = document.createElement('li');
    li.className = 'tr-trunc lifeos-viewer-trunc';
    li.textContent = 'This conversation is longer than the viewer reads; open it raw for the rest.';
    els.lifeOsViewerList.appendChild(li);
  }
  els.lifeOsViewerBody.scrollTop = 0;
}

// `row` is one conversation (an index or search row); `openRaw(row)` opens
// its capture raw in the document sheet, the fallback when it can't be read.
// `opts.url` reads the transcript from elsewhere (an archived capture), and
// `opts.primary` ({ text, onTap(button) }) takes Done's place as the one
// full-width action.
export function openConvoViewer(row, openRaw, opts) {
  const d = els.lifeOsConvoViewer;
  if (!d) return;
  const o = opts || {};
  viewer = { row: row, openRaw: openRaw, url: o.url || '', primary: o.primary || null };
  els.lifeOsViewerDone.textContent = viewer.primary ? viewer.primary.text : 'Done';
  els.lifeOsViewerDone.disabled = false;
  els.lifeOsViewerTitle.textContent = row.title || row.topic || row.slug || row.file || 'Conversation';
  if (!d.open) d.showModal();
  load();
}

export function closeConvoViewer() {
  const d = els.lifeOsConvoViewer;
  if (!d) return;
  if (d.open) d.close();
}

// Escape closes the dialog natively, so the reset lives on its close event.
function reset() {
  seq += 1;
  viewer = null;
  els.lifeOsViewerList.innerHTML = '';
  hideState();
}

export function wireConvoViewer() {
  const d = els.lifeOsConvoViewer;
  if (!d) return;
  els.lifeOsViewerClose.addEventListener('click', closeConvoViewer);
  els.lifeOsViewerDone.addEventListener('click', function () {
    if (viewer && viewer.primary) viewer.primary.onTap(els.lifeOsViewerDone);
    else closeConvoViewer();
  });
  d.addEventListener('close', reset);
  // A tap on the backdrop lands on the <dialog> itself, outside the card.
  d.addEventListener('click', function (ev) {
    if (ev.target === d) closeConvoViewer();
  });
  openLinksOnThePc(els.lifeOsViewerList);
  // Opening at the top (a finished conversation reads from its start), the
  // shared ↓ Latest pill (#1140) shows whenever the end is off screen.
  mountScrollerPill(els.lifeOsViewerLatest, els.lifeOsViewerBody);
}
