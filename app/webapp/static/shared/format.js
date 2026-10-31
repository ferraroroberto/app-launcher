/* Number, date and duration formatting shared across views. */

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

// One duration formatter for the Board card's compact age chip and the
// Coding tab's Running-sessions elapsed time — same quantity, kept at
// the two granularities each surface has always used (compact chip: "3h";
// the denser sessions row: "3h 24m"), parametrised over the two input
// shapes: a pre-computed duration in seconds (`fromEpoch: false`, the
// default), or an epoch second diffed against now (`fromEpoch: true`).
// `granular: true` also switches sub-minute output from "now" to "Ns" and
// shows hours+minutes, rolling into "Nd Nh" past 24 h.
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
