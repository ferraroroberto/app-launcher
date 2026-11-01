/* A session's Links sheet (⋮ → Links).
 *
 * Opens over either menu's session, reads GET .../links once per open and
 * lists every artifact and URL the transcript mentioned, grouped (Artifacts,
 * GitHub, Servers & previews, Other), newest first. A row opens the link in
 * a new tab; its copy button puts the URL on the clipboard. The server
 * already removed credential-looking query strings, so what is listed is
 * what is safe to show and copy.
 *
 * Every agent with a transcript reader. `hasLinks` is what both ⋮ menus gate
 * the item on.
 */

import { els } from '../state.js';
import { terminalJsonApi } from '../webauthn.js';
import { AuthRequiredError, apiFailToast, sessionRoute, toast } from '../api.js';
import { setAsyncState } from '../async-state.js';
import { hasTranscriptReader } from '../chat/session-transcript.js';
import { icon } from '../_vendored/icons/icons.js';

// Why the server could not read, in the words the sheet shows.
const REASON = {
  no_transcript: 'This session has no transcript to read yet',
  session_not_found: 'This session is no longer running',
  session_host_unreachable: 'The session host is not answering',
  read_failed: 'Couldn’t read the transcript',
};

const GROUP_GLYPH = {
  artifacts: 'layout', github: 'git-pull-request', servers: 'globe', other: 'link',
};

let view = null;   // { sid, seq } while the sheet is open

export function hasLinks(s) {
  return !!s && hasTranscriptReader(s);
}

function copyUrl(url) {
  // iOS only allows a clipboard write inside the tap gesture: writeText is
  // called synchronously from the click handler.
  navigator.clipboard.writeText(url).then(
    function () { toast('Link copied', 'good', { icon: 'link' }); },
    function (exc) { apiFailToast('Copy link failed', exc); }
  );
}

function when(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function linkRow(item, key) {
  const row = document.createElement('div');
  row.className = 'link-row';
  row.dataset.group = key;

  const open = document.createElement('a');
  open.className = 'link-open';
  open.href = item.url;
  open.target = '_blank';
  open.rel = 'noopener noreferrer';
  const title = document.createElement('span');
  title.className = 'link-title';
  title.textContent = item.label;
  const meta = document.createElement('span');
  meta.className = 'link-meta';
  meta.textContent = [item.url.replace(/^https?:\/\//, ''), when(item.last_seen)].filter(Boolean).join(' · ');
  open.append(title, meta);
  row.appendChild(open);

  const copy = document.createElement('button');
  copy.type = 'button';
  copy.className = 'icon-button link-copy';
  copy.setAttribute('aria-label', 'Copy link');
  copy.title = 'Copy link';
  copy.insertAdjacentHTML('beforeend', icon('copy'));
  copy.addEventListener('click', function () { copyUrl(item.url); });
  row.appendChild(copy);
  return row;
}

function group(g) {
  const section = document.createElement('section');
  section.className = 'links-group';
  section.dataset.group = g.key;
  const h = document.createElement('h3');
  h.className = 'links-group-title';
  h.insertAdjacentHTML('beforeend', icon(GROUP_GLYPH[g.key] || 'link'));
  h.append(' ' + g.title + ' ' + g.total);
  section.appendChild(h);
  g.links.forEach(function (item) { section.appendChild(linkRow(item, g.key)); });
  if (g.total > g.links.length) {
    const more = document.createElement('p');
    more.className = 'links-note';
    more.textContent = 'The newest ' + g.links.length + ' of ' + g.total;
    section.appendChild(more);
  }
  return section;
}

function render(data) {
  els.linksBody.replaceChildren();
  if (!data.groups.length) {
    setAsyncState(els.linksState, 'empty', { message: 'No links in this session', glyph: 'link' });
    return;
  }
  setAsyncState(els.linksState, 'ready');
  data.groups.forEach(function (g) { els.linksBody.appendChild(group(g)); });
  if (data.partial) {
    const note = document.createElement('p');
    note.className = 'links-note';
    note.textContent = 'Only the newest part of a very long transcript was read';
    els.linksBody.appendChild(note);
  }
}

async function load() {
  const seq = ++view.seq;
  els.linksBody.replaceChildren();
  setAsyncState(els.linksState, 'loading', { message: 'Reading links…', glyph: 'hourglass' });
  let data = null;
  try {
    data = await terminalJsonApi(sessionRoute(view.sid, '/links'));
  } catch (exc) {
    if (exc instanceof AuthRequiredError) return;
    data = null;
  }
  if (!view || view.seq !== seq) return;
  if (!data) {
    setAsyncState(els.linksState, 'error', { message: 'Could not load links.', retry: load });
  } else if (!data.available) {
    setAsyncState(els.linksState, 'empty', {
      message: REASON[data.reason] || REASON.read_failed, glyph: 'link',
    });
  } else {
    render(data);
  }
}

// `s` is a session as the list knows it (only `session_id` is read).
export function openLinks(s) {
  if (!els.linksSheet || !s) return;
  view = { sid: s.session_id, seq: 0 };
  if (!els.linksSheet.open) els.linksSheet.showModal();
  load();
}

function closeSheet() {
  if (els.linksSheet.open) els.linksSheet.close();
}

export function wireLinks() {
  if (!els.linksSheet) return;
  els.linksClose.addEventListener('click', closeSheet);
  els.linksDone.addEventListener('click', closeSheet);
  // Esc, the back gesture and both buttons end here: a response still in
  // flight lands nowhere.
  els.linksSheet.addEventListener('close', function () {
    if (view) view.seq += 1;
    view = null;
  });
}
