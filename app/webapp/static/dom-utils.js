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
// setValue — so a config round-trip that calls setValue can't loop. The menu
// is portaled to <body> while open so toolbar/card overflow cannot clip it;
// controls inside a native <dialog> stay in that top layer. Returns the one
// controller used by every model surface, or null when markup is absent.
export function wireModelCombo(root, onChange) {
  if (!root) return null;
  const trigger = root.querySelector('.model-combo-trigger');
  const menu = root.querySelector('.model-combo-menu');
  if (!trigger || !menu) return null;
  let dispose = null;
  let positionFrame = null;
  const menuParent = menu.parentNode;
  const menuNextSibling = menu.nextSibling;

  function options() {
    return Array.from(menu.querySelectorAll('[role="option"]'));
  }

  function enabledOptions() {
    return options().filter(function (option) { return !option.disabled; });
  }

  function findOption(value) {
    return options().find(function (option) { return option.dataset.value === value; });
  }

  function clearPosition() {
    if (positionFrame != null) {
      cancelAnimationFrame(positionFrame);
      positionFrame = null;
    }
    menu.classList.remove('model-combo-menu--portal');
    ['top', 'left', 'right', 'minWidth', 'maxWidth', 'maxHeight'].forEach(function (name) {
      menu.style[name] = '';
    });
  }

  function restoreMenu() {
    if (menu.parentNode === menuParent) return;
    if (menuNextSibling && menuNextSibling.parentNode === menuParent) {
      menuParent.insertBefore(menu, menuNextSibling);
    } else {
      menuParent.appendChild(menu);
    }
  }

  function positionMenu() {
    positionFrame = null;
    if (menu.hidden || !menu.classList.contains('model-combo-menu--portal')) return;
    const gap = 4;
    const edge = 8;
    const rect = trigger.getBoundingClientRect();
    const viewportWidth = document.documentElement.clientWidth;
    const viewportHeight = document.documentElement.clientHeight;
    const availableBelow = Math.max(0, viewportHeight - rect.bottom - gap - edge);
    const availableAbove = Math.max(0, rect.top - gap - edge);
    const wantedHeight = Math.min(menu.scrollHeight, 280);
    const openAbove = availableBelow < wantedHeight && availableAbove > availableBelow;
    const available = openAbove ? availableAbove : availableBelow;

    menu.style.minWidth = Math.ceil(rect.width) + 'px';
    menu.style.maxWidth = Math.max(0, viewportWidth - edge * 2) + 'px';
    menu.style.maxHeight = Math.max(36, available) + 'px';

    const measured = menu.getBoundingClientRect();
    const left = Math.min(
      Math.max(edge, rect.left),
      Math.max(edge, viewportWidth - edge - measured.width)
    );
    const top = openAbove
      ? Math.max(edge, rect.top - gap - measured.height)
      : Math.min(viewportHeight - edge - measured.height, rect.bottom + gap);
    menu.style.left = Math.round(left) + 'px';
    menu.style.top = Math.round(Math.max(edge, top)) + 'px';
  }

  function schedulePosition() {
    if (positionFrame != null) return;
    positionFrame = requestAnimationFrame(positionMenu);
  }

  function bindPositioning() {
    window.addEventListener('resize', schedulePosition);
    document.addEventListener('scroll', schedulePosition, true);
    if (window.visualViewport) {
      window.visualViewport.addEventListener('resize', schedulePosition);
      window.visualViewport.addEventListener('scroll', schedulePosition);
    }
  }

  function unbindPositioning() {
    window.removeEventListener('resize', schedulePosition);
    document.removeEventListener('scroll', schedulePosition, true);
    if (window.visualViewport) {
      window.visualViewport.removeEventListener('resize', schedulePosition);
      window.visualViewport.removeEventListener('scroll', schedulePosition);
    }
  }

  function close(focusTrigger) {
    if (menu.hidden) return;
    menu.hidden = true;
    trigger.setAttribute('aria-expanded', 'false');
    if (dispose) { dispose(); dispose = null; }
    unbindPositioning();
    clearPosition();
    restoreMenu();
    if (focusTrigger) trigger.focus();
  }

  function focusOption(which) {
    const available = enabledOptions();
    if (!available.length) return;
    const selected = available.find(function (option) {
      return option.getAttribute('aria-selected') === 'true';
    });
    const target = which === 'last' ? available[available.length - 1]
      : which === 'first' ? available[0] : selected || available[0];
    target.focus();
  }

  function open(focusTarget) {
    if (!menu.hidden || trigger.disabled) return;
    if (!root.closest('dialog[open]')) {
      document.body.appendChild(menu);
      menu.classList.add('model-combo-menu--portal');
    }
    menu.hidden = false;
    trigger.setAttribute('aria-expanded', 'true');
    dispose = bindOutsideClickToClose(menu, trigger, function () { close(true); });
    if (menu.classList.contains('model-combo-menu--portal')) {
      bindPositioning();
      positionMenu();
    }
    if (focusTarget) focusOption(focusTarget);
  }

  function apply(value, fire) {
    const opt = findOption(value);
    if (!opt) return;
    root.dataset.value = value;
    trigger.textContent = opt.textContent;
    options().forEach(function (o) {
      o.setAttribute('aria-selected', o === opt ? 'true' : 'false');
    });
    if (fire && onChange) onChange(value);
  }

  function moveFocus(direction) {
    const available = enabledOptions();
    if (!available.length) return;
    const index = available.indexOf(document.activeElement);
    const next = index < 0
      ? (direction > 0 ? 0 : available.length - 1)
      : (index + direction + available.length) % available.length;
    available[next].focus();
  }

  // A tap anywhere in a <summary> also toggles its <details>, so every
  // interactive handler here stops propagation (same guard the sibling
  // Detached/Resume toggles use).
  function onTriggerClick(ev) {
    ev.preventDefault();
    ev.stopPropagation();
    if (menu.hidden) open(false); else close(false);
  }
  function onMenuClick(ev) {
    ev.stopPropagation();
    const opt = ev.target.closest('[data-value]');
    if (!opt || opt.disabled) return;
    apply(opt.dataset.value, true);
    close(true);
  }
  function onTriggerKeydown(ev) {
    if (ev.key === 'Escape') {
      ev.preventDefault();
      ev.stopPropagation();
      close(true);
    } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(ev.key)) {
      ev.preventDefault();
      ev.stopPropagation();
      const edge = ev.key === 'ArrowUp' || ev.key === 'End' ? 'last' : 'first';
      if (menu.hidden) open(edge); else focusOption(edge);
    } else if ((ev.key === 'Enter' || ev.key === ' ') && menu.hidden) {
      ev.preventDefault();
      ev.stopPropagation();
      open('selected');
    }
  }
  function onMenuKeydown(ev) {
    if (ev.key === 'Escape') {
      ev.preventDefault();
      ev.stopPropagation();
      close(true);
    } else if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      ev.preventDefault();
      moveFocus(ev.key === 'ArrowDown' ? 1 : -1);
    } else if (ev.key === 'Home' || ev.key === 'End') {
      ev.preventDefault();
      focusOption(ev.key === 'Home' ? 'first' : 'last');
    } else if (ev.key === 'Enter' || ev.key === ' ') {
      const opt = document.activeElement;
      if (!opt || opt.getAttribute('role') !== 'option' || opt.disabled) return;
      ev.preventDefault();
      apply(opt.dataset.value, true);
      close(true);
    }
  }

  trigger.addEventListener('click', onTriggerClick);
  trigger.addEventListener('keydown', onTriggerKeydown);
  menu.addEventListener('click', onMenuClick);
  menu.addEventListener('keydown', onMenuKeydown);

  return {
    setValue: function (v) { apply(v, false); },
    getValue: function () { return root.dataset.value || ''; },
    setDisabled: function (disabled) {
      trigger.disabled = !!disabled;
      trigger.setAttribute('aria-disabled', String(!!disabled));
      if (disabled) close(false);
    },
    setOptions: function (items) {
      const current = root.dataset.value || '';
      close(false);
      menu.replaceChildren();
      (items || []).forEach(function (item) {
        const data = typeof item === 'string'
          ? { value: item, label: item, available: true }
          : item;
        const option = document.createElement('button');
        option.type = 'button';
        option.setAttribute('role', 'option');
        option.setAttribute('aria-selected', 'false');
        option.tabIndex = -1;
        option.dataset.value = data.value || '';
        option.textContent = data.label == null ? data.value : data.label;
        option.disabled = data.available === false || data.disabled === true;
        option.setAttribute('aria-disabled', String(option.disabled));
        if (option.disabled) {
          option.title = data.unavailable_reason || data.title || 'Unavailable';
        } else if (data.title) {
          option.title = data.title;
        }
        menu.appendChild(option);
      });
      if (findOption(current)) apply(current, false);
    },
    destroy: function () {
      close(false);
      trigger.removeEventListener('click', onTriggerClick);
      trigger.removeEventListener('keydown', onTriggerKeydown);
      menu.removeEventListener('click', onMenuClick);
      menu.removeEventListener('keydown', onMenuKeydown);
    },
  };
}

