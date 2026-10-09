/* Registered-job row rendering and the job sheet's detail block.
 *
 * jobs.js owns fetching, sorting, the sheet and the dialogs. This module
 * owns the row's DOM (#1438, step 6/7 of #1432): the shared action-row
 * anatomy, a job avatar with the alive badge while it runs, the name, one
 * meta line ("in 5m · Every 15 min"), chips for exceptions only, the
 * seven-dot history as the trailing value and one kebab. The poll rebuilds
 * the rows, so every handler sees the job the row was drawn from, never a
 * copy from an earlier poll (#1007).
 */

import { fmtAgo } from './sessions.js';
import { actionRow } from './action-rows.js';
import { avatar, chip } from './glance.js';
import { createRowMenu } from './row-menu.js';
import { icon } from './_vendored/icons/icons.js';

// One menu instance for every job row (the pattern apps-coding.js uses for
// the ⋯ project menu): it remembers which row was open across the 4s poll's
// re-render and reopens it there.
const jobMenu = createRowMenu('project-menu');

export function endJobRowRender() {
  jobMenu.endRender();
}

function fmtUntil(epochSeconds) {
  const secs = Math.floor(epochSeconds - Date.now() / 1000);
  if (secs <= 0) return 'due';
  if (secs < 3600) return 'in ' + Math.max(1, Math.round(secs / 60)) + 'm';
  if (secs < 86400) return 'in ' + Math.round(secs / 3600) + 'h';
  return 'in ' + Math.round(secs / 86400) + 'd';
}

/* Keyed by a run's `outcome` (issue #916), which is its persisted `status`
 * widened with `unconfirmed`: the scheduled-run adapter reserves a block of
 * exit codes for "this may well have delivered, but nobody established that",
 * and drawing those as failures is what made the Board's red stop meaning
 * anything. `status` still keys every other entry, so a caller that has only a
 * status (or a payload predating #916) renders exactly as before.
 *
 * `spark` is the history dot's class. A run in progress is `live`, drawn in
 * the accent (work in progress, #1438): amber means unconfirmed, and the
 * avatar's alive badge already says the job is running. */
const STATUS_META = {
  running: { icon: 'hourglass', spark: 'live' },
  pending: { icon: 'hourglass', spark: 'live' },
  success: { icon: 'circle-check', spark: 'up' },
  failed: { icon: 'circle-x', spark: 'down' },
  unconfirmed: { icon: 'circle-help', spark: 'unconfirmed' },
  // A designed deferral the job itself declared by exit code (#1316): muted,
  // because it needs nobody, and a hollow ring, so it is not mistaken for a
  // run that never happened.
  deferred: { icon: 'clock', spark: 'deferred' },
  skipped: { icon: 'skip-forward', spark: 'unknown' },
  queued: { icon: 'link', spark: 'live' },
  dry_run_success: { icon: 'flask-conical', spark: 'unknown' },
  dry_run_failed: { icon: 'flask-conical', spark: 'unknown' },
};
const DEFAULT_STATUS_META = { icon: '•', spark: 'unknown' };

function statusMeta(status) {
  return STATUS_META[status] || DEFAULT_STATUS_META;
}

export function statusIcon(status) {
  return statusMeta(status).icon;
}

/* A run's outcome, falling back to its persisted status. Every render path
 * goes through this so none of them can drift back to reading `status`. */
export function runOutcome(run) {
  if (!run) return '';
  return run.outcome || run.status || '';
}

export function formatDuration(seconds) {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return null;
  if (seconds < 10) return seconds.toFixed(1) + 's';
  if (seconds < 60) return Math.round(seconds) + 's';
  if (seconds < 3600) {
    const minutes = Math.floor(seconds / 60);
    const remainder = Math.round(seconds - minutes * 60);
    return minutes + 'm' + (remainder ? ' ' + remainder + 's' : '');
  }
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.round((seconds - hours * 3600) / 60);
  return hours + 'h' + (minutes ? ' ' + minutes + 'm' : '');
}

export function formatBytes(bytes) {
  if (bytes == null || !Number.isFinite(bytes) || bytes < 0) return null;
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  const fixed = value >= 10 || index === 0 ? value.toFixed(0) : value.toFixed(1);
  return fixed + ' ' + units[index];
}

export function toEpoch(isoString) {
  if (!isoString) return 0;
  const value = Date.parse(isoString);
  return Number.isFinite(value) ? Math.floor(value / 1000) : 0;
}

// The server's cadence text ("daily 03:00") as the start of a line: only
// the first letter is lifted, never the whole value (#1130).
export function cadenceText(text) {
  const t = (text || '').trim();
  return t ? t.charAt(0).toUpperCase() + t.slice(1) : '';
}

/* The row's one meta line: when it next fires, then how often. A running
 * job says so instead of its countdown; a paused one says what its schedule
 * was ("Was daily 07:00"), and its paused chip carries the state. */
