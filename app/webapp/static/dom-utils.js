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
// usage sheet and the CPU/system-load sheet.
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

function comboOptions(menu) {
  return Array.from(menu.querySelectorAll('[role="option"]'));
}

function enabledComboOptions(menu) {
  return comboOptions(menu).filter(function (option) { return !option.disabled; });
}

function focusComboOption(menu, which) {
  const available = enabledComboOptions(menu);
  if (!available.length) return;
  const selected = available.find(function (option) {
    return option.getAttribute('aria-selected') === 'true';
  });
  const target = which === 'last' ? available[available.length - 1]
    : which === 'first' ? available[0] : selected || available[0];
  target.focus();
}

function moveComboFocus(menu, direction) {
  const available = enabledComboOptions(menu);
  if (!available.length) return;
  const index = available.indexOf(document.activeElement);
  const next = index < 0
    ? (direction > 0 ? 0 : available.length - 1)
    : (index + direction + available.length) % available.length;
  available[next].focus();
}

// One option button from a setOptions item: a bare string, or
// { value, label, available, disabled, unavailable_reason, title }.
function comboOptionEl(item) {
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
  return option;
}

// Where the open menu sits. Outside a <dialog> it is portaled to <body> and
// held to the viewport, flipping above the trigger when there is more room
// there, until release() puts it back where the markup had it; inside one it
// stays in that dialog's top layer.
function comboMenuPlacement(root, trigger, menu) {
  let positionFrame = null;
  const menuParent = menu.parentNode;
  const menuNextSibling = menu.nextSibling;

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

  return {
    // Before the menu is shown: lift it out to <body> unless in a dialog.
    portal: function () {
      if (root.closest('dialog[open]')) return;
      document.body.appendChild(menu);
      menu.classList.add('model-combo-menu--portal');
    },
    // Once shown: place a portaled menu and follow the viewport.
    track: function () {
      if (!menu.classList.contains('model-combo-menu--portal')) return;
      window.addEventListener('resize', schedulePosition);
      document.addEventListener('scroll', schedulePosition, true);
      if (window.visualViewport) {
        window.visualViewport.addEventListener('resize', schedulePosition);
        window.visualViewport.addEventListener('scroll', schedulePosition);
      }
      positionMenu();
    },
    release: function () {
      window.removeEventListener('resize', schedulePosition);
      document.removeEventListener('scroll', schedulePosition, true);
      if (window.visualViewport) {
        window.visualViewport.removeEventListener('resize', schedulePosition);
        window.visualViewport.removeEventListener('scroll', schedulePosition);
      }
      if (positionFrame != null) {
        cancelAnimationFrame(positionFrame);
        positionFrame = null;
      }
      menu.classList.remove('model-combo-menu--portal');
      ['top', 'left', 'right', 'minWidth', 'maxWidth', 'maxHeight'].forEach(function (name) {
        menu.style[name] = '';
      });
      if (menu.parentNode === menuParent) return;
      if (menuNextSibling && menuNextSibling.parentNode === menuParent) {
        menuParent.insertBefore(menu, menuNextSibling);
      } else {
        menuParent.appendChild(menu);
      }
    },
  };
}

// The combo's keyboard: the listbox pattern on the trigger and in the menu.
// `combo` is { open(focus), close(focusTrigger), pick(option) }. The trigger's
// keys stop propagation, as its click does: it sits in a <summary>.
function comboKeyHandlers(menu, combo) {
  function stop(ev) {
    ev.preventDefault();
    ev.stopPropagation();
  }
  return {
    trigger: function (ev) {
      if (ev.key === 'Escape') {
        stop(ev);
        combo.close(true);
      } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(ev.key)) {
        stop(ev);
        const edge = ev.key === 'ArrowUp' || ev.key === 'End' ? 'last' : 'first';
        if (menu.hidden) combo.open(edge); else focusComboOption(menu, edge);
      } else if ((ev.key === 'Enter' || ev.key === ' ') && menu.hidden) {
        stop(ev);
        combo.open('selected');
      }
    },
    menu: function (ev) {
      if (ev.key === 'Escape') {
        stop(ev);
        combo.close(true);
      } else if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
        ev.preventDefault();
        moveComboFocus(menu, ev.key === 'ArrowDown' ? 1 : -1);
      } else if (ev.key === 'Home' || ev.key === 'End') {
        ev.preventDefault();
        focusComboOption(menu, ev.key === 'Home' ? 'first' : 'last');
      } else if (ev.key === 'Enter' || ev.key === ' ') {
        const opt = document.activeElement;
        if (!opt || opt.getAttribute('role') !== 'option' || opt.disabled) return;
        ev.preventDefault();
        combo.pick(opt);
      }
    },
  };
}

