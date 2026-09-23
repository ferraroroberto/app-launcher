/* Life OS conversation viewer (#1119): one captured conversation, read-only,
 * chat-style.
 *
 * Opened from the 📖 on a Conversations row. The capture is parsed
 * server-side (`/api/life-os/file/transcript`, src/life_os_capture.py) into
 * the entry shape the session overlay's Chat pane already renders, and it is
 * rendered by that pane's own renderer (session-transcript.js) — one
 * transcript surface, two mounts (#979). There is no composer: this is a
 * finished conversation, reopened through Resume if it is to continue.
 *
 * Every action lives in the bar's ⋮ menu, on the shared row-menu component
 * (the session overlay's menu, terminal-bar.js, is the model): Resume in
 * <provider> / Start new in <other>, Show/Hide tool calls, Rename, Delete,
 * Open raw. The actions themselves stay in life-os.js — this module is
 * handed them as callbacks, so the two never import each other.
 */

import { els } from './state.js';
import { jsonApi } from './api.js';
import { createRowMenu } from './row-menu.js';
import { renderEntries } from './session-transcript.js';
import { icon } from './_vendored/icons/icons.js';
import { mountScrollerPill } from './latest-pill.js';

const viewerMenu = createRowMenu('terminal-menu');

// null while closed, else { row, actions, seq }. `seq` drops a slow read
// that lands after the viewer moved on to another conversation.
let viewer = null;
let seq = 0;
// Whether folded tool-call / system runs are hidden, as in the Chat pane.
// A capture carries none today (the capture reader excludes them upstream),
// so the menu row is disabled with that reason rather than being a toggle
// that visibly does nothing.
let groupsHidden = true;
let hasGroups = false;

function syncGroups() {
  els.lifeOsViewerList.classList.toggle('tr-hide-groups', groupsHidden);
}

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

function openRawAction() {
  return {
    text: 'Open raw',
    onTap: function () { if (viewer) viewer.actions.openRaw(viewer.row); },
  };
}

// Why this conversation can't be resumed from here, said on screen rather
// than in a disabled menu row's tooltip — a phone has no hover.
function syncNote() {
  const note = els.lifeOsViewerNote;
  const reason = viewer ? viewer.actions.state(viewer.row).reason : '';
  note.hidden = !reason;
  note.innerHTML = reason ? icon('info') : '';
  if (reason) note.appendChild(document.createTextNode(' ' + reason));
}

