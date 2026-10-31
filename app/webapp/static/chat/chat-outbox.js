/* The Chat pane's outbox cursor: files the agent posted beside the
 * transcript pages, merged into the conversation by time, and the newest
 * agent reply's full text for read-aloud.
 */

import { sessionRoute } from '../api.js';
import { terminalJsonApi } from '../webauthn.js';
import { isTurn } from './transcript-render.js';
import { view } from './session-transcript.js';

// --- the outbox cursor -----------------------------------------------
//
// Files the agent posted to its session's outbox arrive beside the
// transcript pages and render as `{kind: 'file'}` entries (transcript-render.js
// draws the card), merged into the conversation by time. A file older than
// everything loaded waits for the older page that reaches it.

function entryTime(e) {
  if (!e || e.timestamp == null) return null;
  const t = new Date(e.timestamp).getTime();
  return Number.isNaN(t) ? null : t;
}

// The earliest time a set of entries carries (-Infinity for none), so an
// outbox file older than everything loaded waits for an older page.
export function oldestTime(entries) {
  let oldest = Infinity;
  entries.forEach(function (e) {
    const t = entryTime(e);
    if (t != null && t < oldest) oldest = t;
  });
  return oldest === Infinity ? -Infinity : oldest;
}

// `entries` with `files` slotted in by time: each file goes before the first
// entry stamped after it; entries with no usable time keep their place.
export function mergeFiles(entries, files) {
  if (!files.length) return entries;
  const pending = files.slice().sort(function (a, b) { return entryTime(a) - entryTime(b); });
  const out = [];
  entries.forEach(function (e) {
    const t = entryTime(e);
    while (pending.length && t != null && entryTime(pending[0]) < t) out.push(pending.shift());
    out.push(e);
  });
  return out.concat(pending);
}

// The files of a transcript response that aren't on screen yet: those at or
// after `since` to render now, the rest held back for an older page.
export function takeOutbox(body, since) {
  if (!view || body.outbox_rev === undefined) return [];
  view.outboxRev = body.outbox_rev;
  const now = [];
  (body.outbox || []).forEach(function (f) {
    if (!f || !f.id || view.outboxShown.has(f.id)) return;
    view.outboxShown.add(f.id);
    const e = { kind: 'file', timestamp: f.timestamp, file: f };
    const t = entryTime(e);
    if (t == null || t >= since) now.push(e);
    else view.outboxHeld.push(e);
  });
  return now;
}

// The held files an older page now reaches back to.
export function releaseHeld(since) {
  const held = view.outboxHeld || [];
  view.outboxHeld = held.filter(function (f) { return entryTime(f) < since; });
  return held.filter(function (f) { return entryTime(f) >= since; });
}

// The newest assistant entry's text for read-aloud in Chat mode / a detached
// session — the same reply provider terminal-readaloud.js otherwise
// reads from the live xterm buffer. Parallels copyTurn()'s upgrade
// (transcript-render.js): the loaded page's text first, then the uncapped
// /transcript/entry fetch when that page capped it, so a long reply is never
// read out silently shortened.
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
      sessionRoute(sid, '/transcript/entry?offset=' + encodeURIComponent(e.offset))
    );
    if (body && body.available) return body.text || '';
  } catch (exc) { /* fall through to the capped text already loaded */ }
  return e.text || '';
}