// A compact model-picker dropdown that is valid inside a <summary>.
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
//
// A menu that holds more than the model list (Life's launch options)
// wraps the options in a `.model-combo-list` listbox; setOptions then
// rebuilds that list alone and leaves the rest of the menu in place. A tap
// on anything that is not an option keeps the menu open.
// The value a combo moves to when its options drop the current one (a model
// switched off in Settings): the first enabled option of the same agent
// (`codex:` of `codex:gpt-…`), else the first enabled option, so the trigger
// never names a missing choice.
function fallbackComboValue(current, enabled) {
  const agent = current.includes(':') ? current.split(':')[0] + ':' : null;
  const next = enabled.find(function (option) {
    return agent && option.dataset.value.startsWith(agent);
  }) || enabled[0];
  return next ? next.dataset.value : '';
}

export function wireModelCombo(root, onChange) {
  if (!root) return null;
  const trigger = root.querySelector('.model-combo-trigger');
  const menu = root.querySelector('.model-combo-menu');
  if (!trigger || !menu) return null;
  const list = menu.querySelector('.model-combo-list') || menu;
  const placement = comboMenuPlacement(root, trigger, menu);
  let dispose = null;

  function findOption(value) {
    return comboOptions(menu).find(function (option) { return option.dataset.value === value; });
  }

  function close(focusTrigger) {
    if (menu.hidden) return;
    menu.hidden = true;
    trigger.setAttribute('aria-expanded', 'false');
    if (dispose) { dispose(); dispose = null; }
    placement.release();
    if (focusTrigger) trigger.focus();
  }

  function open(focusTarget) {
    if (!menu.hidden || trigger.disabled) return;
    placement.portal();
    menu.hidden = false;
    trigger.setAttribute('aria-expanded', 'true');
    dispose = bindOutsideClickToClose(menu, trigger, function () { close(true); });
    placement.track();
    if (focusTarget) focusComboOption(menu, focusTarget);
  }

  function apply(value, fire) {
    const opt = findOption(value);
    if (!opt) return;
    root.dataset.value = value;
    trigger.textContent = opt.textContent;
    comboOptions(menu).forEach(function (o) {
      o.setAttribute('aria-selected', o === opt ? 'true' : 'false');
    });
    if (fire && onChange) onChange(value);
  }

  function pick(opt) {
    apply(opt.dataset.value, true);
    close(true);
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
    pick(opt);
  }
  const keys = comboKeyHandlers(menu, { open: open, close: close, pick: pick });

  trigger.addEventListener('click', onTriggerClick);
  trigger.addEventListener('keydown', keys.trigger);
  menu.addEventListener('click', onMenuClick);
  menu.addEventListener('keydown', keys.menu);

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
      list.replaceChildren();
      (items || []).forEach(function (item) { list.appendChild(comboOptionEl(item)); });
      apply(findOption(current) ? current : fallbackComboValue(current, enabledComboOptions(menu)), false);
    },
    destroy: function () {
      close(false);
      trigger.removeEventListener('click', onTriggerClick);
      trigger.removeEventListener('keydown', keys.trigger);
      menu.removeEventListener('click', onMenuClick);
      menu.removeEventListener('keydown', keys.menu);
    },
  };
}

// One duration formatter for the Board card's compact age chip and the
// Coding tab's Running-sessions elapsed time — same quantity, kept at
// the two granularities each surface has always used (compact chip: "3h";
// the denser sessions row: "3h 24m"), parametrised over the two input
// shapes: a pre-computed duration in seconds (`fromEpoch: false`, the
// default), or an epoch second diffed against now (`fromEpoch: true`).
// `granular: true` also switches sub-minute output from "now" to "Ns" and
// shows hours+minutes, rolling into "Nd Nh" past 24 h.

