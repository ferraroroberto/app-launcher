/* A session's Links sheet (⋮ → Links).
 *
 * Opens over either menu's session, reads GET .../links once per open and
 * lists every artifact and URL the transcript mentioned, grouped (Artifacts,
 * GitHub, Servers & previews, Other). Each group opens folded, showing its name
 * and count; inside it the rows run newest first, each with the page's own title
 * (the server looked it up, or the row keeps its label) and when it was last
 * mentioned. A row opens the link in a new tab; its copy button puts the URL on
 * the clipboard. The server already removed credential-looking query strings,
 * so what is listed is what is safe to show and copy.
 *
 * A very long transcript is read one window at a time: the note under the
 * groups says how far back the read reached, and Read more asks for the next
 * older window and merges its links in, until the start is reached.
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
import { cutoffLabel, timeLabel } from './link-time.js';
import { mergeLinks } from './links-merge.js';

// Why the server could not read, in the words the sheet shows.
const REASON = {
  no_transcript: 'This session has no transcript to read yet',
  session_not_found: 'This session is no longer running',
  session_host_unreachable: 'The session host is not answering',
  read_failed: 'Couldn’t read the transcript',
};

const GROUP_GLYPH = {
  artifacts: 'layout-grid', github: 'git-pull-request', servers: 'globe', other: 'link',
};

let view = null;   // { sid, seq, data, paged } while the sheet is open

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
  title.textContent = item.title || item.label;
  const meta = document.createElement('span');
  meta.className = 'link-meta';
  meta.textContent = [timeLabel(item.last_seen), item.url.replace(/^https?:\/\//, '')].filter(Boolean).join(' · ');
  open.append(title, meta);
  row.appendChild(open);

  const copy = document.createElement('button');
  copy.dataset.track = 'links.copy';
  copy.type = 'button';
  copy.className = 'icon-button link-copy';
  copy.setAttribute('aria-label', 'Copy link');
  copy.title = 'Copy link';
  copy.insertAdjacentHTML('beforeend', icon('copy'));
  copy.addEventListener('click', function () { copyUrl(item.url); });
  row.appendChild(copy);
  return row;
}

// A group is a <details> cloned from the sheet's template (its fold toggle
// carries a constant data-track id in markup) and opens folded.
function group(g) {
  const section = els.linksGroupTemplate.content.firstElementChild.cloneNode(true);
  section.dataset.group = g.key;
  const head = section.querySelector('.links-group-head');
  head.insertAdjacentHTML('afterbegin', icon(GROUP_GLYPH[g.key] || 'link', 'links-group-glyph'));
  head.insertAdjacentHTML('beforeend', icon('chevron-down', 'links-group-chevron'));
  section.querySelector('.links-group-title').textContent = g.title;
  section.querySelector('.links-group-count').textContent = String(g.total);
  const body = section.querySelector('.links-group-body');
  g.links.forEach(function (item) { body.appendChild(linkRow(item, g.key)); });
  if (g.total > g.links.length) {
    const more = document.createElement('p');
    more.className = 'links-note';
    more.textContent = 'The newest ' + g.links.length + ' of ' + g.total;
    body.appendChild(more);
  }
  return section;
}

// The note under the groups: how far back the read reached and, while older
// transcript remains, the button that reads the next window.
function readNote(data) {
  const note = document.createElement('div');
  note.className = 'links-read';
  const text = document.createElement('p');
  text.className = 'links-note';
  if (data.partial) {
    const cut = cutoffLabel(data.read_from);
    text.textContent = cut ? 'Read back to ' + cut : 'Only the newest part of a very long transcript was read';
    note.appendChild(text);
    const more = document.createElement('button');
    more.dataset.track = 'links.read-more';
    more.type = 'button';
    more.className = 'button-tint links-read-more';
    more.textContent = 'Read more';
    more.addEventListener('click', function () { readMore(more); });
    note.appendChild(more);
  } else {
    text.textContent = 'Whole session read';
    note.appendChild(text);
  }
  return note;
}

function render(data) {
  const open = new Set(Array.from(els.linksBody.querySelectorAll('details[open]'),
    function (d) { return d.dataset.group; }));
  els.linksBody.replaceChildren();
  if (!data.groups.length && !data.partial) {
    setAsyncState(els.linksState, 'empty', { message: 'No links in this session', glyph: 'link' });
    return;
  }
  setAsyncState(els.linksState, 'ready');
  data.groups.forEach(function (g) {
    const section = group(g);
    if (open.has(g.key)) section.open = true;
    els.linksBody.appendChild(section);
  });
  if (data.partial || view.paged) els.linksBody.appendChild(readNote(data));
}

async function readMore(button) {
  const seq = view.seq;
  const data = view.data;
  button.disabled = true;
  button.textContent = 'Reading…';
  let older = null;
  let failure = null;
  try {
    older = await terminalJsonApi(sessionRoute(view.sid, '/links?before=' + data.offset));
  } catch (exc) {
    if (exc instanceof AuthRequiredError) return;
    failure = exc;
  }
  if (!view || view.seq !== seq) return;
  if (!older || !older.available) {
    apiFailToast('Read more failed', failure || new Error(REASON[older && older.reason] || REASON.read_failed));
    button.disabled = false;
    button.textContent = 'Read more';
    return;
  }
  view.paged = true;
  view.data = mergeLinks(data, older);
  render(view.data);
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
    view.data = data;
    view.paged = false;
    render(data);
  }
}

// `s` is a session as the list knows it (only `session_id` is read).
export function openLinks(s) {
  if (!els.linksSheet || !s) return;
  view = { sid: s.session_id, seq: 0, data: null, paged: false };
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
