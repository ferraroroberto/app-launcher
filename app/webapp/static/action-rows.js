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

// opts:
//   id         -> li[data-id]
//   className  -> extra li classes (the list's hook: coding-item, …)
//   title      -> the one-line title; `meta` -> the optional context line
//   label      -> the main button's accessible name (what a tap does)
//   onMain     -> the primary action; `disabled` + `hint` grey it out
//   favorite   -> { on, onToggle } adds the leading star
//   kebabClass / kebabLabel -> the trailing kebab's hook class and name
// Returns { li, main, title, meta, kebab } for the caller to finish.
export function actionRow(opts) {
  const li = document.createElement('li');
  li.className = 'action-row' + (opts.className ? ' ' + opts.className : '');
  if (opts.id != null) li.dataset.id = opts.id;

  if (opts.favorite) {
    const fav = document.createElement('button');
    fav.type = 'button';
    // `star-btn` stays as the hook the e2e suite has always used.
    fav.className = 'action-row-fav star-btn';
    fav.setAttribute('aria-pressed', opts.favorite.on ? 'true' : 'false');
    fav.title = opts.favorite.on
      ? 'Unstar (remove from favorites)'
      : 'Star (add to favorites)';
    fav.setAttribute('aria-label', fav.title);
    fav.innerHTML = icon('star', 'action-row-fav-off') + icon('star-fill', 'action-row-fav-on');
    fav.addEventListener('click', opts.favorite.onToggle);
    li.appendChild(fav);
  }

  const main = document.createElement('button');
  main.type = 'button';
  main.className = 'action-row-main';
  const title = document.createElement('span');
  title.className = 'action-row-title';
  title.textContent = opts.title;
  title.title = opts.title;
  main.appendChild(title);
  let meta = null;
  if (opts.meta) {
    meta = document.createElement('span');
    meta.className = 'action-row-meta';
    meta.textContent = opts.meta;
    main.appendChild(meta);
  }
  if (opts.disabled) {
    main.disabled = true;
    main.setAttribute('aria-label', opts.hint || opts.label);
  } else {
    main.setAttribute('aria-label', opts.label);
    main.addEventListener('click', opts.onMain);
  }
  li.appendChild(main);

  const kebab = document.createElement('button');
  kebab.type = 'button';
  kebab.className = 'action-row-kebab' + (opts.kebabClass ? ' ' + opts.kebabClass : '');
  kebab.innerHTML = icon('ellipsis-vertical');
  kebab.title = opts.kebabLabel;
  kebab.setAttribute('aria-label', opts.kebabLabel);
  li.appendChild(kebab);

  return { li: li, main: main, title: title, meta: meta, kebab: kebab };
}