// Whole local calendar days between `date` and today: positive when `date`
// is in the past, negative when it is in the future, 0 for today. `date`'s
// own year/month/day are read as given (in local time) — the caller owns
// parsing an ISO string or epoch into that shape first. The one day-diff
// scan shared by life-os.js's `daysSince`/`convoWhen`, screen-time.js's
// `whenText` and jobs-agenda.js's `_dayOffset`, so none of them redoes this
// `new Date(y, m, d)` + "today at midnight" + `Math.round(diff / 86400000)`
// walk on its own.
export function calendarDaysFromToday(date) {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const day = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  return Math.round((today - day) / 86400000);
}

// Past 24 h a whole-hours count reads as "11d 4h", not "268h"; below
// that it returns null so each caller keeps its own "Nh Nm" shape. Shared by
// fmtDuration's granular mode and jobs-row.js's formatDuration.
export function daysAndHours(hrs) {
  if (hrs < 24) return null;
  return Math.floor(hrs / 24) + 'd ' + (hrs % 24) + 'h';
}

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
  if (o.granular) return daysAndHours(hrs) || hrs + 'h ' + (mins % 60) + 'm';
  if (secs < 86400) return hrs + 'h';
  return Math.floor(secs / 86400) + 'd';
}

// The standing fleet chief (cross-tab parity): one label="chief"
// PTY session. Same label-first, name-fallback match everywhere a session
// might be the chief — a session-host that predates the label field still
// reports the chief's launch name, so the fallback keeps crown/tint/confirm
// working across that host's last pre-label run. Single source of truth for
// board.js (card rendering), sessions.js (Coding tab rows + stop guard), and
// terminal.js (overlay title) — do not re-derive this check independently.
export function isChiefSession(s) {
  return !!s && (s.label === 'chief' || (s.kind === 'pty' && s.name === 'chief'));
}
// The themed confirm's wording (confirmDialog, never a native
// confirm()). Same words as the native dialogs they replaced.
export const CHIEF_KILL_CONFIRM = { title: 'Kill the chief session?', action: 'Kill' };

// A Telegram channel session: the session-host label is
// `telegram:<profile id>`, set only by the Life OS channel launch. Its
// "Telegram · Health" name is derived from the id, so the Board and the Coding
// tab need nothing but the label the host already echoes.
const CHANNEL_LABEL_PREFIX = 'telegram:';
export function isChannelSession(s) {
  return !!s && typeof s.label === 'string' && s.label.indexOf(CHANNEL_LABEL_PREFIX) === 0;
}
// A managed session has its conversation somewhere else (a Telegram chat, the
// Board chat bar), so it never counts as work waiting on the owner. Adding a
// managed kind is one more clause here.
export function isManagedSession(s) {
  return isChannelSession(s) || isChiefSession(s);
}
export function channelSessionName(s) {
  if (!isChannelSession(s)) return '';
  const id = s.label.slice(CHANNEL_LABEL_PREFIX.length);
  return 'Telegram · ' + id.replace(/-/g, ' ').replace(/\b\w/g, function (c) { return c.toUpperCase(); });
}
export function channelKillConfirm(s) {
  return {
    title: 'Stop the ' + channelSessionName(s) + ' session?',
    message: 'Its bot goes offline until you start it again.',
    action: 'Stop',
  };
}
export const CHIEF_RESTART_CONFIRM = {
  title: 'Restart the chief?',
  message: 'It will stop the current one gracefully and resume the same ' +
    'conversation (falling back to a fresh one only if nothing is resumable).',
  action: 'Restart',
};

// A share as a whole-percent string — shared by the usage sheet and the
// CPU/system-load sheet.
export function pctText(pct) {
  return Math.round(pct) + '%';
}

// Color tier for a usage percentage — same 60/80 thresholds as fleet-config's
// statusline-command.ps1, so every surface agrees on what counts as "close".
export function usageTier(pct) {
  if (pct == null || isNaN(pct)) return 'muted';
  if (pct >= 80) return 'danger';
  if (pct >= 60) return 'warn';
  return 'good';
}

