/* The "Glance" UI kit: the shared parts every tab
 * moves onto, so one fact is drawn one way everywhere.
 *
 *   chip()          one chip in four tones, for exceptions only
 *   avatar()        a 36px squircle holding a glyph, with an alive badge
 *   sessionRowBody() the session row's avatar + one-line title + one-line meta
 *   sessionStatusBits() a session's Board status + age, the same words on every tab
 *
 * The tone map, used on every tab:
 *   neutral    normal or a plain fact (detached, paused, stale)
 *   accent     interactive or in progress (in progress, building)
 *   attention  needs you soon or ahead of plan (needs you, due, uncommitted)
 *   danger     broken or failed (failed, down, stalled)
 * A normal state gets no chip at all. Success is never a chip: green is only
 * the avatar's alive badge (and history marks such as run outcomes).
 *
 * Imports nothing that touches the DOM at load time, so the usage meter
 * (which builds on this) stays importable under plain Node for its tests.
 */

import { icon } from './_vendored/icons/icons.js';
import { brandIconEl } from './dom-utils.js';
import { fmtDuration } from './shared/format.js';

export const CHIP_TONES = ['neutral', 'accent', 'attention', 'danger'];

// `extraClass` is a hook for tests and callers, never a second style: every
// chip is drawn by `.chip[data-tone]` alone.
export function chip(text, tone, extraClass) {
  const el = document.createElement('span');
  el.className = 'chip' + (extraClass ? ' ' + extraClass : '');
  el.dataset.tone = CHIP_TONES.indexOf(tone) >= 0 ? tone : 'neutral';
  el.textContent = text;
  return el;
}

// badge: 'alive' (green, the process is up), 'down' (red, it should be up
// but is not), 'crown' (the fleet chief, instead of the alive badge), or
// anything else for no badge (not running). `glyph` is an element (a brand
// mark) or a sprite glyph name.
export function avatar(glyph, badge, extraClass) {
  const el = document.createElement('span');
  el.className = 'avatar' + (extraClass ? ' ' + extraClass : '');
  if (typeof glyph === 'string') {
    el.insertAdjacentHTML('beforeend', icon(glyph, 'avatar-glyph'));
  } else if (glyph) {
    glyph.classList.add('avatar-glyph');
    el.appendChild(glyph);
  }
  if (badge === 'alive' || badge === 'down' || badge === 'crown') {
    const dot = document.createElement('span');
    dot.className = 'avatar-badge';
    dot.dataset.badge = badge;
    if (badge === 'crown') {
      // `board-chief-crown` is the hook both tabs' chief tests key on.
      dot.classList.add('board-chief-crown');
      dot.innerHTML = icon('crown');
      dot.title = 'Fleet chief';
    } else {
      dot.title = badge === 'alive' ? 'Running' : 'Down';
    }
    el.appendChild(dot);
  }
  el.dataset.badge = badge || 'none';
  return el;
}

// A session's Board status words (shared here so
// the Board's rows and the Code tab's read one table). The hook-written
// needs-you is split server-side into five caller-actionable values
// (src/board/transcript.py::_refine_waiting_status), the raw "needs-you"
// string itself never reaches a card's status field. tool-pending is a
// pending ordinary tool_use, genuinely ambiguous between "still executing" and
// "permission-gated", so it renders as an ambient working-ish state. `cls` is
// the Board row's state hook; no rule colours it, status is plain meta.
const STATUS_META = {
  working: { text: 'working', cls: 'is-working' },
  'tool-pending': { text: 'running', cls: 'is-tool-pending' },
  'waiting-on-task': { text: 'waiting on task', cls: 'is-waiting-on-task' },
  stalled: { text: 'stalled', cls: 'is-stalled' },
  'awaiting-decision': { text: 'awaiting decision', cls: 'is-awaiting-decision' },
  'awaiting-input': { text: 'needs you', cls: 'is-awaiting-input' },
  'idle-finished': { text: 'finished', cls: 'is-idle-finished' },
  idle: { text: 'idle', cls: 'is-idle' },
  unknown: { text: '', cls: 'is-unknown' },
};

// The chief's Stop-hook status sits in the needs-you family for nearly all
// of its life between dispatches and the server already routes it out
// of Your turn (build_board's _is_chief_card carve-out). "Resting state" means:
// idle-finished/awaiting-input/awaiting-decision are
// all ordinary waiting for a long-lived chat session, so they get the same
// "standing by" label. stalled is deliberately excluded: a chief dispatch
// outstanding this long is a real anomaly, not something to hide behind a
// benign label.
const CHIEF_STANDING_BY_STATUSES = new Set(['idle-finished', 'awaiting-input', 'awaiting-decision']);
const CHIEF_STANDING_BY_META = { text: 'standing by', cls: 'is-idle' };

