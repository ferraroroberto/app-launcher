/* Swipe actions on a list row, iOS Mail style: drag a row sideways
 * to uncover one action on each edge. A partial swipe rests open on the
 * action's pane (a tap on it commits); a full swipe, or a fast flick, commits
 * the action as it is released. Life's skill rows are the one consumer:
 * Resume on the leading edge (swipe right), New on the trailing one.
 *
 * Touch and pen only: a mouse never swipes, so on a desktop the row's kebab
 * is the route, and it is also the single-pointer alternative WCAG 2.5.1
 * asks of a path gesture (the panes are aria-hidden and out of the tab
 * order). The row surface sets `touch-action: pan-y`, so vertical scrolling
 * is the browser's and is never blocked; a gesture becomes a swipe only once
 * it is clearly horizontal, and one that starts in the screen's leftmost
 * 24px is left to the system back gesture.
 *
 * The decisions are plain functions of numbers (`lockAxis`, `release`,
 * `isArmed`, `clampOffset`), exported for tests/js/swipe_row.test.mjs;
 * nothing touches the DOM at import time.
 */

export const SWIPE = {
  deadZone: 8,          // px before a gesture is read as anything
  axisRatio: 1.5,       // |dx| must beat |dy| by this much to be a swipe
  edgeZone: 24,         // px at the screen's left edge left to the system
  paneWidth: 88,        // px a pane rests open at: glyph and word
  fullRatio: 0.55,      // a release past this share of the row commits
  flickRatio: 0.30,     // a fast release past this share commits too
  flickSpeed: 0.5,      // px/ms that counts as fast
};

// 'x' once a move is clearly horizontal, 'y' once it is clearly not, null
// while it is still inside the dead zone or undecided.
export function lockAxis(dx, dy) {
  const ax = Math.abs(dx);
  const ay = Math.abs(dy);
  if (Math.max(ax, ay) < SWIPE.deadZone) return null;
  if (ax >= ay * SWIPE.axisRatio) return 'x';
  if (ay >= ax) return 'y';
  return null;
}

// Whether a pointer that went down at `clientX` may start a swipe.
export function startsInEdge(clientX) {
  return clientX < SWIPE.edgeZone;
}

// The row's offset while dragging: no further than the row is wide, and
// none toward a side that has no action.
export function clampOffset(offset, width, sides) {
  if (offset > 0 && !sides.leading) return 0;
  if (offset < 0 && !sides.trailing) return 0;
  return Math.max(-width, Math.min(width, offset));
}

// Past the full-swipe line: releasing here commits (the pane shows it).
export function isArmed(offset, width) {
  return Math.abs(offset) >= width * SWIPE.fullRatio;
}

// What a release does: 'commit' the side's action, rest 'open' on its pane,
// or 'close'. `velocity` is px/ms, signed like `offset`. A disabled action
// never commits; its pane may still rest open to show why.
export function release(offset, velocity, width, disabled) {
  if (!offset) return 'close';
  const sameWay = Math.sign(velocity) === Math.sign(offset);
  const full = isArmed(offset, width);
  const flick = sameWay && Math.abs(velocity) >= SWIPE.flickSpeed &&
    Math.abs(offset) >= width * SWIPE.flickRatio;
  if ((full || flick) && !disabled) return 'commit';
  if (Math.abs(offset) >= SWIPE.paneWidth / 2) return 'open';
  return 'close';
}

// ---------------------------------------------------------------- the DOM
// The row currently resting open; opening another closes it.
let openRow = null;

function closeOpenRow() {
  if (openRow) openRow.close();
}

function onDocumentPointerDown(ev) {
  if (openRow && !openRow.li.contains(ev.target)) closeOpenRow();
}

function onDocumentScroll() {
  closeOpenRow();
}

function pane(side, spec) {
  const el = document.createElement('span');
  el.className = 'swipe-pane swipe-pane--' + side;
  el.dataset.tone = spec.tone || 'accent';
  el.setAttribute('aria-hidden', 'true');
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.tabIndex = -1;
  btn.className = 'swipe-pane-btn';
  return { el: el, btn: btn };
}

// Fill a pane with its glyph and word, or its disabled reason.
function paintPane(p, spec, icon) {
  const disabled = !!spec.disabled;
  p.el.classList.toggle('is-disabled', disabled);
  p.btn.disabled = disabled;
  p.btn.innerHTML = icon(spec.glyph) + '<span class="swipe-pane-label"></span>';
  p.btn.querySelector('.swipe-pane-label').textContent =
    disabled ? (spec.reason || spec.label) : spec.label;
  p.el.replaceChildren(p.btn);
}

