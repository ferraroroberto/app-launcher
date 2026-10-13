/* One list row on the vendored action-row (#1128, _vendored/action-row/).
 *
 * The row's text column is its primary action: one `.action-row-main`
 * button the full height of the row. Beside it sit at most a leading
 * favorite star and one trailing kebab, both siblings of the main button,
 * so a tap on one never also fires the row. Everything else goes in the
 * kebab's row menu (row-menu.js). Projects, Life OS skills, and the Apps
 * and Trays lists are all built here.
 */

import { icon } from './_vendored/icons/icons.js';
import { attachSwipe } from './swipe-row.js';

// opts:
//   id         -> li[data-id]
//   className  -> extra li classes (the list's hook: coding-item, …)
//   title      -> the one-line title; `meta` -> the optional context line
//   chips      -> optional exception chips, after the meta text
//   label      -> the main button's accessible name (what a tap does)
//   onMain     -> the primary action; `disabled` + `hint` grey it out
//   favorite   -> { on, onToggle } adds the leading star
//   avatar     -> an avatar element (glance.js avatar(), #1437) leading the
//                 text column inside the main button, so the tap target
//                 stays the whole row
//   kebabClass / kebabLabel -> the trailing kebab's hook class and name;
//                 no kebabLabel, no kebab
//   density    -> 'one-line': the meta runs after the title on one line, at
//                 --row-md (the Board's question rows, #1501); 'small': the
//                 same at --row-sm (the plan lane)
//   trailing   -> an element after the main button and before any kebab: the
//                 row's one other trailing item (a chevron, an Answer button)
//   inert      -> the text column is a plain <div>, not a button: a row that
//                 reports a fact and does nothing on tap (onMain unused)
//   swipe      -> { leading, trailing }, each { label, glyph, tone, onCommit,
//                 disabled, reason }: swipe actions on a touch screen
//                 (swipe-row.js, #1503). The row's content moves into one
//                 sliding surface; the actions must also be in the kebab,
//                 the route for a mouse and for assistive tech
// Each of the last four is optional: a row built without them is
// byte-identical to one built before they existed (the golden in
// tests/e2e/test_life_tab_glance.py).
// Returns { li, main, title, meta, kebab } for the caller to finish.
export function actionRow(opts) {
  const li = document.createElement('li');
  li.className = 'action-row' + (opts.className ? ' ' + opts.className : '');
  if (opts.density) li.classList.add('action-row--' + opts.density);
  if (opts.id != null) li.dataset.id = opts.id;

  if (opts.favorite) {
    const fav = document.createElement('button');
    fav.type = 'button';
    // `star-btn` stays as the hook the e2e suite has always used.
    fav.className = 'icon-button action-row-fav star-btn';
    fav.setAttribute('aria-pressed', opts.favorite.on ? 'true' : 'false');
    fav.title = opts.favorite.on
      ? 'Unstar (remove from favorites)'
      : 'Star (add to favorites)';
    fav.setAttribute('aria-label', fav.title);
    fav.innerHTML = icon('star', 'action-row-fav-off') + icon('star-fill', 'action-row-fav-on');
    fav.addEventListener('click', opts.favorite.onToggle);
    li.appendChild(fav);
  }

  const main = document.createElement(opts.inert ? 'div' : 'button');
  if (!opts.inert) main.type = 'button';
  main.className = 'action-row-main';
  if (opts.inert) main.classList.add('is-inert');
  // With an avatar the main button lays out as a row: the avatar, then the
  // title and meta stacked in their own column.
  let column = main;
  if (opts.avatar) {
    main.classList.add('has-avatar');
    main.appendChild(opts.avatar);
    column = document.createElement('span');
    column.className = 'action-row-text';
    main.appendChild(column);
  }
  const title = document.createElement('span');
  title.className = 'action-row-title';
  title.textContent = opts.title;
  title.title = opts.title;
  column.appendChild(title);
  // The context line: the plain meta text, then any exception chips
  // (glance.js chip(), #1434) after it, so a chip never ellipsizes away.
  let meta = null;
  const chips = opts.chips || [];
  if (opts.meta || chips.length) {
    meta = document.createElement('span');
    meta.className = 'action-row-meta';
    if (chips.length) {
      meta.classList.add('has-chips');
      const text = document.createElement('span');
      text.className = 'action-row-meta-text';
      text.textContent = opts.meta || '';
      meta.appendChild(text);
      chips.forEach(function (c) { meta.appendChild(c); });
    } else {
      meta.textContent = opts.meta;
    }
    column.appendChild(meta);
  }
  if (opts.inert) {
    // Nothing to name: the row's own text is what a reader hears.
  } else if (opts.disabled) {
    main.disabled = true;
    main.setAttribute('aria-label', opts.hint || opts.label);
  } else {
    main.setAttribute('aria-label', opts.label);
    main.addEventListener('click', opts.onMain);
  }
  li.appendChild(main);
  if (opts.trailing) {
    opts.trailing.classList.add('action-row-trailing');
    li.appendChild(opts.trailing);
  }

  // A row with nothing behind a menu (the Jobs tab's Next up fires) passes
  // no kebabLabel and gets no kebab.
  let kebab = null;
  if (opts.kebabLabel) {
    kebab = document.createElement('button');
    kebab.type = 'button';
    kebab.className = 'icon-button action-row-kebab' + (opts.kebabClass ? ' ' + opts.kebabClass : '');
    kebab.innerHTML = icon('ellipsis-vertical');
    kebab.title = opts.kebabLabel;
    kebab.setAttribute('aria-label', opts.kebabLabel);
    li.appendChild(kebab);
  }

  if (opts.swipe) {
    // Everything the row shows slides together on one surface, the panes
    // behind it, both inside a track that clips them; the kebab's menu,
    // appended to the row later, stays outside the clip.
    const track = document.createElement('div');
    track.className = 'swipe-track';
    const surface = document.createElement('div');
    while (li.firstChild) surface.appendChild(li.firstChild);
    track.appendChild(surface);
    li.appendChild(track);
    attachSwipe(li, surface, opts.swipe, icon);
  }

  return { li: li, main: main, title: title, meta: meta, kebab: kebab };
}
