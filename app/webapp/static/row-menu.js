/* Floating per-row action menu — one anchor button per list row opens a
 * vertical list of icon + label actions.
 *
 * Extracted from the session row menu (#953/#967) when the Coding tile grew
 * the same shape (#977: the ⋯ project menu). Its users are that original
 * session menu (whose anchor became a kebab in #1025), the Coding tile's ⋯
 * menu and the session overlay's bar menu (#981). A list that re-renders on
 * a poll would tear an open menu down: the open row's key is remembered here
 * and `attach()` reopens the menu on the rebuilt row; `endRender()` drops
 * the state when the row is gone. Closes on a tap outside, on Escape, and on
 * a second tap of the anchor.
 *
 * Placement is the caller's CSS: the menu is `position: absolute` inside its
 * anchor's positioned box (`.session-menu` floats left of the row's
 * `.row-actions` rail, `.project-menu` drops below it, `.terminal-menu`
 * below the overlay bar — see styles.css).
 *
 * `createRowMenu(cls, { placeAgainst })` opts a menu out of that clipping
 * (#996). An absolutely-positioned menu is clipped by any scrolling
 * ancestor, and the shared composer is mounted inside the Board drawer,
 * which lives in the column carousel — whose `overflow-x: auto` forces
 * `overflow-y: auto`, so the image menu rising above the *top* card's
 * drawer lost its top edge to that box. `placeAgainst()` returns the
 * element the menu floats against (the composer); while open the menu
 * switches to `position: fixed`, whose containing block is the viewport, so
 * no ancestor's overflow can clip it — the same escape the model combo's
 * portal uses (dom-utils.js), minus the move to <body>, so the menu stays
 * inside its mount and mount-scoped selectors keep working. The CSS stays
 * the one source of the geometry: the offset from that element is measured
 * once per open, from where the CSS put the menu.
 *
 * Every open menu, absolute or fixed, is then kept inside the band it may
 * use (#1507): the viewport, minus the floating nav pill while it shows, and
 * minus any ancestor that would clip an absolute menu. When the CSS side
 * doesn't fit, the menu flips to the mirror side of its positioned box
 * (below its anchor ↔ above it), the standard popover rule; only when
 * neither side fits is it clamped, its height capped with an internal
 * scroll. It is re-fitted on every scroll and resize while open.
 */

import { escapeHtml } from './api.js';
import { bindOutsideClickToClose } from './dom-utils.js';
import { icon } from './_vendored/icons/icons.js';

// Keep a viewport-placed menu this far inside the screen edges.
const _EDGE = 8;

// The vertical band [lo, hi] (viewport px) an open menu may occupy. The
// floating bottom-tab pill (`nav.tabs`, fixed on the phone) caps it from
// below while it is visible; every overlay that covers the pill hides it
// with `visibility`, so a menu inside one keeps the whole viewport. A
// `clipped` (absolute) menu is also bounded by each ancestor whose overflow
// would cut it off.
function usableBand(menu, clipped) {
  let lo = _EDGE;
  let hi = document.documentElement.clientHeight - _EDGE;
  const nav = document.querySelector('nav.tabs');
  if (nav) {
    const cs = getComputedStyle(nav);
    const r = nav.getBoundingClientRect();
    if (cs.position === 'fixed' && cs.visibility === 'visible' && r.height > 0 && r.top > 0) {
      hi = Math.min(hi, r.top - _EDGE);
    }
  }
  if (clipped) {
    for (let el = menu.parentElement; el && el !== document.body; el = el.parentElement) {
      if (getComputedStyle(el).overflowY === 'visible') continue;
      const r = el.getBoundingClientRect();
      lo = Math.max(lo, r.top);
      hi = Math.min(hi, r.bottom);
    }
  }
  return { lo: lo, hi: hi };
}