// One row's resting state: its offset, the open-row bookkeeping, and the
// click a gesture leaves behind. `ctl` is what openRow holds.
function swipeState(li, surface, sides) {
  const row = { offset: 0, drag: null, swallowClick: false };

  row.width = function () { return li.getBoundingClientRect().width || 1; };

  row.show = function (next, dragging) {
    row.offset = next;
    surface.style.transform = next ? 'translateX(' + next + 'px)' : '';
    li.classList.toggle('is-dragging', !!dragging);
    if (next) li.dataset.swipe = next > 0 ? 'leading' : 'trailing';
    else delete li.dataset.swipe;
    const action = next > 0 ? sides.leading : next < 0 ? sides.trailing : null;
    li.classList.toggle('is-armed',
      !!(dragging && action && !action.disabled && isArmed(next, row.width())));
  };

  const ctl = {
    li: li,
    close: function () {
      row.show(0, false);
      if (openRow === ctl) {
        openRow = null;
        document.removeEventListener('pointerdown', onDocumentPointerDown, true);
        document.removeEventListener('scroll', onDocumentScroll, true);
      }
    },
  };
  row.ctl = ctl;

  row.rest = function (side) {
    if (openRow && openRow !== ctl) closeOpenRow();
    row.show(side === 'leading' ? SWIPE.paneWidth : -SWIPE.paneWidth, false);
    if (openRow !== ctl) {
      openRow = ctl;
      document.addEventListener('pointerdown', onDocumentPointerDown, true);
      document.addEventListener('scroll', onDocumentScroll, true);
    }
  };
  return row;
}

// The panes go in behind the surface, in its parent; a tap on one commits
// its action (or, disabled, only closes the row).
function mountPanes(surface, sides, icon, row) {
  ['leading', 'trailing'].forEach(function (side) {
    if (!sides[side]) return;
    const p = pane(side, sides[side]);
    paintPane(p, sides[side], icon);
    p.btn.addEventListener('click', function (ev) {
      ev.stopPropagation();
      const action = sides[side];
      row.ctl.close();
      if (!action.disabled) action.onCommit();
    });
    surface.parentNode.insertBefore(p.el, surface);
  });
}

// What a released swipe does: commit, rest open on its pane, or close.
function finishSwipe(row, sides, velocity, cancelled) {
  row.swallowClick = true;
  const side = row.offset > 0 ? 'leading' : row.offset < 0 ? 'trailing' : '';
  const action = side ? sides[side] : null;
  const outcome = cancelled || !action ? 'close'
    : release(row.offset, velocity, row.width(), !!action.disabled);
  if (outcome === 'open') {
    row.rest(side);
  } else {
    row.ctl.close();
    if (outcome === 'commit') action.onCommit();
  }
}

function wirePointer(surface, sides, row) {
  surface.addEventListener('pointerdown', function (ev) {
    if (ev.pointerType === 'mouse' || !ev.isPrimary) return;
    if (row.offset) {
      // A tap on an open row closes it and does nothing else.
      row.swallowClick = true;
      row.ctl.close();
      return;
    }
    if (startsInEdge(ev.clientX)) return;
    if (openRow && openRow !== row.ctl) closeOpenRow();
    row.drag = { id: ev.pointerId, x: ev.clientX, y: ev.clientY, axis: null,
                 lastX: ev.clientX, lastT: ev.timeStamp, velocity: 0 };
  });

  surface.addEventListener('pointermove', function (ev) {
    const drag = row.drag;
    if (!drag || ev.pointerId !== drag.id) return;
    const dx = ev.clientX - drag.x;
    const dy = ev.clientY - drag.y;
    if (!drag.axis) {
      drag.axis = lockAxis(dx, dy);
      if (drag.axis === 'y') { row.drag = null; return; }
      if (drag.axis !== 'x') return;
      try { surface.setPointerCapture(ev.pointerId); } catch (exc) { /* gone */ }
    }
    const dt = ev.timeStamp - drag.lastT;
    if (dt > 0) drag.velocity = (ev.clientX - drag.lastX) / dt;
    drag.lastX = ev.clientX;
    drag.lastT = ev.timeStamp;
    row.show(clampOffset(dx, row.width(), sides), true);
  });

  function finish(ev, cancelled) {
    const drag = row.drag;
    if (!drag || ev.pointerId !== drag.id) return;
    row.drag = null;
    if (drag.axis === 'x') finishSwipe(row, sides, drag.velocity, cancelled);
  }

  surface.addEventListener('pointerup', function (ev) { finish(ev, false); });
  surface.addEventListener('pointercancel', function (ev) { finish(ev, true); });
}

function wireClickSwallow(li, surface, row) {
  // The click a drag (or a tap that closed an open row) leaves behind must
  // not also launch the row or open its menu.
  li.addEventListener('click', function (ev) {
    if (!row.swallowClick) return;
    row.swallowClick = false;
    ev.stopPropagation();
    ev.preventDefault();
  }, true);
  // A swallowed click that never came (a drag often ends without one) must
  // not eat the next real tap, on the row or on an open pane. The one
  // exception is a tap on an open row's surface, which closes it: that
  // tap's own click is the one to swallow.
  li.addEventListener('pointerdown', function (ev) {
    if (row.offset && surface.contains(ev.target)) return;
    row.swallowClick = false;
  }, true);
}

// Make `li` swipeable: `surface` is the element that slides (it holds the
// row's content), and the panes go in behind it, in its parent (the clipping
// track action-rows.js builds); `spec` is { leading, trailing }, each { label, glyph,
// tone, onCommit, disabled, reason } or absent. `icon(name)` returns a
// glyph's markup (the sprite helper, passed in so this module stays
// importable without a DOM).
export function attachSwipe(li, surface, spec, icon) {
  const sides = { leading: spec.leading || null, trailing: spec.trailing || null };
  const row = swipeState(li, surface, sides);
  mountPanes(surface, sides, icon, row);
  li.classList.add('has-swipe');
  surface.classList.add('swipe-surface');
  wirePointer(surface, sides, row);
  wireClickSwallow(li, surface, row);
  return row.ctl;
}
