/* The "Glance" UI kit (#1433, step 1/7 of #1432): the shared parts every tab
 * moves onto, so one fact is drawn one way everywhere.
 *
 *   chip()          one chip in four tones, for exceptions only
 *   avatar()        a 36px squircle holding a glyph, with an alive badge
 *   sessionRowBody() the session row's avatar + one-line title + one-line meta
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

// The session row's leading avatar and text column, shared by the Code
// tab's rows and the Board's session cards; each caller puts them in its
// own tap target (the row button / the drawer toggle) and adds its own
// trailing accessory.
//
// opts: agentId, agentLabel, badge, title, meta (text), chips (elements),
//       iconClass / titleClass / metaClass (each surface's existing hooks).
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
  meta.appendChild(metaText);
  (opts.chips || []).forEach(function (c) { meta.appendChild(c); });
  text.appendChild(meta);

  return [lead, text];
}

// What a session's Board placement says it needs from the user (#1434), so
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