// Context use at which a Telegram channel session is flagged: the
// summary line's alert icon and the popup's highlighted Compact button. Lower
// than usageTier()'s 60 "warn" on purpose — a compaction wants a head start,
// not a last-minute scramble.
export const CONTEXT_ALERT_PCT = 50;

// True only for a real figure at or over the threshold; "not known" (null,
// NaN, a non-number) is never an alert.
export function contextAlert(pct) {
  return typeof pct === 'number' && pct >= CONTEXT_ALERT_PCT;
}

// A reset timestamp in an arbitrary `Intl.DateTimeFormat` shape: usage-meter.js's compact row
// stamps (clock only / day only — a
// full "Sep 11, 14:20" on both windows pushes a row past a 390px viewport)
// and its full sheet stamps are all this one formatter with a different
// `format` object, not three separate near-identical copies.
export function fmtResetFull(value, format) {
  if (value == null) return '';
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  if (isNaN(date.getTime())) return '';
  return new Intl.DateTimeFormat([], format).format(date);
}

// The date on a Board card's second line: the clock for a stamp from
// today ("15:29"), day + short month for any other day ("14 Oct"), in the
// viewer's local time. GitHub stamps are UTC; the conversion is the browser's,
// never a server offset. '' for a value that can't be read, so the caller drops
// the part. `now` is injectable for the unit pin.
export function fmtCardStamp(value, now) {
  if (value == null || value === '') return '';
  const date = new Date(value);
  if (isNaN(date.getTime())) return '';
  const ref = now || new Date();
  const sameDay = date.getFullYear() === ref.getFullYear() &&
    date.getMonth() === ref.getMonth() && date.getDate() === ref.getDate();
  return sameDay
    ? new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false }).format(date)
    : new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short' }).format(date);
}

export function nameLabel(value) {
  return String(value || '').replace(/[-_]+/g, ' ').replace(/^./, function (c) {
    return c.toUpperCase();
  });
}

// A long descriptive line cut down to a short name: at most `max` characters,
// ending on a whole word, with no trailing punctuation or unclosed bracket and
// no ellipsis. Text already within `max` comes back unchanged, so a field
// pre-filled from it never starts out over its own maxLength.
export function fitTitle(text, max) {
  const s = String(text || '');
  if (s.length <= max) return s;
  let out = s.replace(/\s+/g, ' ').trim();
  if (out.length <= max) return out;
  // Back up to the last space so no word is cut; one long word is cut hard.
  if (out.charAt(max) !== ' ') {
    const space = out.lastIndexOf(' ', max);
    out = space > 0 ? out.slice(0, space) : out.slice(0, max);
  } else {
    out = out.slice(0, max);
  }
  for (;;) {
    out = out.replace(/[\s.,;:!?\-–—/]+$/, '');
    const open = [];
    for (let i = 0; i < out.length; i++) {
      const c = out.charAt(i);
      if ('([{'.indexOf(c) !== -1) open.push(i);
      else if (')]}'.indexOf(c) !== -1) open.pop();
    }
    if (!open.length) return out;
    out = out.slice(0, open[open.length - 1]);
  }
}

// Linear pace: how much of the window has elapsed, as a whole
// percent, so weekly usage reads against it directly. Tuesday's `53%` means
// usage under 53% is under budget. The window starts `duration_minutes`
// before `resets_at` (7 days when the backend doesn't say). Null when the
// reset is missing, unparseable or already past: a past reset means the
// window this reading belongs to is over, and a pace from it would be
// stale, which is worse than none.
const DEFAULT_QUOTA_WINDOW_MINUTES = 7 * 24 * 60;

export function quotaPace(windowData, nowMs) {
  if (!windowData || windowData.resets_at == null) return null;
  const value = windowData.resets_at;
  const resetMs = typeof value === 'number' ? value * 1000 : Date.parse(value);
  if (isNaN(resetMs) || nowMs > resetMs) return null;
  const minutes = windowData.duration_minutes;
  const lengthMs = (typeof minutes === 'number' && minutes > 0
    ? minutes : DEFAULT_QUOTA_WINDOW_MINUTES) * 60000;
  const elapsed = (nowMs - (resetMs - lengthMs)) / lengthMs;
  return Math.round(Math.min(1, Math.max(0, elapsed)) * 100);
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