async function load() {
  const mine = ++seq;
  const row = viewer.row;
  els.lifeOsViewerList.innerHTML = '';
  hasGroups = false;
  showState('hourglass', 'Reading conversation…');
  let body;
  try {
    body = await jsonApi(
      '/api/life-os/file/transcript?path=' + encodeURIComponent(row.path)
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
  const entries = body.entries || [];
  hasGroups = entries.some(function (e) {
    return e.kind !== 'user' && e.kind !== 'assistant';
  });
  hideState();
  els.lifeOsViewerList.appendChild(renderEntries(entries, 'reported'));
  if (body.truncated) {
    const li = document.createElement('li');
    li.className = 'tr-trunc lifeos-viewer-trunc';
    li.textContent = 'This conversation is longer than the viewer reads; open it raw for the rest.';
    els.lifeOsViewerList.appendChild(li);
  }
  els.lifeOsViewerBody.scrollTop = 0;
}

// `row` is one Conversations row; `actions` is life-os.js's callbacks:
// state(row) → {canResume, resumeEnabled, provider, reason, canHandoff,
// handoffTo}, plus resume / handoff / canLink / copyLink / rename / del /
// openRaw.
export function openConvoViewer(row, actions) {
  if (!els.lifeOsConvoViewer) return;
  viewer = { row: row, actions: actions };
  groupsHidden = true;
  syncGroups();
  viewerMenu.close();
  els.lifeOsViewerTitle.textContent = row.topic || row.slug || row.file || 'conversation';
  syncNote();
  els.lifeOsConvoViewer.hidden = false;
  load();
}

export function closeConvoViewer() {
  if (!els.lifeOsConvoViewer) return;
  seq += 1;
  viewer = null;
  viewerMenu.close();
  els.lifeOsConvoViewer.hidden = true;
  els.lifeOsViewerList.innerHTML = '';
  hideState();
}

function stateOf() {
  return viewer ? viewer.actions.state(viewer.row) : {};
}

export function wireConvoViewer() {
  if (!els.lifeOsConvoViewer) return;
  els.lifeOsViewerBack.addEventListener('click', closeConvoViewer);
  // Opening at the top (a finished conversation reads from its start), the
  // shared ↓ Latest pill (#1140) shows whenever the end is off screen.
  mountScrollerPill(els.lifeOsViewerLatest, els.lifeOsViewerBody);
  const menu = viewerMenu.attach('lifeos-viewer', els.lifeOsViewerMenu, [
    {
      glyph: 'rotate-ccw',
      className: 'lifeos-viewer-resume',
      label: function () { return 'Resume in ' + (stateOf().provider || 'Claude'); },
      text: function () { return 'Resume in ' + (stateOf().provider || 'Claude'); },
      hidden: function () { return !stateOf().canResume; },
      disabled: function () { return !stateOf().resumeEnabled; },
      title: function () { return stateOf().reason; },
      onTap: function () { if (viewer) viewer.actions.resume(viewer.row); },
    },
    {
      glyph: 'messages-square',
      className: 'lifeos-viewer-handoff',
      label: function () { return 'Start new in ' + stateOf().handoffTo; },
      text: function () { return 'Start new in ' + stateOf().handoffTo; },
      hidden: function () { return !stateOf().canHandoff; },
      onTap: function () { if (viewer) viewer.actions.handoff(viewer.row); },
    },
    {
      glyph: function () { return groupsHidden ? 'eye' : 'eye-off'; },
      className: 'lifeos-viewer-groups',
      label: function () {
        return groupsHidden
          ? 'Show tool calls and system entries'
          : 'Hide tool calls and system entries';
      },
      text: function () { return groupsHidden ? 'Show tool calls' : 'Hide tool calls'; },
      disabled: function () { return !hasGroups; },
      title: 'This capture holds no tool calls',
      onTap: function () { groupsHidden = !groupsHidden; syncGroups(); },
    },
    {
      // The same ?convo= link as the row's Copy link (#1170); the tap writes
      // the clipboard synchronously (iOS).
      glyph: 'link', className: 'lifeos-viewer-copy-link',
      label: 'Copy a link to this conversation', text: 'Copy link',
      hidden: function () { return !viewer || !viewer.actions.canLink(viewer.row); },
      onTap: function () { if (viewer) viewer.actions.copyLink(viewer.row); },
    },
    {
      glyph: 'pencil', className: 'lifeos-viewer-rename',
      label: 'Rename this conversation log', text: 'Rename',
      // The dialog and the round trip both take a while: hold the open
      // conversation's own callbacks rather than reading `viewer` after the
      // await, which a Back tap meanwhile would have nulled.
      onTap: async function () {
        const open = viewer;
        if (open && await open.actions.rename(open.row)) closeConvoViewer();
      },
    },
    {
      glyph: 'trash-2', className: 'lifeos-viewer-delete', danger: true,
      label: 'Delete this conversation log', text: 'Delete',
      onTap: async function () {
        const open = viewer;
        if (open && await open.actions.del(open.row)) closeConvoViewer();
      },
    },
    {
      glyph: 'file-text', className: 'lifeos-viewer-open-raw',
      label: 'Open the raw capture', text: 'Open raw',
      onTap: function () { if (viewer) viewer.actions.openRaw(viewer.row); },
    },
  ]);
  els.lifeOsViewerMenu.closest('.terminal-bar').appendChild(menu);
}
