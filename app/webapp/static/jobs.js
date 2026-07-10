/* Jobs tab: list registered jobs, fire run-now, view history (issue #47).
 *
 * Expanded-panel model — runs list + one selected run's output:
 *   - Tap a job row → panel opens, defaults to the newest run selected.
 *   - The runs list (max 5) is always shown; tap a run to switch the log.
 *   - Polling tick (3 s) only fetches when the panel is open. It always
 *     refreshes the runs list (cheap — no output bytes), and only
 *     re-fetches the SELECTED run's output if that run is still
 *     running/pending. A finalized run is a static log: no flicker.
 *   - Scroll position is preserved on update; auto-follow-bottom kicks
 *     in only when the user was already at the bottom (classic tail -f).
 *
 * This is the residual module after the audit #315 split: list render +
 * poller + in-place patch + the expanded run-history panel. The edit/add
 * dialog and the run-now dialog live in jobs-dialog.js; the foldable
 * Schedule agenda panel lives in jobs-agenda.js. The two are wired in via
 * wireJobDialogs()/wireJobsAgenda() from wireJobs() below.
 */

import { els, state } from './state.js';
import { apiFailToast, AuthRequiredError, jsonApi, logPollFailure, toast } from './api.js';
import { fmtAgo } from './sessions.js';
import { openJobDialog, openRunDialog, removeJob, wireJobDialogs } from './jobs-dialog.js';
import { wireJobsAgenda } from './jobs-agenda.js';
import { icon } from './_vendored/icons/icons.js';

// --------------------------------------------------------------- render

export function renderJobs() {
  const host = els.jobsList;
  host.innerHTML = '';
  els.jobsEmpty.hidden = state.jobs.length !== 0;
  if (els.jobsAddBtn) els.jobsAddBtn.hidden = !state.editMode;
  syncSortBtn();

  sortedJobs().forEach(function (job) {
    host.appendChild(renderJobRow(job).li);
    if (state.expandedJob === job.id) {
      host.appendChild(renderHistoryLi(job));
    }
  });
}

// ------------------------------------------------------------ sort + order
//
// Two orderings (issue #229). 'next' is the default and the point of the
// feature: ascending by the server-computed next_run_epoch, so imminent
// daily jobs float above weekly ones and the eye lands on "what fires
// next". Manual-only / paused jobs (no next fire) sink to the bottom,
// tie-broken by name so the order is stable poll-to-poll. 'name' keeps the
// classic A–Z.

function sortedJobs() {
  const jobs = (state.jobs || []).slice();
  if (state.jobsSort === 'name') {
    jobs.sort(byName);
    return jobs;
  }
  jobs.sort(function (a, b) {
    const ae = Number.isFinite(a.next_run_epoch) ? a.next_run_epoch : Infinity;
    const be = Number.isFinite(b.next_run_epoch) ? b.next_run_epoch : Infinity;
    if (ae !== be) return ae - be;
    return byName(a, b);
  });
  return jobs;
}

function byName(a, b) {
  return (a.name || '').toLowerCase().localeCompare((b.name || '').toLowerCase());
}

function syncSortBtn() {
  const btn = els.jobsSortBtn;
  if (!btn) return;
  if (state.jobsSort === 'name') {
    btn.innerHTML = icon('arrow-down-up') + ' A–Z';
    btn.title = 'Sorted A–Z — tap to sort by next run';
  } else {
    btn.innerHTML = icon('timer') + ' Next run';
    btn.title = 'Sorted by next run — tap to sort A–Z';
  }
}

function toggleSort() {
  state.jobsSort = state.jobsSort === 'next' ? 'name' : 'next';
  localStorage.setItem('launcher.jobsSort', state.jobsSort);
  renderJobs();
}

// Forward-looking countdown — the past-tense sibling of fmtAgo (sessions.js).
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

