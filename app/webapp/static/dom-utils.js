/* Small, dependency-free DOM helpers shared across modules. */

// A plain element with a class and text content — the one element builder
// this shape, was copied verbatim into board-answers.js,
// chat-resume.js, launch-toolbar.js, system-load.js and usage-meter.js.
export function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

// A settings-sheet row: a label on the left, a value (optionally toned via
// the caller setting `.lastChild.dataset.tone`) on the right. Shared by the
// usage sheet and the CPU/system-load sheet; the value keeps one line and the
// label wraps (board.css), so keep a value to a figure, not a sentence.
export function sheetRow(label, value, key) {
  const row = el('div', 'row usage-sheet-row');
  if (key) row.dataset.row = key;
  row.appendChild(el('span', null, label));
  row.appendChild(el('strong', 'usage-sheet-value', value));
  return row;
}

// Covers both positions a caller can land escaped text in: element content
// (`&`/`<`/`>`) and an attribute value (`"`/`'`). Most callers here build a
// text node's innerHTML, where the quote pair is a no-op — a `&quot;` renders
// as a plain `"`. But `markdown.js`'s renderer escapes once up front
// and then interpolates a captured link target into an `href="…"`, so an
// unescaped quote in that value would terminate the attribute early and let
// the rest of the token be parsed as further attributes on the same element.
// Ampersand stays first so the entities emitted below are not re-escaped.
export function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// Brand marks. These used to be <img src="/static/icons/NAME.svg">,
// which is a separate document: its glyph fill cannot reach page CSS vars,
// so every mark gets a theme-aware grey chip *behind* it to keep the
// baked mid-tone legible on both canvases. The marks are inline <symbol>s
// in index.html's brand sprite now (same in-document <use> mechanism as the
// Lucide sprite, same iOS Safari reason), so a monochrome mark simply
// inherits currentColor and the chip is gone. That also means the marks
// share the button's own colour, disabled and :active treatment for free,
// and the ?v= cache-busting iconUrl() existed for is moot —
// the sprite ships in the version-stamped page itself.
//
// `name` is the brand id without the `b-` prefix (an agent id, or `github`
// / `vscode`). Returns markup; brandIconEl() returns a node for the DOM-
// building call sites.
export function brandIcon(name, extraClass) {
  const cls = 'agent-icon' + (extraClass ? ' ' + extraClass : '');
  return '<svg class="' + cls + '" aria-hidden="true"><use href="#b-' + name + '"></use></svg>';
}

// `label` names the mark for assistive tech and as a hover tooltip. An SVG
// has no `alt`, so the accessible name is `aria-label` and the tooltip is a
// child <title> — the SVG equivalents of what the old <img> got from `alt`
// and `title`. Omit `label` for a mark that sits inside an already-labelled
// button (the Coding row's launch buttons), where a second name would just
// be read twice.
export function brandIconEl(name, extraClass, label) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  el.setAttribute('class', 'agent-icon' + (extraClass ? ' ' + extraClass : ''));
  if (label) {
    el.setAttribute('role', 'img');
    el.setAttribute('aria-label', label);
    const t = document.createElementNS('http://www.w3.org/2000/svg', 'title');
    t.textContent = label;
    el.appendChild(t);
  } else {
    el.setAttribute('aria-hidden', 'true');
  }
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  // setAttribute (not the xlink: form): every browser the PWA targets
  // resolves the plain `href` on <use>, and the sprite is same-document.
  use.setAttribute('href', '#b-' + name);
  el.appendChild(use);
  return el;
}

// Bind a document-level pointerdown listener that closes a popover on any
// tap outside `box` (and outside `toggle`, when given — so re-tapping the
// button that opened the popover doesn't immediately re-close it; the
// opening tap's pointerdown has already fired by the time the popover is
// shown and this binds, so the `contains()` guards exist only to cover a
// tap on the toggle button itself). Returns a disposer that removes the
// listener — call it once, on close. Callers are still responsible for
// guarding against double-binding (skip calling this again while already
// open) and for calling the returned disposer before re-binding.
export function bindOutsideClickToClose(box, toggle, closer) {
  const handler = function (ev) {
    if (box.contains(ev.target) || (toggle && toggle.contains(ev.target))) return;
    closer();
  };
  document.addEventListener('pointerdown', handler);
  return function dispose() {
    document.removeEventListener('pointerdown', handler);
  };
}

// Bring `el` into view for an empty state's action: open every
// disclosure it sits in (itself included), scroll it to the middle of the
// screen, and focus it when it takes focus.
export function revealInCard(el) {
  if (!el) return;
  for (let d = el.closest('details'); d; d = d.parentElement && d.parentElement.closest('details')) {
    d.open = true;
  }
  el.scrollIntoView({ block: 'center' });
  if (typeof el.focus === 'function') el.focus({ preventScroll: true });
}