// One duration formatter for the Board card's compact age chip and the
// Coding tab's Running-sessions elapsed time (#750) — same quantity, kept at
// the two granularities each surface has always used (compact chip: "3h";
// the denser sessions row: "3h 24m"), parametrised over the two input
// shapes: a pre-computed duration in seconds (`fromEpoch: false`, the
// default), or an epoch second diffed against now (`fromEpoch: true`).
// `granular: true` also switches sub-minute output from "now" to "Ns" and
// caps the format at hours+minutes instead of rolling into days.
export function fmtDuration(value, opts) {
  const o = opts || {};
  let secs;
  if (o.fromEpoch) {
    if (!value) return '';
    secs = Math.max(0, Math.floor(Date.now() / 1000 - value));
  } else {
    if (value == null || isNaN(value)) return '';
    secs = value;
  }
  if (secs < 60) return o.granular ? secs + 's' : 'now';
  const mins = Math.floor(secs / 60);
  if (mins < 60) return mins + 'm';
  const hrs = Math.floor(mins / 60);
  if (o.granular) return hrs + 'h ' + (mins % 60) + 'm';
  if (secs < 86400) return hrs + 'h';
  return Math.floor(secs / 86400) + 'd';
}

// The standing fleet chief (#245, cross-tab parity #547): one label="chief"
// PTY session. Same label-first, name-fallback match everywhere a session
// might be the chief — a session-host that predates the label field still
// reports the chief's launch name, so the fallback keeps crown/tint/confirm
// working across that host's last pre-label run. Single source of truth for
// board.js (card rendering), sessions.js (Coding tab rows + stop guard), and
// terminal.js (overlay title) — do not re-derive this check independently.
export function isChiefSession(s) {
  return !!s && (s.label === 'chief' || (s.kind === 'pty' && s.name === 'chief'));
}
export const CHIEF_KILL_CONFIRM = 'Kill the chief session?';
export const CHIEF_RESTART_CONFIRM =
  'Restart the chief? It will stop the current one gracefully and resume ' +
  'the same conversation (falling back to a fresh one only if nothing is ' +
  'resumable).';