export function jobMeta(job) {
  const cadence = (job.schedule_chip || '').trim();
  if (job.running) return 'running now' + (cadence ? ' · ' + cadenceText(cadence) : '');
  if (job.paused) return cadence ? 'Was ' + cadence : 'Paused';
  if (!cadence) return 'Manual only';
  return Number.isFinite(job.next_run_epoch)
    ? fmtUntil(job.next_run_epoch) + ' · ' + cadenceText(cadence)
    : cadenceText(cadence);
}

/* Chips for exceptions only (the tone map, #1432): a failed last run is
 * danger; stuck, not firing and not confirmed want a look, so attention;
 * paused is a plain fact, so neutral. A healthy job gets none. A running
 * job's last outcome is history, so it raises no chip of its own. */
export function jobChips(job) {
  const out = [];
  const outcome = job.last_run ? runOutcome(job.last_run) : '';
  const reason = job.last_run && job.last_run.outcome_reason;
  if (job.stuck) {
    out.push(chip('stuck', 'attention', 'job-stuck-chip'));
  } else if (!job.running && outcome === 'failed') {
    const c = chip('failed', 'danger', 'job-failed-chip');
    if (reason) c.title = reason;
    out.push(c);
  } else if (!job.running && outcome === 'unconfirmed') {
    const c = chip('not confirmed', 'attention', 'job-unconfirmed-chip');
    if (reason) c.title = reason;
    out.push(c);
  }
  // Missed-fire coverage (#697): only 'problem' raises it; 'ok', 'exempt'
  // and 'unknown' stay silent, so an unestablished fact never reads as one.
  if (job.coverage && job.coverage.state === 'problem') {
    const c = chip('not firing', 'attention', 'job-coverage-chip');
    c.title = job.coverage.detail || 'This schedule is not firing';
    out.push(c);
  }
  if (job.paused) out.push(chip('paused', 'neutral', 'job-paused-chip'));
  return out;
}

function renderSparkline(job) {
  const last7 = job.stats && Array.isArray(job.stats.last7) ? job.stats.last7 : [];
  if (!last7.length) return null;
  const span = document.createElement('span');
  span.className = 'job-sparkline';
  span.dataset.role = 'sparkline';
  span.setAttribute('aria-label', 'Last ' + last7.length + ' runs');
  last7.forEach(function (entry) {
    const dot = document.createElement('span');
    const status = runOutcome(entry);
    dot.className = 'job-spark-dot ' + statusMeta(status).spark;
    dot.title = (entry && entry.run_id ? entry.run_id + ' · ' : '') +
      (status === 'unconfirmed' ? 'not confirmed' : (status || 'unknown'));
    span.appendChild(dot);
  });
  return span;
}

// Why Run now is unavailable, or '' when it is not.
export function runBlockedReason(job) {
  if (job.running) return job.name + ' is running';
  if (job.manual_run_allowed === false) {
    return 'Runs through an externally managed task, so Run now is unavailable here';
  }
  return '';
}

// handlers: onOpen (the row tap), onRun, onPause, onEdit, onRemove.
export function renderJobRow(job, handlers) {
  const row = actionRow({
    id: job.id,
    className: 'job-row',
    title: job.name,
    meta: jobMeta(job),
    chips: jobChips(job),
    avatar: avatar('activity', job.running ? 'alive' : null, 'job-avatar'),
    label: 'Open ' + job.name,
    onMain: function () { handlers.onOpen(job); },
    kebabClass: 'job-menu-anchor',
    kebabLabel: job.name + ' actions',
  });
  row.meta.dataset.role = 'job-meta';
  const spark = renderSparkline(job);
  if (spark) row.main.appendChild(spark);

  const hasSchedule = job.paused ||
    (job.schedule && job.schedule.type && job.schedule.type !== 'none');
  const canPause = hasSchedule && job.schedule_controls_allowed !== false;
  row.li.appendChild(jobMenu.attach(job.id, row.kebab, [
    {
      className: 'job-run-item', glyph: 'play',
      label: 'Run ' + job.name + ' now', text: 'Run now',
      disabled: !!runBlockedReason(job),
      title: runBlockedReason(job),
      onTap: function () { handlers.onRun(job); },
    },
    {
      className: 'job-pause-item',
      glyph: job.paused ? 'play' : 'pause',
      label: (job.paused ? 'Resume schedule for ' : 'Pause schedule for ') + job.name,
      text: job.paused ? 'Resume' : 'Pause',
      hidden: !canPause,
      onTap: function () { handlers.onPause(job); },
    },
    {
      className: 'job-dry-run-item', glyph: 'flask-conical',
      label: 'Dry-run check', text: 'Dry-run check',
      onTap: function () { handlers.onRun(job, { dryRun: 'check', skipDialog: true }); },
    },
    {
      className: 'job-edit-item', glyph: 'pencil',
      label: 'Edit ' + job.name, text: 'Edit',
      onTap: function () { handlers.onEdit(job); },
    },
    {
      className: 'job-remove-item', glyph: 'trash-2', danger: true,
      label: 'Remove ' + job.name, text: 'Remove',
      onTap: function () { handlers.onRemove(job); },
    },
  ]));
  return row.li;
}

