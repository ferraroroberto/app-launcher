/* Registered-job row rendering and in-place poll updates.
 *
 * jobs.js owns fetching, sorting, dialogs, and expanded history. This module
 * owns the compact row's DOM contract so its initial render and poll-time
 * patch cannot drift apart.
 */

import { fmtAgo } from './sessions.js';
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

function renderCountdownChip(job) {
  if (!Number.isFinite(job.next_run_epoch)) return null;
  const chip = document.createElement('span');
  chip.className = 'kind-pill job-countdown-chip';
  // No glyph (#1130): line 2 has to hold this, the cadence and the seven-dot
  // history on one line at 390px, and the accent colour already sets "when
  // it next fires" apart from the cadence beside it.
  chip.textContent = 'Next ' + fmtUntil(job.next_run_epoch);
  chip.title = job.next_run
    ? 'Next scheduled run: ' + job.next_run
    : 'Next scheduled run';
  return chip;
}

/* Keyed by a run's `outcome` (issue #916), which is its persisted `status`
 * widened with `unconfirmed`: the scheduled-run adapter reserves a block of
 * exit codes for "this may well have delivered, but nobody established that",
 * and drawing those as failures is what made the Board's red stop meaning
 * anything. `status` still keys every other entry, so a caller that has only a
 * status (or a payload predating #916) renders exactly as before. */
const STATUS_META = {
  running: { class: 'up', icon: 'hourglass', spark: 'live' },
  pending: { class: '', icon: 'hourglass', spark: 'live' },
  success: { class: 'up', icon: 'circle-check', spark: 'up' },
  failed: { class: 'down', icon: 'circle-x', spark: 'down' },
  unconfirmed: { class: 'unconfirmed', icon: 'circle-help', spark: 'unconfirmed' },
  skipped: { class: '', icon: 'skip-forward', spark: 'unknown' },
  queued: { class: '', icon: 'link', spark: 'live' },
  dry_run_success: { class: '', icon: 'flask-conical', spark: 'unknown' },
  dry_run_failed: { class: '', icon: 'flask-conical', spark: 'unknown' },
};
const DEFAULT_STATUS_META = { class: '', icon: '•', spark: 'unknown' };

function statusMeta(status) {
  return STATUS_META[status] || DEFAULT_STATUS_META;
}

export function statusIcon(status) {
  return statusMeta(status).icon;
}

function sparkClass(status) {
  return statusMeta(status).spark;
}

/* A run's outcome, falling back to its persisted status. Every render path
 * goes through this so none of them can drift back to reading `status`. */
export function runOutcome(run) {
  if (!run) return '';
  return run.outcome || run.status || '';
}

function statusClass(job) {
  if (job.stuck) return 'stuck';
  if (job.running) return 'up';
  return job.last_run ? statusMeta(runOutcome(job.last_run)).class : '';
}

/* Single owner of the status dot's rendered state, shared by the initial
 * render and the poll-time patch so the two cannot drift (this module's whole
 * reason for existing). The title carries the exit code's own one-liner when
 * the adapter defines one — "stalled …", "the run reported it delivered no
 * work" — so an amber or red dot says why without a trip to the log (#916). */
