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

// Provider-native quota badges (issues #326/#847), shared between Board and
// Coding. The backend selects one exact harness/provider source; this renderer
// treats every native bucket/window as data so Pi/Grok adapters need no new UI
// branch when verified evidence becomes available.

// Color tier for a usage percentage — same 60/80 thresholds as fleet-config's
// statusline-command.ps1, so every surface agrees on what counts as "close".
function usageTier(pct) {
  if (pct == null || isNaN(pct)) return 'muted';
  if (pct >= 80) return 'danger';
  if (pct >= 60) return 'warn';
  return 'good';
}

function fmtResetLocal(value) {
  if (value == null) return '';
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  if (isNaN(date.getTime())) return '';
  return new Intl.DateTimeFormat([], {
    month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
  }).format(date);
}

function durationLabel(minutes) {
  const value = Number(minutes);
  if (!Number.isFinite(value) || value <= 0) return '';
  if (value % 10080 === 0) return (value / 10080) + 'w';
  if (value % 1440 === 0) return (value / 1440) + 'd';
  if (value % 60 === 0) return (value / 60) + 'h';
  return value + 'm';
}

function nameLabel(value) {
  return String(value || '').replace(/[-_]+/g, ' ').replace(/^./, function (c) {
    return c.toUpperCase();
  });
}

function renderUsageBadge(el, item, sourceState, label) {
  if (!el) return;
  const windowData = item.window;
  const pct = typeof windowData.used_percentage === 'number'
    ? windowData.used_percentage : NaN;
  const measured = Number.isFinite(pct);
  const stale = sourceState === 'stale' || item.observationState === 'stale' || windowData.state === 'stale';
  const tier = measured ? usageTier(pct) : 'muted';
  el.className = 'usage-badge ' + tier + (stale ? ' stale' : '');
  const bits = [label, nameLabel(item.bucket), nameLabel(windowData.id)];
  const duration = durationLabel(windowData.duration_minutes);
  if (duration) bits.push(duration);
  bits.push(measured ? Math.round(pct) + '% used' : 'usage unknown');
  const resetTxt = fmtResetLocal(windowData.resets_at);
  if (resetTxt) bits.push('resets ' + resetTxt);
  if (item.sharedAccount) bits.push('shared account');
  if (stale) bits.push('stale');
  el.textContent = bits.filter(Boolean).join(' · ');
  el.hidden = false;
}

export function clearUsageBadgeRow(container) {
  if (!container) return;
  container.querySelectorAll('.usage-badge').forEach(function (badge) {
    badge.hidden = true;
    badge.textContent = '';
    if (badge.dataset.dynamic === 'true') badge.remove();
  });
  container.hidden = true;
}

function stateCopy(rateLimits) {
  const label = rateLimits.label || nameLabel(rateLimits.harness) || 'Quota';
  if (rateLimits.state === 'unknown') return label + ' quota unknown';
  if (rateLimits.state === 'unsupported') return label + ' quota unsupported';
  if (rateLimits.state === 'error') return label + ' quota unavailable';
  if (rateLimits.state === 'stale') return label + ' quota stale';
  return '';
}

function legacyObservations(rateLimits) {
  const windows = [];
  [['five_hour', 300], ['seven_day', 10080]].forEach(function (entry) {
    const item = rateLimits[entry[0]];
    if (!item) return;
    windows.push({
      id: entry[0], duration_minutes: entry[1],
      used_percentage: item.used_percentage, resets_at: item.resets_at,
      state: rateLimits.stale ? 'stale' : 'available',
    });
  });
  return windows.length ? [{ bucket: 'claude-code', state: rateLimits.stale ? 'stale' : 'available', windows }] : [];
}

// Update one row from the versioned provider view. The two legacy spans are
// reusable slots; additional native buckets get data-owned siblings.
export function renderUsageBadgeRow(container, sessionEl, weeklyEl, rateLimits) {
  if (!container) return;
  clearUsageBadgeRow(container);
  if (!rateLimits) return;
  const sourceState = rateLimits.state || (rateLimits.available
    ? (rateLimits.stale ? 'stale' : 'available') : 'unknown');
  const observations = Array.isArray(rateLimits.observations)
    ? rateLimits.observations : legacyObservations(rateLimits);
  const items = [];
  observations.forEach(function (observation) {
    (observation.windows || []).forEach(function (windowData) {
      items.push({
        bucket: observation.bucket,
        observationState: observation.state,
        sharedAccount: observation.shared_account === true,
        window: windowData,
      });
    });
  });

  const slots = [sessionEl, weeklyEl].filter(Boolean);
  while (slots.length < Math.max(1, items.length)) {
    const badge = document.createElement('span');
    badge.className = 'usage-badge';
    badge.dataset.dynamic = 'true';
    container.appendChild(badge);
    slots.push(badge);
  }
  container.dataset.harness = rateLimits.harness || '';
  container.dataset.provider = rateLimits.provider || '';
  container.dataset.state = sourceState;
  container.hidden = false;

  if (!items.length || !['available', 'stale'].includes(sourceState)) {
    slots[0].className = 'usage-badge quota-state ' + sourceState;
    slots[0].textContent = stateCopy({ ...rateLimits, state: sourceState });
    slots[0].hidden = false;
    return;
  }
  items.forEach(function (item, index) {
    renderUsageBadge(slots[index], item, sourceState, rateLimits.label || nameLabel(rateLimits.harness));
  });
}