// Tapping an agenda row (jobs-agenda.js) jumps to that job in the
// Registered-jobs list and expands it — the agenda is a lens, not a
// second control surface.
export function revealJob(jobId) {
  const job = state.jobs.find(function (j) { return j.id === jobId; });
  if (!job) return;
  if (state.expandedJob !== jobId) {
    state.expandedJob = jobId;
    state.selectedRun = null;
    renderJobs();
    refreshExpandedContent(jobId, { fetchOutput: true }).catch(function () {});
  }
  const row = els.jobsList.querySelector(
    "li.app-item[data-id='" + cssEscape(jobId) + "']"
  );
  if (row && row.scrollIntoView) {
    row.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
}

// A row's DOM handle — the fields patchRowsInPlace needs to mutate on a
// poll tick, stashed on the <li> itself at build time. Single source of
// truth for "what this row looks like": renderJobRow builds it once,
// patchRowNodes only ever mutates through it — it never re-derives the
// row's structure independently (issue #315; this bit sparkClass once
// already when the two paths silently drifted apart).
function renderJobRow(job) {
  const li = document.createElement('li');
  li.className = 'app-item job-item';
  li.dataset.id = job.id;

  const main = document.createElement('div');
  main.className = 'app-main';

  const info = document.createElement('button');
  info.type = 'button';
  info.className = 'launch-btn session-open';

  // Row 1: dot + name. The job name is short and belongs above the
  // chips so a narrow phone never forces it to letter-stack vertically.
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

  // Row 2: type + schedule. Always its own row — pinned ordering so
  // the eye lands in the same place across every job.
  const pills = document.createElement('div');
  pills.className = 'job-row-pills';
  pills.dataset.role = 'job-pills';

  const pill = document.createElement('span');
  pill.className = 'kind-pill';
  pill.textContent = job.target_kind || '?';
  pills.appendChild(pill);

  if (job.schedule_chip) {
    const chip = document.createElement('span');
    chip.className = 'kind-pill';
    chip.textContent = job.schedule_chip;
    pills.appendChild(chip);
  }

  // Relative countdown to the next computed fire (issue #229). Sits right
  // after the cadence chip so "every Friday" reads "every Friday · in 2d".
  const countdown = renderCountdownChip(job);
  if (countdown) {
    countdown.dataset.role = 'countdown-chip';
    pills.appendChild(countdown);
  }

  if (job.elevated) {
    // Issue #352 — an elevated job's real Task Scheduler entry is
    // externally-managed (the launcher's own sync always no-ops for it),
    // so mark it clearly rather than let it look like every other job.
    const ext = document.createElement('span');
    ext.className = 'kind-pill job-elevated-pill';
    ext.dataset.role = 'elevated-chip';
    ext.innerHTML = icon('lock') + ' externally scheduled';
    ext.title = 'Registered by hand via schtasks /RL HIGHEST — ' +
      'this app never creates, edits, or deletes its Task Scheduler entry';
    pills.appendChild(ext);
  }

  if (job.mutex_group) {
    const mg = document.createElement('span');
    mg.className = 'kind-pill job-mutex-pill';
    const depth = Number.isFinite(job.queue_depth) ? job.queue_depth : 0;
    mg.innerHTML = icon('link') + ' ';
    mg.append(depth > 0 ? job.mutex_group + ' (' + depth + ')'
                               : job.mutex_group);
    mg.title = 'Mutex group: ' + job.mutex_group +
      (depth > 0 ? ' — ' + depth + ' queued' : '');
    pills.appendChild(mg);
  }

  if (job.webhook) {
    // Webhook-target job (issue #73) — fireable by an external service
    // (GitHub/Stripe/generic) over POST /api/jobs/<id>/hook, gated by its
    // own signature rather than the app's bearer token.
    const wh = document.createElement('span');
    wh.className = 'kind-pill job-webhook-pill';
    wh.innerHTML = icon('webhook') + ' ' + job.webhook.provider;
    wh.title = 'Webhook trigger (' + job.webhook.provider + ') — ' +
      'POST /api/jobs/' + job.id + '/hook';
    pills.appendChild(wh);
  }

  info.appendChild(pills);

  // Row 3: load (duration percentiles) + sparkline. Same idea — its own
  // row regardless of available width, so this is always *where* you
  // look for "how heavy is this job + how have the last few runs gone".
  const load = document.createElement('div');
  load.className = 'job-row-load';
  load.dataset.role = 'job-load';

  const durationChip = renderDurationChip(job);
  if (durationChip) {
    durationChip.dataset.role = 'duration-chip';
    load.appendChild(durationChip);
  }

  const sparkline = renderSparkline(job);
  if (sparkline) {
    sparkline.dataset.role = 'sparkline';
    load.appendChild(sparkline);
  }

  info.appendChild(load);

  const meta = document.createElement('span');
  meta.className = 'meta';
  meta.dataset.role = 'meta';
  meta.textContent = describeLastRun(job);
  info.appendChild(meta);

  info.addEventListener('click', function () { toggleExpanded(job); });
  main.appendChild(info);
  li.appendChild(main);

  const actions = document.createElement('div');
  actions.className = 'row-actions session-actions';

  const runBtn = document.createElement('button');
  runBtn.type = 'button';
  runBtn.className = 'icon-btn';
  runBtn.dataset.role = 'run-btn';
  setRunBtnState(runBtn, job);
  runBtn.addEventListener('click', function (ev) { ev.stopPropagation(); runJobNow(job); });
  actions.appendChild(runBtn);

  // Pause / resume — only meaningful for scheduled jobs. A job whose
  // current AND parked schedule are both none has nothing to toggle.
  const hasSchedule = job.paused
    ? true
    : (job.schedule && job.schedule.type && job.schedule.type !== 'none');
  if (hasSchedule) {
    const pauseBtn = document.createElement('button');
    pauseBtn.type = 'button';
    pauseBtn.className = 'icon-btn';
    pauseBtn.dataset.role = 'pause-btn';
    pauseBtn.innerHTML = job.paused ? icon('play') : icon('pause');
    pauseBtn.title = job.paused
      ? 'Resume schedule for ' + job.name
      : 'Pause schedule for ' + job.name;
    pauseBtn.setAttribute('aria-label', job.paused ? 'Resume' : 'Pause');
    pauseBtn.addEventListener('click', function (ev) {
      ev.stopPropagation();
      togglePause(job);
    });
    actions.appendChild(pauseBtn);
  }

  if (state.editMode) {
    // Dry-run check (issue #69) — "would this even start?" without
    // spawning the target. Authoring/test control, so edit-mode only.
    const dryBtn = document.createElement('button');
    dryBtn.type = 'button';
    dryBtn.className = 'icon-btn';
    dryBtn.innerHTML = icon('flask-conical');
    dryBtn.title = 'Dry-run check ' + job.name + ' (resolve only, no spawn)';
    dryBtn.setAttribute('aria-label', 'Dry-run check');
    dryBtn.addEventListener('click', function (ev) {
      ev.stopPropagation();
      runJobNow(job, { dryRun: 'check', skipDialog: true });
    });
    actions.appendChild(dryBtn);

    const editBtn = document.createElement('button');
    editBtn.type = 'button';
    editBtn.className = 'icon-btn';
    editBtn.innerHTML = icon('pencil');
    editBtn.title = 'Edit ' + job.name;
    editBtn.setAttribute('aria-label', 'Edit');
    editBtn.addEventListener('click', function (ev) { ev.stopPropagation(); openJobDialog(job); });
    actions.appendChild(editBtn);

    const removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'icon-btn danger';
    removeBtn.innerHTML = icon('trash-2');
    removeBtn.title = 'Remove ' + job.name;
    removeBtn.setAttribute('aria-label', 'Remove');
    removeBtn.addEventListener('click', function (ev) { ev.stopPropagation(); removeJob(job); });
    actions.appendChild(removeBtn);
  }

  li.appendChild(actions);

  const nodes = {
    li: li,
    dotEl: dot,
    nameEl: name,
    pillsEl: pills,
    loadEl: load,
    metaEl: meta,
    runBtnEl: runBtn,
    countdownEl: countdown,
    durationEl: durationChip,
    sparkEl: sparkline,
  };
  li._rowNodes = nodes;
  return nodes;
}

function setRunBtnState(btn, job) {
  btn.innerHTML = job.running ? icon('hourglass') : icon('play');
  btn.title = job.running ? 'A run is in progress' : ('Run ' + job.name + ' now');
  btn.setAttribute('aria-label', 'Run now');
  btn.disabled = !!job.running;
}

// Single source of truth for every status → {dot class, run icon, spark
// class} mapping (issue #315) — these used to be three independent
// lookups that quietly diverged (e.g. a status landing a class in one
// and dropping out of another). A new status is now one entry here.
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

function statusIcon(status) {
  return statusMeta(status).icon;
}

function sparkClass(status) {
  return statusMeta(status).spark;
}

// The job-level health dot: stuck/running are job flags (not part of any
// single run's status), so they're checked first; otherwise the dot
// reflects the last run's status via the shared table above.
function statusClass(job) {
  if (job.stuck) return 'stuck';
  if (job.running) return 'up';
  return job.last_run ? statusMeta(job.last_run.status).class : '';
}

function formatDuration(seconds) {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return null;
  if (seconds < 10) return seconds.toFixed(1) + 's';
  if (seconds < 60) return Math.round(seconds) + 's';
  if (seconds < 3600) {
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds - m * 60);
    return m + 'm' + (s ? ' ' + s + 's' : '');
  }
  const h = Math.floor(seconds / 3600);
  const m = Math.round((seconds - h * 3600) / 60);
  return h + 'h' + (m ? ' ' + m + 'm' : '');
}

