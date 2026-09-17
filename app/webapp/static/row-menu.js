/* Floating per-row action menu — one anchor button per list row opens a
 * vertical list of icon + label actions.
 *
 * Extracted from the session gear menu (#953/#967) when the Coding tile
 * grew the same shape (#977: the ⋯ project menu). Both lists re-render on a
 * poll, which would tear an open menu down: the open row's key is remembered
 * here and `attach()` reopens the menu on the rebuilt row; `endRender()`
 * drops the state when the row is gone. Closes on a tap outside, on Escape,
 * and on a second tap of the anchor.
 *
 * Placement is the caller's CSS: the menu is `position: absolute` inside the
 * row's `.row-actions` rail (`.session-menu` floats left of the rail,
 * `.project-menu` drops below it — see styles.css).
 */

import { escapeHtml } from './api.js';
import { bindOutsideClickToClose } from './dom-utils.js';
import { icon } from './_vendored/icons/icons.js';

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

// `menuClass` names the placement variant (`session-menu` / `project-menu`)
// and is the class the caller's CSS and tests key on.
export function createRowMenu(menuClass) {
  let openKey = null;
  let openAnchor = null;
  let openMenu = null;
  let disposeOutside = null;
  let reopened = false;
  // menu element → its render function (re-evaluates hidden/text/label).
  const renderers = new WeakMap();

  function onKey(ev) {
    if (ev.key === 'Escape') close();
  }

  function close() {
    openKey = null;
    if (disposeOutside) { disposeOutside(); disposeOutside = null; }
    document.removeEventListener('keydown', onKey);
    if (openMenu) openMenu.hidden = true;
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
