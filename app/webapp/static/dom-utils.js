/* Small, dependency-free DOM helpers shared across modules. */

// Cache-busted URL for a brand-icon SVG (issue #372). The serve-time JS
// import rewrite stamps ?v=<fleet hash> onto every module URL, so our own
// import.meta.url already carries the hash — reuse it on the icon src so
// an icon edit changes the cache key (iOS Safari held the pre-#361 icons
// past the deploy). Falls back to the bare URL when served unstamped.
const _ASSET_V = new URL(import.meta.url).searchParams.get('v');
export function iconUrl(name) {
  return '/static/icons/' + name + '.svg' + (_ASSET_V ? '?v=' + _ASSET_V : '');
}

// Flip an aria-checked toggle switch (button or input) and return the new
// boolean state. Every toggle-switch site across the SPA (options cards,
// jobs dialog, life-os, board dispatch, apps) reads/writes this same
// attribute pair — call this instead of re-deriving `next` by hand.
export function toggleAriaChecked(el) {
  const next = el.getAttribute('aria-checked') !== 'true';
  el.setAttribute('aria-checked', String(next));
  return next;
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

// A compact model-picker dropdown that is valid inside a <summary> (#540).
// A native <select> cannot live there — WebKit's HTML parser derails on it,
// closing the enclosing <details> early and cascading into a broken tree
// that even zeroes custom-property inheritance on ancestors (the flat 0-token
// layout bug). So the control is built from phrasing-only content — a
// <button class="model-combo-trigger"> plus a <span class="model-combo-menu"
// role="listbox"> of <button role="option" data-value>. Looks like the
// flattened board select.
//
// `root` is the .model-combo wrapper (its data-value holds the current
// value). `onChange(value)` fires only on a user pick, never on programmatic
// setValue — so a config round-trip that calls setValue can't loop. Returns
// { setValue(v), getValue() }, or null when the markup is absent.
export function wireModelCombo(root, onChange) {
  if (!root) return null;
  const trigger = root.querySelector('.model-combo-trigger');
  const menu = root.querySelector('.model-combo-menu');
  if (!trigger || !menu) return null;
  let dispose = null;

  function close() {
    if (menu.hidden) return;
    menu.hidden = true;
    trigger.setAttribute('aria-expanded', 'false');
    if (dispose) { dispose(); dispose = null; }
  }
  function open() {
    if (!menu.hidden) return;
    menu.hidden = false;
    trigger.setAttribute('aria-expanded', 'true');
    dispose = bindOutsideClickToClose(menu, trigger, close);
  }
  function apply(value, fire) {
    const opt = menu.querySelector('[data-value="' + value + '"]');
    if (!opt) return;
    root.dataset.value = value;
    trigger.textContent = opt.textContent;
    menu.querySelectorAll('[role="option"]').forEach(function (o) {
      o.setAttribute('aria-selected', o === opt ? 'true' : 'false');
    });
    if (fire && onChange) onChange(value);
  }

  // A tap anywhere in a <summary> also toggles its <details>, so every
  // interactive handler here stops propagation (same guard the sibling
  // Detached/Resume toggles use).
  trigger.addEventListener('click', function (ev) {
    ev.stopPropagation();
    if (menu.hidden) open(); else close();
  });
  menu.addEventListener('click', function (ev) {
    ev.stopPropagation();
    const opt = ev.target.closest('[data-value]');
    if (!opt) return;
    apply(opt.dataset.value, true);
    close();
  });
  trigger.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape') close();
  });

  return {
    setValue: function (v) { apply(v, false); },
    getValue: function () { return root.dataset.value || ''; },
  };
}

// Claude 5h/7d usage badges (issue #326) — shared between the Board tab and
// the Coding tab's Running-sessions header, both of which poll their own
// endpoint (GET /api/board, GET /api/rate-limits) but render the identical
// {available, stale, five_hour, seven_day} shape the same way.

// Color tier for a usage percentage — same 60/80 thresholds as fleet-config's
// statusline-command.ps1, so every surface agrees on what counts as "close".
function usageTier(pct) {
  if (pct == null || isNaN(pct)) return 'muted';
  if (pct >= 80) return 'danger';
  if (pct >= 60) return 'warn';
  return 'good';
}

// Time-until a rate-limit window's "resets_at" (Unix epoch seconds).
// Recomputed on every render call, so it stays live between polls with no
// timer of its own.
function fmtResetCountdown(epochSeconds) {
  if (epochSeconds == null || isNaN(epochSeconds)) return '';
  const secs = epochSeconds - Math.floor(Date.now() / 1000);
  if (secs <= 0) return 'now';
  const mins = Math.floor(secs / 60);
  if (mins < 60) return mins + 'm';
  const hours = Math.floor(mins / 60);
  if (hours < 24) return hours + 'h ' + (mins % 60) + 'm';
  const days = Math.floor(hours / 24);
  return days + 'd ' + (hours % 24) + 'h';
}

function renderUsageBadge(el, windowData, suffix, stale) {
  if (!el) return;
  if (!windowData || windowData.used_percentage == null) {
    el.hidden = true;
    return;
  }
  const pct = Math.round(windowData.used_percentage);
  el.className = 'usage-badge ' + usageTier(pct) + (stale ? ' stale' : '');
  const resetTxt = windowData.resets_at != null
    ? ' · resets ' + fmtResetCountdown(windowData.resets_at)
    : '';
  el.textContent = pct + '%' + suffix + resetTxt;
  el.hidden = false;
}

// Update a usage-badges row from a {available, stale, five_hour, seven_day}
// payload — either GET /api/board's `rate_limits` sub-object or GET
// /api/rate-limits's response body directly. Hides the whole container when
// unavailable (no cache yet, or it went missing/corrupt) — never an error.
export function renderUsageBadgeRow(container, sessionEl, weeklyEl, rateLimits) {
  if (!container) return;
  if (!rateLimits || !rateLimits.available) {
    container.hidden = true;
    return;
  }
  container.hidden = false;
  renderUsageBadge(sessionEl, rateLimits.five_hour, 's', rateLimits.stale);
  renderUsageBadge(weeklyEl, rateLimits.seven_day, 'w', rateLimits.stale);
}