/* The last run, as one sentence, under the detail block's own "Last run"
 * key (#1130): the success rate and retention counts are sibling rows. */
function describeLastRun(job) {
  const bits = [];
  if (job.last_run) {
    const ago = fmtAgo(toEpoch(job.last_run.started_at));
    const status = runOutcome(job.last_run) || '?';
    const duration = formatDuration(job.last_run.duration_seconds);
    if (job.running || status === 'running' || status === 'pending') {
      bits.push('running now' + (ago ? ' · started ' + ago + ' ago' : ''));
    } else {
      const label = status === 'unconfirmed' ? 'not confirmed' : status;
      bits.push(label +
        (ago ? ' · ' + ago + ' ago' : '') +
        (duration ? ' · ' + duration : ''));
    }
  } else {
    bits.push('never run');
  }
  if (job.stuck) bits.push(icon('triangle-alert') + ' stuck');
  return bits.join(' · ');
}

function describeDuration(job) {
  const stats = job.stats || {};
  const p50 = formatDuration(stats.p50);
  const p95 = formatDuration(stats.p95);
  if (!p50 && !p95) return null;
  return p50 && p95 ? 'p50 ' + p50 + ' · p95 ' + p95 : 'p50 ' + (p50 || p95);
}

/* The sheet's detail block (#1130, in the sheet since #1438): what it runs,
 * its cadence and next fire, how long it takes, how often it succeeds, what
 * is kept, whether it alerts (the bell that left the row), and the
 * situational flags (an externally-managed schedule, a mutex group, a
 * webhook trigger). */
function detailRow(label, value) {
  if (value === null || value === undefined || value === '') return null;
  const row = document.createElement('div');
  row.className = 'job-detail-row';
  const key = document.createElement('span');
  key.className = 'job-detail-key';
  key.textContent = label;
  const val = document.createElement('span');
  val.className = 'job-detail-value';
  if (typeof value === 'string') val.innerHTML = value;
  else val.appendChild(value);
  row.append(key, val);
  return row;
}

function flagChip(className, glyph, text, title) {
  const el = chip('', 'neutral', className);
  el.innerHTML = icon(glyph) + ' ';
  el.append(text);
  el.title = title;
  return el;
}

export function renderJobDetails(job) {
  const section = document.createElement('section');
  section.className = 'job-details';
  section.dataset.role = 'job-details';

  const stats = job.stats || {};
  const successRate = stats.success_rate_30d;
  const kept = [];
  if (Number.isFinite(job.run_count)) kept.push(job.run_count + ' kept');
  if (Number.isFinite(job.pinned_count) && job.pinned_count > 0) {
    kept.push(job.pinned_count + ' pinned');
  }

  const rows = [
    detailRow('Type', job.target_kind || '?'),
    detailRow('Schedule', job.paused
      ? (job.schedule_chip || 'scheduled') + ' · paused'
      : (job.schedule_chip || 'manual only')),
    detailRow('Next run', job.next_run || null),
    detailRow('Duration', describeDuration(job)),
    detailRow('Success', successRate != null && Number.isFinite(successRate)
      ? Math.round(successRate * 100) + '% over 30 days' : null),
    detailRow('Runs', kept.length ? kept.join(' · ') : null),
    detailRow('Last run', describeLastRun(job)),
    detailRow('Alerts', job.alert_on_failure ? 'Telegram, on failure' : null),
  ];
  const alerts = rows[rows.length - 1];
  if (alerts) alerts.dataset.role = 'alerts-line';

  // The situational flags, kept as chips: each is a fact, not a measurement.
  const flags = document.createElement('div');
  flags.className = 'job-detail-flags';
  if (job.elevated) {
    flags.appendChild(flagChip('job-elevated-pill', 'lock', 'external schedule',
      'Runs through an externally managed elevated task. ' +
      'Run-now and schedule controls are unavailable here.'));
  }
  if (job.session_less) {
    flags.appendChild(flagChip('job-session-less-pill', 'moon', 'logged-out',
      'Registered to run whether the user is logged on or not (S4U). The ' +
      'Task Scheduler entry is externally managed, so schedule controls are ' +
      'unavailable here; re-register it from an elevated shell.'));
  }
  if (job.mutex_group) {
    const depth = Number.isFinite(job.queue_depth) ? job.queue_depth : 0;
    flags.appendChild(flagChip('job-mutex-pill', 'link',
      depth > 0 ? job.mutex_group + ' (' + depth + ')' : job.mutex_group,
      'Exclusive group: ' + job.mutex_group + (depth > 0 ? ' — ' + depth + ' queued' : '')));
  }
  if (job.webhook) {
    flags.appendChild(flagChip('job-webhook-pill', 'webhook', job.webhook.provider,
      'Webhook trigger (' + job.webhook.provider + ') — POST /api/jobs/' + job.id + '/hook'));
  }

  rows.forEach(function (row) { if (row) section.appendChild(row); });
  if (flags.childElementCount) section.appendChild(flags);
  return section;
}