// Provider-native quota rows (issues #326/#847/#860), shared between Board
// and Coding. The backend hands over one already-collapsed row per heavy
// agent — no native bucket ids reach the UI — and this renderer only turns
// each into a single nowrap line.

// Color tier for a usage percentage — same 60/80 thresholds as fleet-config's
// statusline-command.ps1, so every surface agrees on what counts as "close".
function usageTier(pct) {
  if (pct == null || isNaN(pct)) return 'muted';
  if (pct >= 80) return 'danger';
  if (pct >= 60) return 'warn';
  return 'good';
}

// Compact reset stamps for the two-line rows (#860). A full "Sep 11, 14:20"
// on both windows is what pushes a line past a 390px viewport, so the
// 5-hour window — which always resets today or tomorrow — shows the clock
// only, and the weekly one shows the day only.
function fmtResetClock(value) {
  if (value == null) return '';
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  if (isNaN(date.getTime())) return '';
  return new Intl.DateTimeFormat([], { hour: 'numeric', minute: '2-digit' }).format(date);
}

function fmtResetDay(value) {
  if (value == null) return '';
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  if (isNaN(date.getTime())) return '';
  return new Intl.DateTimeFormat([], { month: 'short', day: 'numeric' }).format(date);
}

function nameLabel(value) {
  return String(value || '').replace(/[-_]+/g, ' ').replace(/^./, function (c) {
    return c.toUpperCase();
  });
}

