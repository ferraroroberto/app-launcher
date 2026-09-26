// Long-press hint (#1238 J-05): the phone's stand-in for a hover tooltip.
//
// A few controls stay icon-only by decision, and their glyphs aren't
// conventional (the context ring, the Terminal / Chat toggle, read-aloud,
// the terminal keys). On a desktop their `title` explains them on hover; a
// touch screen has no hover, so holding one for HOLD_MS toasts that same
// text instead. The hold never triggers the control: the click that ends it
// is swallowed, and a finger that moves is a scroll, not a hold.
import { toast } from './api.js';

const HOLD_MS = 500;
const MOVE_SLOP_PX = 10;

export function bindLongPressHint(el) {
  if (!el) return;
  let timer = null;
  let shown = false;
  let startX = 0;
  let startY = 0;

  function cancel() {
    if (timer) clearTimeout(timer);
    timer = null;
  }

  el.addEventListener('pointerdown', function (ev) {
    if (ev.pointerType !== 'touch') return;
    shown = false;
    startX = ev.clientX;
    startY = ev.clientY;
    cancel();
    timer = setTimeout(function () {
      timer = null;
      const hint = el.title || el.getAttribute('aria-label');
      if (!hint) return;
      shown = true;
      toast(hint);
    }, HOLD_MS);
  });
  el.addEventListener('pointermove', function (ev) {
    if (timer && Math.hypot(ev.clientX - startX, ev.clientY - startY) > MOVE_SLOP_PX) {
      cancel();
    }
  });
  el.addEventListener('pointerup', cancel);
  el.addEventListener('pointercancel', cancel);
  // Capture phase on the element itself runs before its own click handlers,
  // so the hold's closing click never reaches them.
  el.addEventListener('click', function (ev) {
    if (!shown) return;
    shown = false;
    ev.preventDefault();
    ev.stopImmediatePropagation();
  }, true);
  // iOS/Android would otherwise open the long-press context menu on top.
  el.addEventListener('contextmenu', function (ev) {
    if (shown || timer) ev.preventDefault();
  });
}