// { text, cls } for a Board status; `chief` applies the standing-by label.
export function sessionStatusMeta(status, chief) {
  return chief && CHIEF_STANDING_BY_STATUSES.has(status)
    ? CHIEF_STANDING_BY_META
    : STATUS_META[status] || STATUS_META.unknown;
}

// The "status · age" bits a session row carries after its folder: the status
// word and how long since it last did anything ("working", "9m"). One
// formatter for the Board's rows and the Code tab's. `hideStatus`
// keeps the age but drops the word, for a row that already says it as a chip
// (stalled everywhere; "needs you" on the Code tab). An unknown status (the
// Code tab's placement could not be worked out) yields nothing: no word and
// no age to guess at.
export function sessionStatusBits(status, ageSeconds, opts) {
  const o = opts || {};
  if (status == null) return [];
  const meta = sessionStatusMeta(status, !!o.chief);
  return [o.hideStatus ? '' : meta.text, fmtDuration(ageSeconds)].filter(Boolean);
}

// Drop the tail whenever the line would otherwise be ellipsized, so a phone
// keeps the folder, status and age whole instead of cutting "up 1d 4h" to
// "up 1". Re-measured on every width change (rotation, the keyboard).
// The tail leaves the line's own width alone, so hiding it cannot re-trigger
// the observer. Without ResizeObserver the tail simply stays.
function fitMetaTail(metaText, tail) {
  if (typeof ResizeObserver !== 'function') return;
  new ResizeObserver(function () {
    tail.hidden = false;
    tail.hidden = metaText.scrollWidth > metaText.clientWidth;
  }).observe(metaText);
}

// The session row's leading avatar and text column, for the Code tab's rows
// (the Board's session rows are action-rows with the same avatar).
// The caller puts them in its own tap target and adds its own trailing
// accessory.
//
// opts: agentId, agentLabel, badge, title, meta (text), chips (elements),
//       iconClass / titleClass / metaClass (the caller's own hooks),
//       metaTail (optional text that ends the meta line and is the first
//       part dropped when the line is too narrow for it, e.g. "up 1d 4h").
// Returns [avatarEl, textEl].
export function sessionRowBody(opts) {
  const lead = avatar(
    brandIconEl(
      opts.agentId,
      'session-agent-icon' + (opts.iconClass ? ' ' + opts.iconClass : ''),
      opts.agentLabel
    ),
    opts.badge,
    'session-avatar'
  );

  const text = document.createElement('span');
  text.className = 'srow-text';

  const title = document.createElement('span');
  title.className = 'srow-title' + (opts.titleClass ? ' ' + opts.titleClass : '');
  title.textContent = opts.title;
  if (opts.title) title.title = opts.title;
  text.appendChild(title);

  // One line: the plain meta ellipsizes, the chips after it never do, so an
  // exception is never the part a phone cuts.
  const meta = document.createElement('span');
  meta.className = 'srow-meta' + (opts.metaClass ? ' ' + opts.metaClass : '');
  const metaText = document.createElement('span');
  metaText.className = 'srow-meta-text';
  metaText.textContent = opts.meta || '';
  if (opts.metaTail) {
    const tail = document.createElement('span');
    tail.className = 'srow-meta-tail';
    tail.textContent = (opts.meta ? ' · ' : '') + opts.metaTail;
    metaText.appendChild(tail);
    fitMetaTail(metaText, tail);
  }
  meta.appendChild(metaText);
  (opts.chips || []).forEach(function (c) { meta.appendChild(c); });
  text.appendChild(meta);

  return [lead, text];
}

// What a session's Board placement says it needs from the user, so
// the Code tab's rows and header speak the Board's status vocabulary. The
// server routes each session with the Board's own logic and sends the
// result on the sessions poll (`board_column`, `board_status`):
//   'stalled'    in Your turn because it stalled (danger)
//   'needs-you'  in Your turn otherwise: awaiting a decision or input
//   ''           anything else: working, idle, or not known
// An unknown placement (null) is never turned into "needs you".
export function sessionAttention(s) {
  if (!s || s.board_column !== 'your_turn') return '';
  return s.board_status === 'stalled' ? 'stalled' : 'needs-you';
}

// The chip for that state, or null when there is no exception to show.
export function attentionChip(s) {
  const att = sessionAttention(s);
  if (att === 'stalled') return chip('stalled', 'danger', 'session-attention');
  if (att === 'needs-you') return chip('needs you', 'attention', 'session-attention');
  return null;
}