// ------------------------------------------------- compact quota lines
//
// Two fixed rows — Claude Code above Codex — always both, whatever the
// model picker is pointed at (issue #860). Each is one nowrap line:
//
//   Claude Code · 5h 39% ↻ 14:20 · 1w 19% ↻ Sep 11
//
// The whole line carries the tier colour (no status dot), driven by the
// *worse* of its two windows so a line reddens as soon as either does.
// Both surfaces (Coding + Board) render through here; the backend has
// already collapsed the native buckets to one pair of windows, so this
// function never sees a bucket id.

const QUOTA_LINE_STATE_COPY = {
  unknown: 'unknown',
  unsupported: 'unsupported',
  error: 'unavailable',
};

// Last measured pair per harness, for the life of the page.
//
// Claude's shard is only rewritten when a session paints its statusline and
// carries a 10-minute expiry, so an idle stretch routinely takes the source
// to `unknown` with a perfectly good last reading behind it. Blanking the
// row there is worse than useless — the numbers vanish exactly when you sit
// down to decide what to launch. So an unmeasured row falls back to what it
// last showed, dimmed and with the state word appended: never silently, and
// never folded into the confident state (global CLAUDE.md — a check that
// failed to establish a fact reports that as its own state).
const lastMeasuredQuota = new Map();

function quotaLineTier(windows) {
  const pcts = windows
    .map(function (w) { return w && w.used_percentage; })
    .filter(function (v) { return typeof v === 'number' && !isNaN(v); });
  if (!pcts.length) return 'muted';
  return usageTier(Math.max.apply(null, pcts));
}

function quotaWindowText(windowData, label, fmtReset) {
  if (!windowData || typeof windowData.used_percentage !== 'number') return '';
  let text = label + ' ' + Math.round(windowData.used_percentage) + '%';
  const reset = fmtReset(windowData.resets_at);
  if (reset) text += ' ↻ ' + reset;
  return text;
}

// ``withResets: false`` for a fallback reading — a percentage that is no
// longer confirmed has a reset time that may already be in the past, so
// printing it would be worse than omitting it, and the width it frees is
// what keeps the "unknown" marker itself from being the part that gets
// ellipsed off a 390px line.
function quotaWindowTexts(pair, withResets) {
  const noReset = function () { return ''; };
  return [
    quotaWindowText(pair[0], '5h', withResets ? fmtResetClock : noReset),
    quotaWindowText(pair[1], '1w', withResets ? fmtResetDay : noReset),
  ].filter(Boolean);
}

export function renderQuotaLines(container, lines) {
  if (!container) return;
  const rows = Array.isArray(lines) ? lines : [];
  const slots = Array.from(container.querySelectorAll('.quota-line'));
  slots.forEach(function (slot, index) {
    const line = rows[index];
    if (!line) {
      slot.hidden = true;
      slot.textContent = '';
      return;
    }
    const harness = line.harness || '';
    let pair = [line.five_hour, line.weekly];
    let texts = quotaWindowTexts(pair, true);
    let stale = line.state === 'stale' || line.stale === true;
    let note = '';

    if (texts.length) {
      lastMeasuredQuota.set(harness, pair);
      if (stale) note = 'stale';
    } else {
      // Nothing measured this poll — show the last good reading, marked.
      // Dimming means "these numbers are no longer confirmed", so it only
      // applies when there are numbers; a row with nothing to fall back to
      // is already `muted`, and stacking the two just makes it hard to read.
      pair = lastMeasuredQuota.get(harness) || [null, null];
      texts = quotaWindowTexts(pair, false);
      note = QUOTA_LINE_STATE_COPY[line.state] || 'unknown';
      stale = texts.length > 0;
    }

    slot.dataset.harness = harness;
    slot.dataset.state = line.state || '';
    slot.className = 'quota-line ' + (texts.length ? quotaLineTier(pair) : 'muted') +
      (stale ? ' stale' : '');
    // "Claude Code · quota unknown" when there is nothing to show at all;
    // the bare word when it only qualifies numbers already on the line.
    const suffix = note ? [texts.length ? note : 'quota ' + note] : [];
    const bits = [line.label || nameLabel(harness)].concat(texts, suffix);
    slot.textContent = bits.join(' · ');
    slot.title = slot.textContent;
    slot.hidden = false;
  });
}