function applyStatusDot(dotEl, job) {
  dotEl.className = 'health-dot ' + statusClass(job);
  const reason = job.last_run && job.last_run.outcome_reason;
  if (reason) dotEl.title = reason;
  else dotEl.removeAttribute('title');
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

function renderDurationChip(job) {
  const stats = job.stats || {};
  const p50 = formatDuration(stats.p50);
  const p95 = formatDuration(stats.p95);
  if (!p50 && !p95) return null;
  const chip = document.createElement('span');
  chip.className = 'kind-pill job-duration-chip';
  chip.textContent = p50 && p95
    ? 'p50 ' + p50 + ' · p95 ' + p95
    : 'p50 ' + (p50 || p95);
  chip.title = 'Duration percentiles across the last ' +
    (stats.completed_count || 0) + ' completed run(s)';
  return chip;
}

/* Missed-fire coverage badge (issue #697): the schedule isn't firing at all —
 * a missing/disabled Task Scheduler entry, or an elapsed slot that produced no
 * run record. Only 'problem' renders; 'ok'/'exempt'/'unknown' stay silent, so
 * an unestablished fact never reads as an alert. */
function renderCoveragePill(job) {
  const coverage = job.coverage;
  if (!coverage || coverage.state !== 'problem') return null;
  const pill = document.createElement('span');
  pill.className = 'kind-pill job-coverage-pill';
  pill.innerHTML = icon('triangle-alert') + ' not firing';
  pill.title = coverage.detail || 'This schedule is not firing';
  pill.setAttribute('aria-label', 'Schedule not firing: ' + (coverage.detail || ''));
  return pill;
}

// The server's cadence text ("daily 03:00") reads as a sentence on the row;
// the uppercase transform that used to make it "DAILY 03:00" is gone with
// #1130, so only the first letter is lifted here.
function renderCadenceChip(job) {
  const text = (job.schedule_chip || '').trim();
  if (!text) return null;
  const chip = document.createElement('span');
  chip.className = 'kind-pill job-cadence-chip';
  chip.textContent = text.charAt(0).toUpperCase() + text.slice(1);
  chip.title = 'Schedule: ' + text;
  return chip;
}

function renderSparkline(job) {
  const last7 = job.stats && Array.isArray(job.stats.last7) ? job.stats.last7 : [];
  if (!last7.length) return null;
  const span = document.createElement('span');
  span.className = 'job-sparkline';
  span.setAttribute('aria-label', 'Last ' + last7.length + ' runs');
  last7.forEach(function (entry) {
    const dot = document.createElement('span');
    const status = runOutcome(entry);
    const className = sparkClass(status);
    dot.className = 'job-spark-dot' + (className ? ' ' + className : '');
    dot.title = (entry && entry.run_id ? entry.run_id + ' · ' : '') +
      (status === 'unconfirmed' ? 'not confirmed' : (status || 'unknown'));
    span.appendChild(dot);
  });
  return span;
}

/* The last run, as one sentence. Since #1130 it sits under the detail
 * block's own "Last run" key, so it no longer carries the `last:` prefix,
 * nor the success rate and retention counts — those are sibling rows there,
 * and printing them twice is what made the old row's meta line unreadable. */
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

export function toEpoch(isoString) {
  if (!isoString) return 0;
  const value = Date.parse(isoString);
  return Number.isFinite(value) ? Math.floor(value / 1000) : 0;
}

function setRunBtnState(button, job) {
  button.innerHTML = job.running ? icon('hourglass') : icon('play');
  // Icon-only on purpose (#1238 J-01), so the accessible name carries the
  // job: a bare "Run now" on every row left a screen reader unable to say
  // which job each one runs.
  const label = job.running ? job.name + ' is running' : 'Run ' + job.name + ' now';
  button.title = label;
  button.setAttribute('aria-label', label);
  button.disabled = !!job.running;
}

export function renderJobRow(job, options) {
  const handlers = options || {};
  // Every action handler below reads the job through this holder rather than
  // closing over the `job` parameter (#1007). The 4s poll replaces every job
  // object (`jobs.js`: `state.jobs = body.jobs || []`) and then reuses the
  // existing <li> whenever sort order is unchanged, patching only the status
  // dot, meta line, run-button state and chips -- the buttons and their
  // listeners are never rebuilt. A handler closed over the render-time object
  // therefore acts on a stale copy while the row *renders* the fresh one: a
  // job newly flagged "Require confirmation" elsewhere would run with no
  // dialog, and Edit would reopen with the stale schedule/params so Save
  // silently clobbered the concurrent change. `patchRowNodes` refreshes this
  // one holder, so a single assignment re-points all six handlers at once.
  const ref = { job: job };
  const li = document.createElement('li');
  li.className = 'app-item job-item';
  li.dataset.id = job.id;

  const main = document.createElement('div');
  main.className = 'app-main';
  const info = document.createElement('button');
  info.type = 'button';
  info.className = 'launch-btn session-open';

  const head = document.createElement('div');
  head.className = 'session-head job-row-head';
  const dot = document.createElement('span');
  applyStatusDot(dot, job);
  dot.dataset.role = 'status-dot';
  head.appendChild(dot);
  const name = document.createElement('span');
  name.className = 'name';
  name.textContent = job.name;
  name.title = job.name;
  head.appendChild(name);
  if (job.alert_on_failure) {
    const alertIcon = document.createElement('span');
    alertIcon.className = 'job-alert-icon';
    alertIcon.dataset.role = 'alert-icon';
    alertIcon.innerHTML = icon('bell');
    alertIcon.title = 'Alerts to Telegram on failure';
    alertIcon.setAttribute('aria-label', 'Alerts to Telegram on failure');
    head.appendChild(alertIcon);
  }
  info.appendChild(head);

  // Line 2 (#1130): when it next runs, how often, and how the last seven
  // went. Everything else the row used to carry -- type, percentiles, the
  // last-run sentence, success rate, retention, and the situational chips --
  // moved into the detail block the row opens (renderJobDetails below), so
  // the row reads in one glance instead of fourteen data points.
  const pills = document.createElement('div');
  pills.className = 'job-row-pills';
  pills.dataset.role = 'job-pills';
  const countdown = renderCountdownChip(job);
  if (countdown) {
    countdown.dataset.role = 'countdown-chip';
    pills.appendChild(countdown);
  }
  const cadence = renderCadenceChip(job);
  if (cadence) {
    cadence.dataset.role = 'cadence-chip';
    pills.appendChild(cadence);
  }
  const spark = renderSparkline(job);
  if (spark) {
    spark.dataset.role = 'sparkline';
    pills.appendChild(spark);
  }
  // An alert, not a detail: a schedule that is not firing stays on the row.
  // Last, so the poll-time patch can append/remove it without an anchor.
  const coverage = renderCoveragePill(job);
  if (coverage) {
    coverage.dataset.role = 'coverage-chip';
    pills.appendChild(coverage);
  }
  info.appendChild(pills);

  info.title = 'View run history for ' + job.name;
  info.setAttribute('aria-label', 'View run history for ' + job.name);
  info.addEventListener('click', function () {
    if (handlers.onToggle) handlers.onToggle(ref.job);
  });
  main.appendChild(info);
  li.appendChild(main);

  // One visible action (#1130): Run, at the tint tier and the 44px touch
  // floor. Pause/Resume, the dry-run check, Edit and Remove move into the
  // row's ⋯ menu -- the same shared menu the Coding tile uses -- so the rail
  // stops being a stack of five equally-weighted glyphs.
  const actions = document.createElement('div');
  actions.className = 'row-actions session-actions job-row-actions';
  let run = null;
  if (job.manual_run_allowed !== false) {
    run = document.createElement('button');
    run.type = 'button';
    run.className = 'button-tint job-run-btn';
    run.dataset.role = 'run-btn';
    setRunBtnState(run, job);
    run.addEventListener('click', function (event) {
      event.stopPropagation();
      if (handlers.onRun) handlers.onRun(ref.job);
    });
    actions.appendChild(run);
  }

  const hasSchedule = job.paused ||
    (job.schedule && job.schedule.type && job.schedule.type !== 'none');
  const canPause = hasSchedule && job.schedule_controls_allowed !== false;
  // Every item reads `ref.job`, not the render-time `job`: the poll reuses
  // this <li> and re-points `ref` (#1007), and the menu outlives the render.
  const menuItems = [
    {
      glyph: function () { return ref.job.paused ? 'play' : 'pause'; },
      label: function () {
        return (ref.job.paused ? 'Resume schedule for ' : 'Pause schedule for ') + ref.job.name;
      },
      text: function () { return ref.job.paused ? 'Resume' : 'Pause'; },
      hidden: !canPause,
      onTap: function () { if (handlers.onPause) handlers.onPause(ref.job); },
    },
    {
      glyph: 'flask-conical',
      label: 'Dry-run check',
      text: 'Dry-run check',
      hidden: !handlers.editMode,
      onTap: function () {
        if (handlers.onRun) handlers.onRun(ref.job, { dryRun: 'check', skipDialog: true });
      },
    },
    {
      glyph: 'pencil',
      label: 'Edit',
      text: 'Edit',
      hidden: !handlers.editMode,
      onTap: function () { if (handlers.onEdit) handlers.onEdit(ref.job); },
    },
    {
      glyph: 'trash-2',
      className: 'danger',
      danger: true,
      label: 'Remove',
      text: 'Remove',
      hidden: !handlers.editMode,
      onTap: function () { if (handlers.onRemove) handlers.onRemove(ref.job); },
    },
  ];
  if (menuItems.some(function (item) { return !item.hidden; })) {
    const anchor = document.createElement('button');
    anchor.type = 'button';
    anchor.className = 'icon-btn job-menu-anchor';
    anchor.dataset.role = 'job-menu';
    anchor.innerHTML = icon('ellipsis-vertical');
    anchor.title = 'Job actions for ' + job.name;
    anchor.setAttribute('aria-label', 'Job actions');
    anchor.addEventListener('click', function (event) { event.stopPropagation(); });
    const menuEl = jobMenu.attach(job.id, anchor, menuItems);
    actions.appendChild(anchor);
    actions.appendChild(menuEl);
  } else if (actions.childElementCount) {
    // No visible menu item: keep the kebab's slot so Run lines up with every
    // other row's (#1207). The same box, never seen, focused or announced.
    const slot = document.createElement('button');
    slot.type = 'button';
    slot.className = 'icon-btn job-menu-slot';
    slot.disabled = true;
    slot.tabIndex = -1;
    slot.setAttribute('aria-hidden', 'true');
    slot.innerHTML = icon('ellipsis-vertical');
    actions.appendChild(slot);
  }

  if (actions.childElementCount) li.appendChild(actions);

  const nodes = {
    li: li,
    ref: ref,
    dotEl: dot,
    nameEl: name,
    pillsEl: pills,
    runBtnEl: run,
    countdownEl: countdown,
    cadenceEl: cadence,
    coverageEl: coverage,
    sparkEl: spark,
  };
  li._rowNodes = nodes;
  return nodes;
}

function swapChip(container, oldElement, freshElement, anchor) {
  if (oldElement && freshElement) {
    container.replaceChild(freshElement, oldElement);
    return freshElement;
  }
  if (oldElement && !freshElement) { oldElement.remove(); return null; }
  if (!oldElement && freshElement) {
    if (anchor) container.insertBefore(freshElement, anchor);
    else container.appendChild(freshElement);
    return freshElement;
  }
  return null;
}

/* The row's detail block (#1130) — everything the two-line row no longer
 * carries, in the panel that row opens: what it runs, its cadence and next
 * fire, how long it takes, how often it succeeds, what is kept, and the
 * situational facts (an externally-managed schedule, a mutex group, a
 * webhook trigger). Built from the same helpers the row used, so a value
 * reads identically wherever it lands.
 */
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

export function renderJobDetails(job) {
  const section = document.createElement('section');
  section.className = 'job-details';
  section.dataset.role = 'job-details';

  const stats = job.stats || {};
  const duration = renderDurationChip(job);
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
    detailRow('Duration', duration ? duration.textContent : null),
    detailRow('Success', successRate != null && Number.isFinite(successRate)
      ? Math.round(successRate * 100) + '% over 30 days' : null),
    detailRow('Runs', kept.length ? kept.join(' · ') : null),
    detailRow('Last run', describeLastRun(job)),
  ];

  // The situational chips, kept as chips: each is a flag, not a measurement.
  const flags = document.createElement('div');
  flags.className = 'job-detail-flags';
  if (job.elevated) {
    const elevated = document.createElement('span');
    elevated.className = 'kind-pill job-elevated-pill';
    elevated.dataset.role = 'elevated-chip';
    elevated.innerHTML = icon('lock') + ' external schedule';
    elevated.title = 'Runs through an externally managed elevated task. ' +
      'Run-now and schedule controls are unavailable here.';
    flags.appendChild(elevated);
  }
  if (job.session_less) {
    const sessionLess = document.createElement('span');
    sessionLess.className = 'kind-pill job-session-less-pill';
    sessionLess.dataset.role = 'session-less-chip';
    sessionLess.innerHTML = icon('moon') + ' logged-out';
    sessionLess.title = 'Registered to run whether the user is logged on or ' +
      'not (S4U). The Task Scheduler entry is externally managed, so schedule ' +
      'controls are unavailable here; re-register it from an elevated shell.';
    flags.appendChild(sessionLess);
  }
  if (job.mutex_group) {
    const mutex = document.createElement('span');
    mutex.className = 'kind-pill job-mutex-pill';
    const depth = Number.isFinite(job.queue_depth) ? job.queue_depth : 0;
    mutex.innerHTML = icon('link') + ' ';
    mutex.append(depth > 0 ? job.mutex_group + ' (' + depth + ')' : job.mutex_group);
    mutex.title = 'Exclusive group: ' + job.mutex_group +
      (depth > 0 ? ' — ' + depth + ' queued' : '');
    flags.appendChild(mutex);
  }
  if (job.webhook) {
    const webhook = document.createElement('span');
    webhook.className = 'kind-pill job-webhook-pill';
    webhook.innerHTML = icon('webhook') + ' ' + job.webhook.provider;
    webhook.title = 'Webhook trigger (' + job.webhook.provider + ') — POST /api/jobs/' +
      job.id + '/hook';
    flags.appendChild(webhook);
  }

  rows.forEach(function (row) { if (row) section.appendChild(row); });
  if (flags.childElementCount) section.appendChild(flags);
  return section;
}