function formatBytes(bytes) {
  if (bytes == null || !Number.isFinite(bytes) || bytes < 0) return null;
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = bytes;
  let idx = 0;
  while (value >= 1024 && idx < units.length - 1) {
    value /= 1024;
    idx += 1;
  }
  const fixed = value >= 10 || idx === 0 ? value.toFixed(0) : value.toFixed(1);
  return fixed + ' ' + units[idx];
}

function renderDurationChip(job) {
  const stats = job.stats || {};
  const p50 = formatDuration(stats.p50);
  const p95 = formatDuration(stats.p95);
  if (!p50 && !p95) return null;
  const chip = document.createElement('span');
  chip.className = 'kind-pill job-duration-chip';
  if (p50 && p95) {
    chip.textContent = 'p50 ' + p50 + ' · p95 ' + p95;
  } else {
    chip.textContent = 'p50 ' + (p50 || p95);
  }
  chip.title = 'Duration percentiles across the last ' +
    (stats.completed_count || 0) + ' completed run(s)';
  return chip;
}

function renderSparkline(job) {
  const last7 = (job.stats && Array.isArray(job.stats.last7)) ? job.stats.last7 : [];
  if (!last7.length) return null;
  const span = document.createElement('span');
  span.className = 'job-sparkline';
  span.setAttribute('aria-label', 'Last ' + last7.length + ' runs');
  last7.forEach(function (entry) {
    const dot = document.createElement('span');
    const status = (entry && entry.status) || '';
    const cls = sparkClass(status);
    dot.className = 'job-spark-dot' + (cls ? ' ' + cls : '');
    dot.textContent = '●';
    dot.title = (entry && entry.run_id ? entry.run_id + ' · ' : '') + (status || 'unknown');
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
      (duration && status !== 'running' && status !== 'pending' ? ' · ' + duration : '');
    bits.push('last: ' + tail);
  } else {
    bits.push('never run');
  }
  if (job.stuck) bits.push('⚠️ stuck');
  const sr = job.stats && job.stats.success_rate_30d;
  if (sr != null && Number.isFinite(sr)) bits.push(Math.round(sr * 100) + '% / 30d');
  // "next" now lives in the countdown chip on the pills row (issue #229),
  // so it's intentionally not repeated here.
  return bits.join(' · ');
}

function toEpoch(isoStr) {
  if (!isoStr) return 0;
  const t = Date.parse(isoStr);
  return Number.isFinite(t) ? Math.floor(t / 1000) : 0;
}

// --------------------------------------------------- expanded history <li>

function renderHistoryLi(job) {
  const li = document.createElement('li');
  li.className = 'jobs-history-li';
  li.dataset.historyFor = job.id;

  const bar = document.createElement('div');
  bar.className = 'jobs-history-bar';
  const title = document.createElement('span');
  title.className = 'jobs-history-title';
  title.textContent = 'Recent runs · ' + job.name;
  bar.appendChild(title);
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'jobs-history-close';
  close.innerHTML = icon('x') + ' Close';
  close.addEventListener('click', function (ev) { ev.stopPropagation(); collapseExpanded(); });
  bar.appendChild(close);
  li.appendChild(bar);

  const body = document.createElement('div');
  body.className = 'jobs-history-body';
  body.dataset.role = 'history-body';

  const runsList = document.createElement('ul');
  runsList.className = 'jobs-runs-list';
  runsList.dataset.role = 'runs-list';
  body.appendChild(runsList);

  const label = document.createElement('div');
  label.className = 'jobs-output-label';
  label.dataset.role = 'output-label';
  label.textContent = 'Loading…';
  body.appendChild(label);

  const tail = document.createElement('pre');
  tail.className = 'jobs-output-tail';
  tail.dataset.role = 'output-tail';
  tail.textContent = '';
  tail.title = 'Tap to copy log';
  tail.setAttribute('aria-label', 'Tap to copy log');
  tail.addEventListener('click', function () { copyOutputTail(tail); });
  body.appendChild(tail);

  // Raw webhook payload (issue #73) — collapsed by default, only shown
  // (and populated) for a run that was actually webhook-triggered.
  const webhookDetails = document.createElement('details');
  webhookDetails.className = 'jobs-webhook-payload';
  webhookDetails.dataset.role = 'webhook-payload-details';
  webhookDetails.hidden = true;
  const webhookSummary = document.createElement('summary');
  webhookSummary.innerHTML = icon('webhook') + ' Webhook payload';
  webhookDetails.appendChild(webhookSummary);
  const webhookPre = document.createElement('pre');
  webhookPre.className = 'jobs-webhook-payload-body';
  webhookPre.dataset.role = 'webhook-payload-body';
  webhookDetails.appendChild(webhookPre);
  body.appendChild(webhookDetails);

  li.appendChild(body);
  return li;
}

function panelEl(jobId) {
  return els.jobsList.querySelector(
    'li.jobs-history-li[data-history-for="' + cssEscape(jobId) + '"]'
  );
}

function cssEscape(s) {
  if (window.CSS && typeof window.CSS.escape === 'function') return window.CSS.escape(s);
  return String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
}

function redrawRunsList(jobId, runs) {
  const panel = panelEl(jobId);
  if (!panel) return;
  const list = panel.querySelector('[data-role="runs-list"]');
  if (!list) return;
  list.innerHTML = '';
  if (!runs.length) {
    const empty = document.createElement('li');
    empty.className = 'muted small';
    empty.textContent = 'No runs yet.';
    list.appendChild(empty);
    return;
  }
  // Look up the job's current params declaration to spot keys that have
  // since been removed (used by the Re-run pre-fill flow, issue #67).
  const job = state.jobs.find(function (j) { return j.id === jobId; });
  const declaredNames = new Set(((job && job.params) || []).map(function (p) { return p.name; }));

  // Render the whole server response (already capped at MAX_RUNS_PER_JOB) —
  // a hardcoded 5-row slice hid older runs on high-cadence jobs, making their
  // logs unreachable (#316).
  runs.forEach(function (r) {
    const li = document.createElement('li');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'jobs-run-btn';
    if (state.selectedRun && state.selectedRun.jobId === jobId
        && state.selectedRun.runId === r.run_id) {
      btn.classList.add('selected');
    }
    const iconEl = document.createElement('span');
    iconEl.className = 'jobs-run-icon';
    iconEl.innerHTML = icon(statusIcon(r.status));
    btn.appendChild(iconEl);
    const meta = document.createElement('span');
    meta.className = 'jobs-run-meta';
    const ago = fmtAgo(toEpoch(r.started_at));
    const exitText = (r.exit_code === undefined || r.exit_code === null)
      ? '' : ' · exit ' + r.exit_code;
    const paramsChip = formatRunParams(r.params);
    meta.textContent = (r.status || '?') +
      (ago ? ' · ' + ago + ' ago' : '') +
      ' · ' + (r.trigger || '?') + exitText +
      (r.dry_run ? ' · 🧪 dry' : '') +
      (paramsChip ? ' · ' + paramsChip : '');
    btn.appendChild(meta);
    btn.addEventListener('click', function () { selectRun(jobId, r.run_id); });
    li.appendChild(btn);

    // Re-run button (issue #67) — only meaningful when the job declares
    // params now. Opens the run dialog pre-filled with this run's values.
    if (job && declaredNames.size && r.params && typeof r.params === 'object') {
      const rerun = document.createElement('button');
      rerun.type = 'button';
      rerun.className = 'icon-btn';
      rerun.textContent = '↻';
      rerun.title = 'Re-run with these parameters';
      rerun.setAttribute('aria-label', 'Re-run with these parameters');
      rerun.addEventListener('click', function (ev) {
        ev.stopPropagation();
        const prefill = {};
        const stale = [];
        Object.keys(r.params).forEach(function (k) {
          if (declaredNames.has(k)) prefill[k] = r.params[k];
          else stale.push(k);
        });
        runJobNow(job, { prefill: prefill, staleKeys: stale });
      });
      li.appendChild(rerun);
    }

    list.appendChild(li);
  });
}

function formatRunParams(params) {
  if (!params || typeof params !== 'object') return '';
  const keys = Object.keys(params);
  if (!keys.length) return '';
  return keys.map(function (k) {
    const v = params[k];
    return k + '=' + (typeof v === 'string' ? v : JSON.stringify(v));
  }).join(' ');
}

function writeOutput(jobId, runId, text, status, extras) {
  const panel = panelEl(jobId);
  if (!panel) return;
  const label = panel.querySelector('[data-role="output-label"]');
  const tail = panel.querySelector('[data-role="output-tail"]');
  if (!tail) return;
  if (label) {
    const bits = ['Output · ' + runId];
    if (status) bits.push(status + (status === 'running' || status === 'pending' ? ' (live)' : ''));
    const cpu = extras && Number.isFinite(extras.cpu_seconds)
      ? Math.round(extras.cpu_seconds) + ' s CPU' : null;
    const rss = extras && Number.isFinite(extras.peak_rss_bytes)
      ? 'peak ' + formatBytes(extras.peak_rss_bytes) : null;
    const dur = extras && Number.isFinite(extras.duration_seconds)
      ? formatDuration(extras.duration_seconds) : null;
    if (dur && status !== 'running' && status !== 'pending') bits.push(dur);
    if (cpu) bits.push(cpu);
    if (rss) bits.push(rss);
    label.textContent = bits.join(' · ');
  }
  renderKillButton(jobId, runId, status, extras);
  const isSameRun = tail.dataset.runId === runId;
  const wasAtBottom = !isSameRun ||
    (tail.scrollTop + tail.clientHeight >= tail.scrollHeight - 4);
  const prevScrollTop = tail.scrollTop;
  tail.dataset.runId = runId;
  tail.textContent = text || '(no output)';
  // Classic tail -f: jump to bottom on first paint of a run or while
  // the user is already pinned to the bottom. If they scrolled up to
  // read older lines, leave them exactly where they were.
  if (wasAtBottom) {
    tail.scrollTop = tail.scrollHeight;
  } else {
    tail.scrollTop = prevScrollTop;
  }

  const webhookDetails = panel.querySelector('[data-role="webhook-payload-details"]');
  const webhookPre = panel.querySelector('[data-role="webhook-payload-body"]');
  if (webhookDetails && webhookPre) {
    const wh = extras && extras.webhook_payload;
    webhookDetails.hidden = !wh;
    webhookPre.textContent = wh ? JSON.stringify(wh, null, 2) : '';
  }
}

// Tap-to-copy (issue #97). One tap on the run's output pane drops the whole
// log on the clipboard so it can be pasted into an error report / chat. We
// read textContent live, so the same handler always copies the currently
// selected run. Guards: a non-empty manual selection inside the pane is left
// alone (the user is copying a sub-range by hand), and the empty placeholder
// is a no-op — there's nothing to copy.
async function copyOutputTail(tail) {
  const selection = window.getSelection && window.getSelection();
  if (selection && String(selection).length &&
      tail.contains(selection.anchorNode)) {
    return;
  }
  const text = tail.textContent || '';
  if (!text || text === '(no output)') return;
  try {
    await navigator.clipboard.writeText(text);
    toast('📋 Copied log', 'good');
  } catch (exc) {
    toast('Clipboard unavailable — copy manually', 'error');
  }
}

// ---------------------------------------------------------- interactions

function collapseExpanded() {
  state.expandedJob = null;
  state.selectedRun = null;
  renderJobs();
  fetchJobs().catch(function () {});
}

async function toggleExpanded(job) {
  if (state.expandedJob === job.id) {
    collapseExpanded();
    return;
  }
  state.expandedJob = job.id;
  state.selectedRun = null;
  renderJobs();
  await refreshExpandedContent(job.id, { fetchOutput: true });
}

function selectRun(jobId, runId) {
  state.selectedRun = { jobId: jobId, runId: runId };
  // Re-render the runs list so the highlight moves immediately, then
  // load the chosen run's output (always — even if static).
  redrawRunsList(jobId, state.jobRuns[jobId] || []);
  refreshOutputForRun(jobId, runId).catch(function () {});
}

async function refreshExpandedContent(jobId, opts) {
  opts = opts || {};
  // Always-cheap fetch: the runs list (no output bytes).
  let runs = [];
  try {
    const body = await jsonApi('/api/jobs/' + encodeURIComponent(jobId) + '/runs');
    runs = body.runs || [];
    state.jobRuns[jobId] = runs;
  } catch (exc) {
    if (exc instanceof AuthRequiredError) return;
  }

  // Default selection on first paint: newest run.
  if (!state.selectedRun || state.selectedRun.jobId !== jobId) {
    if (runs.length) state.selectedRun = { jobId: jobId, runId: runs[0].run_id };
  }
  // If selection no longer exists (pruned), fall back to newest — but only on
  // an explicit/initial refresh, never on the 2 s poll. Snapping the poll back
  // to the newest run yanked the user off an older run's log they were reading
  // (#316); on the poll we leave a vanished selection alone.
  if (!opts.poll && state.selectedRun && state.selectedRun.jobId === jobId &&
      !runs.find(function (r) { return r.run_id === state.selectedRun.runId; })) {
    state.selectedRun = runs.length ? { jobId: jobId, runId: runs[0].run_id } : null;
  }

  redrawRunsList(jobId, runs);

  if (!state.selectedRun || state.selectedRun.jobId !== jobId) return;
  const selectedRunId = state.selectedRun.runId;
  const selected = runs.find(function (r) { return r.run_id === selectedRunId; });
  const panel = panelEl(jobId);
  const tail = panel ? panel.querySelector('[data-role="output-tail"]') : null;
  const isLive = selected && (selected.status === 'running' || selected.status === 'pending');
  const isFirstPaint = !tail || tail.dataset.runId !== selectedRunId;
  // Skip the output fetch when the selected run is final AND we've
  // already painted it — a static log doesn't need re-polling, which
  // is the difference between "I can read this" and "it keeps jumping".
  if (opts.fetchOutput || isLive || isFirstPaint) {
    await refreshOutputForRun(jobId, selectedRunId);
  }
}

async function refreshOutputForRun(jobId, runId) {
  let detail;
  try {
    detail = await jsonApi(
      '/api/jobs/' + encodeURIComponent(jobId) + '/runs/' + encodeURIComponent(runId)
    );
  } catch (exc) {
    return;
  }
  const record = detail.run || {};
  writeOutput(jobId, runId, record.output_tail || '', record.status, {
    cpu_seconds: record.cpu_seconds,
    peak_rss_bytes: record.peak_rss_bytes,
    duration_seconds: record.duration_seconds,
    webhook_payload: record.webhook_payload,
  });
}

function renderKillButton(jobId, runId, status, extras) {
  const panel = panelEl(jobId);
  if (!panel) return;
  const body = panel.querySelector('[data-role="history-body"]');
  if (!body) return;
  let killBtn = body.querySelector('[data-role="kill-btn"]');
  const job = state.jobs.find(function (j) { return j.id === jobId; });
  const isLive = status === 'running' || status === 'pending';
  // Only show kill on the *latest* run of a stuck job — older runs
  // can't be running (status would already be final by definition).
  const showKill = !!(job && job.stuck && isLive);
  if (!showKill) {
    if (killBtn) killBtn.remove();
    return;
  }
  if (!killBtn) {
    killBtn = document.createElement('button');
    killBtn.type = 'button';
    killBtn.className = 'icon-btn danger jobs-kill-btn';
    killBtn.dataset.role = 'kill-btn';
    killBtn.innerHTML = icon('octagon-x') + ' Kill stuck run';
    killBtn.addEventListener('click', function () { killRun(jobId, runId); });
    body.insertBefore(killBtn, body.querySelector('[data-role="output-label"]'));
  } else {
    // Re-bind in case runId has changed since the last render.
    killBtn.onclick = function () { killRun(jobId, runId); };
  }
}

async function killRun(jobId, runId) {
  if (!confirm('Kill the running process tree for this run?')) return;
  try {
    await jsonApi(
      '/api/jobs/' + encodeURIComponent(jobId) + '/runs/' + encodeURIComponent(runId) + '/kill',
      { method: 'POST' }
    );
    toast('🛑 Kill signal sent.', 'good');
    await refreshExpandedContent(jobId, { fetchOutput: true });
    await fetchJobs();
  } catch (exc) {
    apiFailToast('Kill failed', exc);
  }
}

async function togglePause(job) {
  const action = job.paused ? 'resume' : 'pause';
  try {
    await jsonApi(
      '/api/jobs/' + encodeURIComponent(job.id) + '/' + action,
      { method: 'POST' }
    );
    toast(job.paused ? '▶ Resumed ' + job.name : '⏸ Paused ' + job.name, 'good');
    await fetchJobs();
  } catch (exc) {
    apiFailToast(action.charAt(0).toUpperCase() + action.slice(1) + ' failed', exc);
  }
}

export async function runJobNow(job, options) {
  // Issue #67: jobs with declared params open a small typed form so the
  // user supplies values. Parameter-less jobs keep their one-tap fire.
  const params = (job && job.params) || [];
  const opts = options || {};
  if (params.length > 0 && !opts.skipDialog) {
    openRunDialog(job, opts.prefill || null, opts.staleKeys || null);
    return;
  }
  // Body carries params (issue #67) and/or a dry_run mode (issue #69:
  // "check" = resolve only, "execute" = spawn with JOB_DRY_RUN=1).
  const body = {};
  if (opts.params) body.params = opts.params;
  if (opts.dryRun) body.dry_run = opts.dryRun;
  const hasBody = Object.keys(body).length > 0;
  // Confirm-on-fire (issue #69). A flagged job needs explicit
  // confirmation before a real fire; a dry-run "check" is exempt
  // (no side effects). The ?confirmed=1 keeps the server gate honest.
  const isCheck = opts.dryRun === 'check';
  const needConfirm = !!(job && job.confirm) && !isCheck;
  if (needConfirm &&
      !confirm('Run ' + job.name + '? This job requires confirmation before running.')) {
    return;
  }
  try {
    const res = await jsonApi(
      '/api/jobs/' + encodeURIComponent(job.id) + '/run' +
        (needConfirm ? '?confirmed=1' : ''),
      {
        method: 'POST',
        headers: hasBody ? { 'Content-Type': 'application/json' } : undefined,
        body: hasBody ? JSON.stringify(body) : undefined,
      });
    if (res && res.dry_run) {
      if (res.status === 'dry_run_failed') {
        toast('🧪 Dry-run check failed for ' + job.name + ' — see history.', 'error');
      } else if (res.status === 'dry_run_success') {
        toast('🧪 Dry-run check passed for ' + job.name + '.', 'good');
      } else {
        toast('🧪 Dry-run started for ' + job.name + '.', 'good');
        job.running = true;
      }
    } else if (res && res.status === 'queued') {
      const blocker = res.mutex_blocked_by ? ' (behind ' + res.mutex_blocked_by + ')' : '';
      toast('🪢 Queued ' + job.name + blocker + '.', 'good');
    } else {
      toast('🚀 Started ' + job.name + '.', 'good');
      job.running = true;
    }
    renderJobs();
    // Brief delayed nudge so the new run shows up promptly without
    // waiting for the next poll tick.
    setTimeout(function () { fetchJobs().catch(function () {}); }, 1500);
  } catch (exc) {
    if (exc && exc.status === 429) {
      // FastAPI wraps our raise HTTPException(detail={...}) in its own
      // {"detail": ...} envelope, so the cooldown payload is nested one
      // level deeper than a typical error body (#403).
      const payload = exc.body && exc.body.detail;
      const detail = payload && payload.detail;
      const remaining = payload && Number(payload.retry_after_seconds);
      const cd = payload && Number(payload.cooldown_seconds);
      if (detail === 'cooldown' && Number.isFinite(remaining)) {
        const suffix = (Number.isFinite(cd) && cd > 0) ? ' (cooldown ' + cd + 's)' : '';
        toast('⏭ Skipped — cooled down for ' + remaining + ' more s' + suffix + '.');
        return;
      }
    }
    apiFailToast('Run failed', exc);
  }
}

// ------------------------------------------------------ in-place row patch
//
// Swap `oldEl` for `freshEl` inside `container`, inserting before `anchor`
// when there is no existing element to replace. The one place that knows
// how to graft a chip into a row — countdown/duration/sparkline all reuse
// it instead of three hand-rolled, independently-drifting copies (#315).
function swapChip(container, oldEl, freshEl, anchor) {
  if (oldEl && freshEl) { container.replaceChild(freshEl, oldEl); return freshEl; }
  if (oldEl && !freshEl) { oldEl.remove(); return null; }
  if (!oldEl && freshEl) {
    if (anchor) container.insertBefore(freshEl, anchor); else container.appendChild(freshEl);
    return freshEl;
  }
  return null;
}

// Mutate a row via its stashed RowNodes handle — never rebuilds the row's
// structure independently of renderJobRow (issue #315).
function patchRowNodes(nodes, job) {
  nodes.dotEl.className = 'health-dot ' + statusClass(job);
  nodes.metaEl.textContent = describeLastRun(job);
  setRunBtnState(nodes.runBtnEl, job);

  // The countdown ticks down between polls — swap it in place so the
  // "in 3h" stays honest without re-rendering the whole row.
  const freshCountdown = renderCountdownChip(job);
  if (freshCountdown) freshCountdown.dataset.role = 'countdown-chip';
  const mutexEl = nodes.pillsEl.querySelector('.job-mutex-pill');
  nodes.countdownEl = swapChip(nodes.pillsEl, nodes.countdownEl, freshCountdown, mutexEl);

  // Sparkline + duration chip can change between polls (new run finished,
  // stats recomputed) — swap them in place so the rest of the row doesn't
  // flash. Both live on the "load" sub-row; duration is anchored ahead of
  // the (still-old, not-yet-swapped) sparkline element.
  const freshDuration = renderDurationChip(job);
  if (freshDuration) freshDuration.dataset.role = 'duration-chip';
  nodes.durationEl = swapChip(nodes.loadEl, nodes.durationEl, freshDuration, nodes.sparkEl);

  const freshSpark = renderSparkline(job);
  if (freshSpark) freshSpark.dataset.role = 'sparkline';
  nodes.sparkEl = swapChip(nodes.loadEl, nodes.sparkEl, freshSpark, null);
}

function patchRowsInPlace() {
  const host = els.jobsList;
  const existing = Array.from(host.querySelectorAll('li.app-item[data-id]'));
  // Compare against the *sorted* order — the DOM is rendered sorted, so a
  // length OR order change (a job's next_run_epoch crossing another's
  // between polls) means the in-place patch can't keep rows aligned; fall
  // back to a full re-render in that case.
  const ordered = sortedJobs();
  if (existing.length !== ordered.length) { renderJobs(); return; }
  for (let i = 0; i < existing.length; i++) {
    const li = existing[i];
    const job = ordered[i];
    if (!job || li.dataset.id !== job.id) { renderJobs(); return; }
    const nodes = li._rowNodes;
    if (!nodes) { renderJobs(); return; }
    patchRowNodes(nodes, job);
  }
}

// ------------------------------------------------------------ fetch + wire

export async function fetchJobs() {
  if (state.tab !== 'jobs') return;
  // While a row is expanded, polling refreshes that one panel's content
  // in place — touching the row list would tear down the user's view.
  if (state.expandedJob) {
    // Poll path: refresh content in place without stealing the user's run
    // selection (see the `opts.poll` guard in refreshExpandedContent, #316).
    await refreshExpandedContent(state.expandedJob, { poll: true });
    return;
  }
  try {
    const body = await jsonApi('/api/jobs');
    state.jobs = body.jobs || [];
    patchRowsInPlace();
  } catch (exc) {
    logPollFailure('jobs fetch failed', exc);
  }
}

export function wireJobs() {
  if (!els.tabJobs) return;
  els.tabJobs.addEventListener('click', function () {
    fetchJobs().catch(function () {});
  });
  if (els.jobsSortBtn) {
    // The toggle lives in the card's <summary>; stop the click so it
    // flips the sort without also toggling the <details> open/closed.
    els.jobsSortBtn.addEventListener('click', function (ev) {
      ev.stopPropagation();
      toggleSort();
    });
  }
  wireJobsAgenda();
  wireJobDialogs();
}