// Where a menu of `height` whose CSS put its top at `top` should go: there
// when it fits the band, else mirrored across `ref` (the box its CSS offset
// is measured from), else clamped with its height capped to the band.
function fitVertical(top, height, ref, band) {
  const fits = function (t) { return t >= band.lo && t + height <= band.hi; };
  if (fits(top)) return { top: top, maxHeight: null };
  const flipped = ref.top + ref.bottom - top - height;
  if (fits(flipped)) return { top: flipped, maxHeight: null };
  const room = Math.max(0, band.hi - band.lo);
  if (height > room) return { top: band.lo, maxHeight: room };
  return { top: Math.min(Math.max(band.lo, top), band.hi - height), maxHeight: null };
}

function capHeight(menu, maxHeight) {
  menu.style.maxHeight = maxHeight == null ? '' : Math.floor(maxHeight) + 'px';
  menu.style.overflowY = maxHeight == null ? '' : 'auto';
}

// An item property may be a plain value or a function of no arguments,
// re-evaluated every time the menu opens (#982: the terminal bar's menu
// shows chat-only rows, and their glyph/label flip with the pane's state).
function resolve(v) {
  return typeof v === 'function' ? v() : v;
}

// Paint (or repaint) one row's glyph, caption and accessible name.
function paintButton(btn, item) {
  btn.innerHTML = (resolve(item.html) || icon(resolve(item.glyph))) +
    '<span class="row-menu-label">' + escapeHtml(resolve(item.text)) + '</span>';
  const disabled = !!resolve(item.disabled);
  btn.disabled = disabled;
  btn.title = disabled && item.title ? resolve(item.title) : resolve(item.label);
  btn.setAttribute('aria-label', btn.title);
}

// One menu row. `label` is the accessible name (aria-label/title, the stable
// hook the e2e suite targets); `text` is the short caption next to the
// glyph. `glyph` is a Lucide sprite name; `html` overrides it with ready
// markup (a brand mark). A `disabled` item stays visible with `title` as
// its hover hint, like a greyed-out rail button; both may be functions,
// re-evaluated on every open like `hidden` (#1119: the Life OS viewer's
// menu serves whichever conversation is open). `dataset` sets data-*
// attributes — the Coding menu's launch rows carry `data-agent` (#1070),
// the same hook the row button they replaced had, so a caller (or a test)
// can name one agent's row without matching on its label text. `danger`
// marks a destructive item (#1129: Stop, Kill, Remove, Delete): it renders
// last, after a divider, in the danger text colour — the one place a row's
// destructive action lives (the action-row contract, fleet-config#965). Any
// confirm stays the caller's, in onTap.
function menuButton(item, close) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'icon-btn row-menu-btn ' + (item.danger ? 'row-menu-danger ' : '') +
    (item.className || '');
  if (item.dataset) {
    Object.keys(item.dataset).forEach(function (k) {
      btn.dataset[k] = item.dataset[k];
    });
  }
  paintButton(btn, item);
  btn.setAttribute('role', 'menuitem');
  btn.addEventListener('click', function () {
    if (btn.disabled) return;
    close();
    item.onTap();
  });
  return btn;
}

