/* Floating per-row action menu — one anchor button per list row opens a
 * vertical list of icon + label actions.
 *
 * Extracted from the session gear menu (#953/#967) when the Coding tile
 * grew the same shape (#977: the ⋯ project menu); that original caller is
 * gone since #1025, and the users now are the Coding tile's ⋯ menu and the
 * session overlay's bar menu (#981). A list that re-renders on a poll would
 * tear an open menu down: the open row's key is remembered here and
 * `attach()` reopens the menu on the rebuilt row; `endRender()` drops the
 * state when the row is gone. Closes on a tap outside, on Escape, and on a
 * second tap of the anchor.
 *
 * Placement is the caller's CSS: the menu is `position: absolute` inside its
 * anchor's positioned box (`.project-menu` drops below the row's
 * `.row-actions` rail, `.terminal-menu` below the overlay bar — see
 * styles.css).
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
 */

import { escapeHtml } from './api.js';
import { bindOutsideClickToClose } from './dom-utils.js';
import { icon } from './_vendored/icons/icons.js';

// Keep a viewport-placed menu this far inside the screen edges.
const _EDGE = 8;

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
  btn.title = item.disabled && item.title ? item.title : resolve(item.label);
  btn.setAttribute('aria-label', btn.title);
}

// One menu row. `label` is the accessible name (aria-label/title, the stable
// hook the e2e suite targets); `text` is the short caption next to the
// glyph. `glyph` is a Lucide sprite name; `html` overrides it with ready
// markup (a brand <img>). A `disabled` item stays visible with `title` as
// its hover hint, like a greyed-out rail button.
function menuButton(item, close) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'icon-btn row-menu-btn ' + (item.className || '');
  paintButton(btn, item);
  btn.setAttribute('role', 'menuitem');
  if (item.disabled) {
    btn.disabled = true;
  } else {
    btn.addEventListener('click', function () {
      close();
      item.onTap();
    });
  }
  return btn;
}

// `menuClass` names the placement variant (`project-menu` / `terminal-menu`)
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
  let floatFrame = null;
  // menu element → its render function (re-evaluates hidden/text/label).
  const renderers = new WeakMap();

  function onKey(ev) {
    if (ev.key === 'Escape') close();
  }

  // Re-apply the CSS-derived offset in viewport coordinates. A fixed menu no
  // longer travels with its anchor, so this runs again whenever anything
  // moves the box underneath it.
  function place() {
    floatFrame = null;
    if (!floatBox || !openMenu || openMenu.hidden) return;
    const box = floatBox.getBoundingClientRect();
    const menuRect = openMenu.getBoundingClientRect();
    const vw = document.documentElement.clientWidth;
    const vh = document.documentElement.clientHeight;
    // Clamp rather than flip: the offset already encodes the side the CSS
    // chose, and a two-row menu only ever needs nudging back on screen.
    const top = Math.min(
      Math.max(_EDGE, box.top + floatOffset.dy),
      Math.max(_EDGE, vh - _EDGE - menuRect.height)
    );
    const left = Math.min(
      Math.max(_EDGE, box.left + floatOffset.dx),
      Math.max(_EDGE, vw - _EDGE - menuRect.width)
    );
    openMenu.style.top = Math.round(top) + 'px';
    openMenu.style.left = Math.round(left) + 'px';
  }

  function schedulePlace() {
    if (floatFrame != null) return;
    floatFrame = requestAnimationFrame(place);
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
    place();
    bindPlacement(true);
  }

  function unfloatMenu(menu) {
    if (!floatBox) return;
    floatBox = null;
    floatOffset = null;
    if (floatFrame != null) { cancelAnimationFrame(floatFrame); floatFrame = null; }
    bindPlacement(false);
    menu.classList.remove('row-menu--fixed');
    menu.style.top = '';
    menu.style.left = '';
    menu.style.bottom = '';
    menu.style.right = '';
  }

  function close() {
    openKey = null;
    if (disposeOutside) { disposeOutside(); disposeOutside = null; }
    document.removeEventListener('keydown', onKey);
    if (openMenu) {
      openMenu.hidden = true;
      unfloatMenu(openMenu);
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
    // Synchronous, before the browser paints the unhidden menu, so the
    // absolute placement it is measured from is never seen.
    floatMenu(menu);
    anchor.setAttribute('aria-expanded', 'true');
    if (disposeOutside) disposeOutside();
    disposeOutside = bindOutsideClickToClose(menu, anchor, close);
    document.addEventListener('keydown', onKey);
  }

  return {
    // Wire `anchor` to toggle a menu of `items` for the row `key`; returns
    // the menu element for the caller to place in the rail. Reopens at once
    // when this row's menu was open before the list re-rendered.
    attach: function (key, anchor, items) {
      const menu = document.createElement('div');
      menu.className = 'row-menu ' + menuClass;
      menu.setAttribute('role', 'menu');
      menu.hidden = true;
      // Every row is built once; a hidden one is *detached* from the menu
      // (not [hidden]) so a count of the menu's buttons — the e2e suite's
      // and any caller's — only ever sees what is offered. Re-appending the
      // visible rows in declaration order keeps the menu's order stable
      // when a row comes back.
      const rows = items.map(function (item) {
        return { item: item, btn: menuButton(item, close) };
      });
      const render = function () {
        rows.forEach(function (row) {
          if (resolve(row.item.hidden)) {
            if (row.btn.parentNode) row.btn.parentNode.removeChild(row.btn);
            return;
          }
          paintButton(row.btn, row.item);
          menu.appendChild(row.btn);
        });
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
