/* Registered-job row rendering and in-place poll updates.
 *
 * jobs.js owns fetching, sorting, dialogs, and expanded history. This module
 * owns the compact row's DOM contract so its initial render and poll-time
 * patch cannot drift apart.
 */

import { fmtAgo } from './sessions.js';
import { icon } from './_vendored/icons/icons.js';

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
  chip.innerHTML = icon('timer') + ' ' + fmtUntil(job.next_run_epoch);
  if (job.next_run) chip.title = 'Next run: ' + job.next_run;
  return chip;
}

const STATUS_META = {
  running: { class: 'up', icon: 'hourglass', spark: 'live' },
  pending: { class: '', icon: 'hourglass', spark: 'live' },
  success: { class: 'up', icon: 'circle-check', spark: 'up' },
  failed: { class: 'down', icon: 'circle-x', spark: 'down' },
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

function statusClass(job) {
  if (job.stuck) return 'stuck';
  if (job.running) return 'up';
  return job.last_run ? statusMeta(job.last_run.status).class : '';
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

function renderSparkline(job) {
  const last7 = job.stats && Array.isArray(job.stats.last7) ? job.stats.last7 : [];
  if (!last7.length) return null;
  const span = document.createElement('span');
  span.className = 'job-sparkline';
  span.setAttribute('aria-label', 'Last ' + last7.length + ' runs');
  last7.forEach(function (entry) {
    const dot = document.createElement('span');
    const status = entry && entry.status ? entry.status : '';
    const className = sparkClass(status);
    dot.className = 'job-spark-dot' + (className ? ' ' + className : '');
    dot.textContent = '●';
    dot.title = (entry && entry.run_id ? entry.run_id + ' · ' : '') +
      (status || 'unknown');
    span.appendChild(dot);
  });
  return span;
}

function describeLastRun(job) {
  const bits = [];
  if (job.last_run) {
    const ago = fmtAgo(toEpoch(job.last_run.started_at));
    const status = job.last_run.status || '?';
    const duration = formatDuration(job.last_run.duration_seconds);
    const tail = status +
      (ago ? ' · ' + ago + ' ago' : '') +
      (duration && status !== 'running' && status !== 'pending'
        ? ' · ' + duration : '');
    bits.push('last: ' + tail);
  } else {
    bits.push('never run');
  }
  if (job.stuck) bits.push('⚠️ stuck');
  const successRate = job.stats && job.stats.success_rate_30d;
  if (successRate != null && Number.isFinite(successRate)) {
    bits.push(Math.round(successRate * 100) + '% / 30d');
  }
  return bits.join(' · ');
}

export function toEpoch(isoString) {
  if (!isoString) return 0;
  const value = Date.parse(isoString);
  return Number.isFinite(value) ? Math.floor(value / 1000) : 0;
}

function setRunBtnState(button, job) {
  button.innerHTML = job.running ? icon('hourglass') : icon('play');
  button.title = job.running ? 'A run is in progress' : 'Run ' + job.name + ' now';
  button.setAttribute('aria-label', 'Run now');
  button.disabled = !!job.running;
}

export function renderJobRow(job, options) {
  const handlers = options || {};
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
  dot.className = 'health-dot ' + statusClass(job);
  dot.dataset.role = 'status-dot';
  head.appendChild(dot);
  const name = document.createElement('span');
  name.className = 'name';
  name.textContent = job.name;
  head.appendChild(name);
  info.appendChild(head);

  const pills = document.createElement('div');
  pills.className = 'job-row-pills';
  pills.dataset.role = 'job-pills';
  const kind = document.createElement('span');
  kind.className = 'kind-pill';
  kind.textContent = job.target_kind || '?';
  pills.appendChild(kind);
  if (job.schedule_chip) {
    const schedule = document.createElement('span');
    schedule.className = 'kind-pill';
    schedule.textContent = job.schedule_chip;
    pills.appendChild(schedule);
  }
  const countdown = renderCountdownChip(job);
  if (countdown) {
    countdown.dataset.role = 'countdown-chip';
    pills.appendChild(countdown);
  }
  if (job.elevated) {
    const elevated = document.createElement('span');
    elevated.className = 'kind-pill job-elevated-pill';
    elevated.dataset.role = 'elevated-chip';
    elevated.innerHTML = icon('lock') + ' externally scheduled';
    elevated.title = 'Registered by hand via schtasks /RL HIGHEST — ' +
      'this app never creates, edits, or deletes its Task Scheduler entry';
    pills.appendChild(elevated);
  }
  if (job.mutex_group) {
    const mutex = document.createElement('span');
    mutex.className = 'kind-pill job-mutex-pill';
    const depth = Number.isFinite(job.queue_depth) ? job.queue_depth : 0;
    mutex.innerHTML = icon('link') + ' ';
    mutex.append(depth > 0 ? job.mutex_group + ' (' + depth + ')' : job.mutex_group);
    mutex.title = 'Mutex group: ' + job.mutex_group +
      (depth > 0 ? ' — ' + depth + ' queued' : '');
    pills.appendChild(mutex);
  }
  if (job.webhook) {
    const webhook = document.createElement('span');
    webhook.className = 'kind-pill job-webhook-pill';
    webhook.innerHTML = icon('webhook') + ' ' + job.webhook.provider;
    webhook.title = 'Webhook trigger (' + job.webhook.provider + ') — POST /api/jobs/' +
      job.id + '/hook';
    pills.appendChild(webhook);
  }
  info.appendChild(pills);

  const load = document.createElement('div');
  load.className = 'job-row-load';
  load.dataset.role = 'job-load';
  const duration = renderDurationChip(job);
  if (duration) {
    duration.dataset.role = 'duration-chip';
    load.appendChild(duration);
  }
  const spark = renderSparkline(job);
  if (spark) {
    spark.dataset.role = 'sparkline';
    load.appendChild(spark);
  }
  info.appendChild(load);

  const meta = document.createElement('span');
  meta.className = 'meta';
  meta.dataset.role = 'meta';
  meta.textContent = describeLastRun(job);
  info.appendChild(meta);
  info.addEventListener('click', function () {
    if (handlers.onToggle) handlers.onToggle(job);
  });
  main.appendChild(info);
  li.appendChild(main);

  const actions = document.createElement('div');
  actions.className = 'row-actions session-actions';
  const run = document.createElement('button');
  run.type = 'button';
  run.className = 'icon-btn';
  run.dataset.role = 'run-btn';
  setRunBtnState(run, job);
  run.addEventListener('click', function (event) {
    event.stopPropagation();
    if (handlers.onRun) handlers.onRun(job);
  });
  actions.appendChild(run);

  const hasSchedule = job.paused ||
    (job.schedule && job.schedule.type && job.schedule.type !== 'none');
  if (hasSchedule) {
    const pause = document.createElement('button');
    pause.type = 'button';
    pause.className = 'icon-btn';
    pause.dataset.role = 'pause-btn';
    pause.innerHTML = job.paused ? icon('play') : icon('pause');
    pause.title = job.paused
      ? 'Resume schedule for ' + job.name
      : 'Pause schedule for ' + job.name;
    pause.setAttribute('aria-label', job.paused ? 'Resume' : 'Pause');
    pause.addEventListener('click', function (event) {
      event.stopPropagation();
      if (handlers.onPause) handlers.onPause(job);
    });
    actions.appendChild(pause);
  }

  if (handlers.editMode) {
    const dryRun = document.createElement('button');
    dryRun.type = 'button';
    dryRun.className = 'icon-btn';
    dryRun.innerHTML = icon('flask-conical');
    dryRun.title = 'Dry-run check ' + job.name + ' (resolve only, no spawn)';
    dryRun.setAttribute('aria-label', 'Dry-run check');
    dryRun.addEventListener('click', function (event) {
      event.stopPropagation();
      if (handlers.onRun) {
        handlers.onRun(job, { dryRun: 'check', skipDialog: true });
      }
    });
    actions.appendChild(dryRun);

    const edit = document.createElement('button');
    edit.type = 'button';
    edit.className = 'icon-btn';
    edit.innerHTML = icon('pencil');
    edit.title = 'Edit ' + job.name;
    edit.setAttribute('aria-label', 'Edit');
    edit.addEventListener('click', function (event) {
      event.stopPropagation();
      if (handlers.onEdit) handlers.onEdit(job);
    });
    actions.appendChild(edit);

    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'icon-btn danger';
    remove.innerHTML = icon('trash-2');
    remove.title = 'Remove ' + job.name;
    remove.setAttribute('aria-label', 'Remove');
    remove.addEventListener('click', function (event) {
      event.stopPropagation();
      if (handlers.onRemove) handlers.onRemove(job);
    });
    actions.appendChild(remove);
  }
  li.appendChild(actions);

  const nodes = {
    li: li,
    dotEl: dot,
    nameEl: name,
    pillsEl: pills,
    loadEl: load,
    metaEl: meta,
    runBtnEl: run,
    countdownEl: countdown,
    durationEl: duration,
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

export function patchRowNodes(nodes, job) {
  nodes.dotEl.className = 'health-dot ' + statusClass(job);
  nodes.metaEl.textContent = describeLastRun(job);
  setRunBtnState(nodes.runBtnEl, job);

  const freshCountdown = renderCountdownChip(job);
  if (freshCountdown) freshCountdown.dataset.role = 'countdown-chip';
  const mutex = nodes.pillsEl.querySelector('.job-mutex-pill');
  nodes.countdownEl = swapChip(
    nodes.pillsEl, nodes.countdownEl, freshCountdown, mutex
  );

  const freshDuration = renderDurationChip(job);
  if (freshDuration) freshDuration.dataset.role = 'duration-chip';
  nodes.durationEl = swapChip(
    nodes.loadEl, nodes.durationEl, freshDuration, nodes.sparkEl
  );

  const freshSpark = renderSparkline(job);
  if (freshSpark) freshSpark.dataset.role = 'sparkline';
  nodes.sparkEl = swapChip(nodes.loadEl, nodes.sparkEl, freshSpark, null);
}