// `menuClass` names the placement variant (`session-menu` / `project-menu` /
// `terminal-menu`)
// and is the class the caller's CSS and tests key on.
export function createRowMenu(menuClass, opts) {
  // () → the element an unclipped menu floats against, or null for the
  // default absolute placement (see the header).
  const placeAgainst = (opts && opts.placeAgainst) || null;
  let openKey = null;
  let openAnchor = null;
  let openMenu = null;
  let disposeOutside = null;
  let reopened = false;
  // While a menu is viewport-placed: the element it floats against and the
  // offset from that element's top-left the CSS asked for.
  let floatBox = null;
  let floatOffset = null;
  let placeFrame = null;
  // menu element → its render function (re-evaluates hidden/text/label).
  const renderers = new WeakMap();

  function onKey(ev) {
    if (ev.key !== 'Escape') return;
    // One Escape closes one layer: a menu open inside a <dialog> (the Other
    // ports sheet, #1437) closes without also closing the dialog.
    ev.preventDefault();
    close();
  }

  // Fit the open menu into its band (see the header). A fixed menu no longer
  // travels with its anchor, so this re-applies its CSS-derived offset in
  // viewport coordinates; an absolute one travels with its row but can
  // scroll into the nav, so it is re-measured from its CSS placement. Runs
  // again whenever anything moves the box underneath the menu.
  function place() {
    placeFrame = null;
    const menu = openMenu;
    if (!menu || menu.hidden) return;
    // Re-measuring drops the cap for a moment; keep a capped menu's own
    // scroll position across it.
    const scrolled = menu.scrollTop;
    capHeight(menu, null);
    if (floatBox) {
      const box = floatBox.getBoundingClientRect();
      const menuRect = menu.getBoundingClientRect();
      const vw = document.documentElement.clientWidth;
      const fit = fitVertical(box.top + floatOffset.dy, menuRect.height, box,
        usableBand(menu, false));
      const left = Math.min(
        Math.max(_EDGE, box.left + floatOffset.dx),
        Math.max(_EDGE, vw - _EDGE - menuRect.width)
      );
      menu.style.top = Math.round(fit.top) + 'px';
      menu.style.left = Math.round(left) + 'px';
      capHeight(menu, fit.maxHeight);
      menu.scrollTop = scrolled;
      return;
    }
    const parent = menu.offsetParent;
    if (!parent) return;
    menu.style.top = '';
    menu.style.bottom = '';
    const ref = parent.getBoundingClientRect();
    const menuRect = menu.getBoundingClientRect();
    const fit = fitVertical(menuRect.top, menuRect.height, ref, usableBand(menu, true));
    if (fit.maxHeight == null && Math.abs(fit.top - menuRect.top) < 1) return;
    // Inline top in the positioned box's padding-box coordinates; `bottom`
    // neutralised as in floatMenu() so the box keeps its own height.
    menu.style.top = Math.round(fit.top - ref.top - parent.clientTop) + 'px';
    menu.style.bottom = 'auto';
    capHeight(menu, fit.maxHeight);
    menu.scrollTop = scrolled;
  }

  function schedulePlace(ev) {
    // A capped menu scrolling its own items moves nothing underneath it.
    if (ev && ev.type === 'scroll' && openMenu && openMenu.contains(ev.target)) return;
    if (placeFrame != null) return;
    placeFrame = requestAnimationFrame(place);
  }

  function bindPlacement(bind) {
    const on = bind ? 'addEventListener' : 'removeEventListener';
    window[on]('resize', schedulePlace);
    // Capture: the page, the column carousel and the transcript list are all
    // scrollers that move the box without bubbling a scroll event.
    document[on]('scroll', schedulePlace, true);
    if (window.visualViewport) {
      window.visualViewport[on]('resize', schedulePlace);
      window.visualViewport[on]('scroll', schedulePlace);
    }
  }

  // Lift the just-shown menu out of every ancestor's overflow, keeping it
  // exactly where its own CSS put it.
  function floatMenu(menu) {
    const box = placeAgainst && placeAgainst();
    if (!box) return;
    const boxRect = box.getBoundingClientRect();
    const menuRect = menu.getBoundingClientRect();
    floatBox = box;
    floatOffset = { dx: menuRect.left - boxRect.left, dy: menuRect.top - boxRect.top };
    menu.classList.add('row-menu--fixed');
    // Inline, not in that class: a caller's placement CSS sets `bottom` /
    // `right` (`.composer-menu`), and leaving either alongside the `top` /
    // `left` written below over-constrains the box — an auto-sized element
    // given both edges stretches to span them instead of keeping its own
    // size. Inline wins over the caller's rule wherever that rule sits.
    menu.style.bottom = 'auto';
    menu.style.right = 'auto';
  }

  function unplaceMenu(menu) {
    floatBox = null;
    floatOffset = null;
    if (placeFrame != null) { cancelAnimationFrame(placeFrame); placeFrame = null; }
    bindPlacement(false);
    menu.classList.remove('row-menu--fixed');
    menu.style.top = '';
    menu.style.left = '';
    menu.style.bottom = '';
    menu.style.right = '';
    capHeight(menu, null);
  }

  function close() {
    openKey = null;
    if (disposeOutside) { disposeOutside(); disposeOutside = null; }
    document.removeEventListener('keydown', onKey);
    if (openMenu) {
      openMenu.hidden = true;
      unplaceMenu(openMenu);
    }
    if (openAnchor) openAnchor.setAttribute('aria-expanded', 'false');
    openMenu = null;
    openAnchor = null;
  }

  function show(key, anchor, menu) {
    if (openMenu && openMenu !== menu) close();
    openKey = key;
    openAnchor = anchor;
    openMenu = menu;
    const render = renderers.get(menu);
    if (render) render();
    menu.hidden = false;
    // Synchronous, before the browser paints the unhidden menu, so neither
    // the CSS placement it is measured from nor a side it flips away from
    // is ever seen.
    floatMenu(menu);
    place();
    bindPlacement(true);
    anchor.setAttribute('aria-expanded', 'true');
    if (disposeOutside) disposeOutside();
    disposeOutside = bindOutsideClickToClose(menu, anchor, close);
    document.addEventListener('keydown', onKey);
  }

  return {
    // Wire `anchor` to toggle a menu of `items` for the row `key`; returns
    // the menu element for the caller to place in the rail. Reopens at once
    // when this row's menu was open before the list re-rendered.
    // `opts.header` (#1437), a value or a function like an item's, is one
    // muted, non-interactive line above the items: a fact worth a glance
    // but not the row's width (a pid, a bat path).
    attach: function (key, anchor, items, opts) {
      const menu = document.createElement('div');
      menu.className = 'row-menu ' + menuClass;
      menu.setAttribute('role', 'menu');
      menu.hidden = true;
      const header = opts && opts.header;
      const head = document.createElement('div');
      head.className = 'row-menu-head';
      head.setAttribute('role', 'presentation');
      // Every row is built once; a hidden one is *detached* from the menu
      // (not [hidden]) so a count of the menu's buttons — the e2e suite's
      // and any caller's — only ever sees what is offered. Re-appending the
      // visible rows in declaration order keeps the menu's order stable
      // when a row comes back.
      const rows = items.map(function (item) {
        return { item: item, btn: menuButton(item, close) };
      });
      // Destructive rows go last whatever the caller's order, behind one
      // divider that only shows when something safe sits above them.
      const divider = document.createElement('div');
      divider.className = 'row-menu-divider';
      divider.setAttribute('role', 'separator');
      const render = function () {
        const headText = resolve(header) || '';
        if (headText) {
          head.textContent = headText;
          head.title = headText;
          menu.insertBefore(head, menu.firstChild);
        } else if (head.parentNode) {
          head.parentNode.removeChild(head);
        }
        const shown = [];
        rows.forEach(function (row) {
          if (resolve(row.item.hidden)) {
            if (row.btn.parentNode) row.btn.parentNode.removeChild(row.btn);
            return;
          }
          paintButton(row.btn, row.item);
          shown.push(row);
        });
        const safe = shown.filter(function (row) { return !row.item.danger; });
        const danger = shown.filter(function (row) { return row.item.danger; });
        safe.forEach(function (row) { menu.appendChild(row.btn); });
        if (safe.length && danger.length) menu.appendChild(divider);
        else if (divider.parentNode) divider.parentNode.removeChild(divider);
        danger.forEach(function (row) { menu.appendChild(row.btn); });
      };
      render();
      renderers.set(menu, render);
      anchor.classList.add('row-menu-anchor');
      anchor.setAttribute('aria-haspopup', 'menu');
      anchor.setAttribute('aria-expanded', 'false');
      anchor.addEventListener('click', function () {
        if (openKey === key) close();
        else show(key, anchor, menu);
      });
      if (openKey === key) {
        show(key, anchor, menu);
        reopened = true;
      }
      return menu;
    },
    // Call once after the list is rebuilt: an open menu whose row wasn't
    // re-attached (the row is gone) drops its stale state.
    endRender: function () {
      if (openKey && !reopened) close();
      reopened = false;
    },
    close: close,
    isOpen: function (key) { return openKey === key; },
  };
}