export function patchRowNodes(nodes, job) {
  // First, before any rendering: re-point the row's action handlers at the
  // fresh job (#1007). The poll reuses this <li>, so without this the
  // buttons (and the ⋯ menu's items, which read the same holder) keep
  // acting on the object captured at row-creation time.
  if (nodes.ref) nodes.ref.job = job;
  applyStatusDot(nodes.dotEl, job);
  if (nodes.runBtnEl) setRunBtnState(nodes.runBtnEl, job);

  // The row's own order: countdown, cadence, sparkline, coverage. Each chip
  // is swapped against the next one still in the DOM, so a chip that was
  // absent comes back in the right place.
  const freshCountdown = renderCountdownChip(job);
  if (freshCountdown) freshCountdown.dataset.role = 'countdown-chip';
  nodes.countdownEl = swapChip(
    nodes.pillsEl, nodes.countdownEl, freshCountdown,
    nodes.cadenceEl || nodes.sparkEl || nodes.coverageEl
  );

  const freshCadence = renderCadenceChip(job);
  if (freshCadence) freshCadence.dataset.role = 'cadence-chip';
  nodes.cadenceEl = swapChip(
    nodes.pillsEl, nodes.cadenceEl, freshCadence, nodes.sparkEl || nodes.coverageEl
  );

  const freshSpark = renderSparkline(job);
  if (freshSpark) freshSpark.dataset.role = 'sparkline';
  nodes.sparkEl = swapChip(
    nodes.pillsEl, nodes.sparkEl, freshSpark, nodes.coverageEl
  );

  const freshCoverage = renderCoveragePill(job);
  if (freshCoverage) freshCoverage.dataset.role = 'coverage-chip';
  nodes.coverageEl = swapChip(
    nodes.pillsEl, nodes.coverageEl, freshCoverage, null
  );
}
