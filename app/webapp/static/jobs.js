/* Jobs tab: list registered jobs, fire run-now, view history, manage in
 * edit mode (issue #47).
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
 */

import { els, state } from './state.js';
import { jsonApi, toast } from './api.js';
import { fmtAgo } from './sessions.js';

// --------------------------------------------------------------- render

export function renderJobs() {
  const host = els.jobsList;
  host.innerHTML = '';
  els.jobsEmpty.hidden = state.jobs.length !== 0;
  if (els.jobsAddBtn) els.jobsAddBtn.hidden = !state.editMode;

  state.jobs.forEach(function (job) {
    host.appendChild(renderJobRow(job));
    if (state.expandedJob === job.id) {
      host.appendChild(renderHistoryLi(job));
    }
  });
}

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

  if (job.mutex_group) {
    const mg = document.createElement('span');
    mg.className = 'kind-pill job-mutex-pill';
    const depth = Number.isFinite(job.queue_depth) ? job.queue_depth : 0;
    mg.textContent = depth > 0 ? '🪢 ' + job.mutex_group + ' (' + depth + ')'
                               : '🪢 ' + job.mutex_group;
    mg.title = 'Mutex group: ' + job.mutex_group +
      (depth > 0 ? ' — ' + depth + ' queued' : '');
    pills.appendChild(mg);
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
    pauseBtn.textContent = job.paused ? '▶' : '⏸';
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
    const editBtn = document.createElement('button');
    editBtn.type = 'button';
    editBtn.className = 'icon-btn';
    editBtn.textContent = '✏️';
    editBtn.title = 'Edit ' + job.name;
    editBtn.setAttribute('aria-label', 'Edit');
    editBtn.addEventListener('click', function (ev) { ev.stopPropagation(); openJobDialog(job); });
    actions.appendChild(editBtn);

    const removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'icon-btn danger';
    removeBtn.textContent = '🗑️';
    removeBtn.title = 'Remove ' + job.name;
    removeBtn.setAttribute('aria-label', 'Remove');
    removeBtn.addEventListener('click', function (ev) { ev.stopPropagation(); removeJob(job); });
    actions.appendChild(removeBtn);
  }

  li.appendChild(actions);
  return li;
}

function setRunBtnState(btn, job) {
  btn.textContent = job.running ? '⏳' : '▶';
  btn.title = job.running ? 'A run is in progress' : ('Run ' + job.name + ' now');
  btn.setAttribute('aria-label', 'Run now');
  btn.disabled = !!job.running;
}

function statusClass(job) {
  if (job.stuck) return 'stuck';
  if (job.running || (job.last_run && job.last_run.status === 'running')) return 'up';
  if (job.last_run && job.last_run.status === 'success') return 'up';
  if (job.last_run && job.last_run.status === 'failed') return 'down';
  return '';
}

function statusIcon(status) {
  if (status === 'running' || status === 'pending') return '⏳';
  if (status === 'success') return '✅';
  if (status === 'failed') return '❌';
  if (status === 'skipped') return '⏭';
  if (status === 'queued') return '🪢';
  return '•';
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

function sparkClass(status) {
  if (status === 'success') return 'up';
  if (status === 'failed') return 'down';
  if (status === 'running' || status === 'pending') return 'live';
  if (status === 'queued') return 'live';
  if (status === 'skipped') return 'unknown';
  return 'unknown';
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
  if (job.next_run) bits.push('next: ' + job.next_run);
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
  close.textContent = '✕ Close';
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
  body.appendChild(tail);

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

  runs.slice(0, 5).forEach(function (r) {
    const li = document.createElement('li');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'jobs-run-btn';
    if (state.selectedRun && state.selectedRun.jobId === jobId
        && state.selectedRun.runId === r.run_id) {
      btn.classList.add('selected');
    }
    const icon = document.createElement('span');
    icon.className = 'jobs-run-icon';
    icon.textContent = statusIcon(r.status);
    btn.appendChild(icon);
    const meta = document.createElement('span');
    meta.className = 'jobs-run-meta';
    const ago = fmtAgo(toEpoch(r.started_at));
    const exitText = (r.exit_code === undefined || r.exit_code === null)
      ? '' : ' · exit ' + r.exit_code;
    const paramsChip = formatRunParams(r.params);
    meta.textContent = (r.status || '?') +
      (ago ? ' · ' + ago + ' ago' : '') +
      ' · ' + (r.trigger || '?') + exitText +
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
    if (String(exc.message) === 'auth required') return;
  }

  // Default selection on first paint: newest run.
  if (!state.selectedRun || state.selectedRun.jobId !== jobId) {
    if (runs.length) state.selectedRun = { jobId: jobId, runId: runs[0].run_id };
  }
  // If selection no longer exists (pruned), fall back to newest.
  if (state.selectedRun && state.selectedRun.jobId === jobId &&
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
    killBtn.textContent = '🛑 Kill stuck run';
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
    toast('Kill failed: ' + (exc.message || exc), 'error');
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
    toast(action.charAt(0).toUpperCase() + action.slice(1) +
          ' failed: ' + (exc.message || exc), 'error');
  }
}

async function runJobNow(job, options) {
  // Issue #67: jobs with declared params open a small typed form so the
  // user supplies values. Parameter-less jobs keep their one-tap fire.
  const params = (job && job.params) || [];
  const opts = options || {};
  if (params.length > 0 && !opts.skipDialog) {
    openRunDialog(job, opts.prefill || null, opts.staleKeys || null);
    return;
  }
  const body = opts.params ? { params: opts.params } : null;
  try {
    const res = await jsonApi('/api/jobs/' + encodeURIComponent(job.id) + '/run', {
      method: 'POST',
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (res && res.status === 'queued') {
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
      // The server returns a structured body with retry_after_seconds.
      // Fall back to the Retry-After-derived remaining if the body is
      // unexpectedly absent so the toast still says something useful.
      const detail = exc.body && exc.body.detail;
      const remaining = exc.body && Number(exc.body.retry_after_seconds);
      const cd = exc.body && Number(exc.body.cooldown_seconds);
      if (detail === 'cooldown' && Number.isFinite(remaining)) {
        const suffix = (Number.isFinite(cd) && cd > 0) ? ' (cooldown ' + cd + 's)' : '';
        toast('⏭ Skipped — cooled down for ' + remaining + ' more s' + suffix + '.');
        return;
      }
    }
    toast('Run failed: ' + (exc.message || exc), 'error');
  }
}

// --------------------------------------------------- chain checklist (dialog)
//
// Two <ul>s in the job dialog, one for on_success and one for on_failure.
// Each list is populated from state.jobs minus the currently-edited job
// (a job can't chain to itself — the server validates this too, but the UI
// just hides the row so the user can't even try). The cycle check is
// strictly server-side; the toast surfaces the server's precise error.

function populateChainList(host, selected, currentId, kind) {
  if (!host) return;
  host.innerHTML = '';
  const want = new Set(selected || []);
  const all = (state.jobs || []).slice().sort(function (a, b) {
    return (a.name || '').toLowerCase().localeCompare((b.name || '').toLowerCase());
  });
  let rendered = 0;
  all.forEach(function (j) {
    if (currentId && j.id === currentId) return;
    const li = document.createElement('li');
    li.className = 'job-chain-row';
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.value = j.id;
    cb.checked = want.has(j.id);
    cb.dataset.role = 'chain-' + kind;
    const label = document.createElement('label');
    label.className = 'job-chain-row-label';
    label.appendChild(cb);
    const text = document.createElement('span');
    text.textContent = j.name + '  ·  ' + j.id;
    label.appendChild(text);
    li.appendChild(label);
    host.appendChild(li);
    rendered += 1;
  });
  if (rendered === 0) {
    const li = document.createElement('li');
    li.className = 'job-chain-row muted small';
    li.textContent = '(no other jobs to choose from)';
    host.appendChild(li);
  }
}

function readChainList(host, kind) {
  if (!host) return [];
  const selector = 'input[type="checkbox"][data-role="chain-' + kind + '"]:checked';
  const checked = Array.from(host.querySelectorAll(selector));
  return checked.map(function (cb) { return cb.value; });
}

// -------------------------------------------------- params editor (dialog)

const PARAM_KINDS = ['string', 'int', 'enum', 'bool', 'date'];

function renderParamRow(param) {
  const li = document.createElement('li');
  li.className = 'job-param-row';
  li.dataset.role = 'job-param-row';

  const head = document.createElement('div');
  head.className = 'job-param-row-head';

  const nameInput = document.createElement('input');
  nameInput.type = 'text';
  nameInput.className = 'input-native';
  nameInput.placeholder = 'name (snake_case)';
  nameInput.dataset.role = 'param-name';
  nameInput.value = (param && param.name) || '';
  head.appendChild(nameInput);

  const kindSel = document.createElement('select');
  kindSel.className = 'input-native';
  kindSel.dataset.role = 'param-kind';
  PARAM_KINDS.forEach(function (k) {
    const opt = document.createElement('option');
    opt.value = k;
    opt.textContent = k;
    kindSel.appendChild(opt);
  });
  kindSel.value = (param && param.kind) || 'string';
  head.appendChild(kindSel);

  const rmBtn = document.createElement('button');
  rmBtn.type = 'button';
  rmBtn.className = 'icon-btn danger';
  rmBtn.textContent = '✕';
  rmBtn.title = 'Remove parameter';
  rmBtn.setAttribute('aria-label', 'Remove parameter');
  rmBtn.addEventListener('click', function () { li.remove(); });
  head.appendChild(rmBtn);

  li.appendChild(head);

  const grid = document.createElement('div');
  grid.className = 'job-param-row-grid';

  function makeField(role, placeholder, value) {
    const inp = document.createElement('input');
    inp.type = 'text';
    inp.className = 'input-native';
    inp.placeholder = placeholder;
    inp.dataset.role = role;
    inp.value = value == null ? '' : String(value);
    return inp;
  }

  grid.appendChild(makeField('param-flag', '--flag (optional)',
    param && param.flag));
  grid.appendChild(makeField('param-env', 'ENV_VAR (optional)',
    param && param.env));
  grid.appendChild(makeField('param-default', 'default (optional)',
    param && (param.default == null ? '' : param.default)));
  grid.appendChild(makeField('param-options',
    'enum options, comma-separated',
    param && param.options ? param.options.join(', ') : ''));

  li.appendChild(grid);
  return li;
}

function setParamsEditor(params) {
  if (!els.jobParamsList) return;
  els.jobParamsList.innerHTML = '';
  (params || []).forEach(function (p) {
    els.jobParamsList.appendChild(renderParamRow(p));
  });
}

function readParamsEditor() {
  if (!els.jobParamsList) return [];
  const rows = Array.from(els.jobParamsList.querySelectorAll('[data-role="job-param-row"]'));
  const seen = new Set();
  return rows.map(function (row) {
    const name = (row.querySelector('[data-role="param-name"]').value || '').trim();
    if (!name) throw new Error('Parameter name is required');
    if (!/^[a-z][a-z0-9_]*$/.test(name)) {
      throw new Error('Parameter name ' + JSON.stringify(name) + ' must be snake_case');
    }
    if (seen.has(name)) throw new Error('Duplicate parameter name: ' + name);
    seen.add(name);
    const kind = row.querySelector('[data-role="param-kind"]').value;
    const flag = (row.querySelector('[data-role="param-flag"]').value || '').trim();
    const env = (row.querySelector('[data-role="param-env"]').value || '').trim();
    const defaultRaw = (row.querySelector('[data-role="param-default"]').value || '').trim();
    const optionsRaw = (row.querySelector('[data-role="param-options"]').value || '').trim();
    if (flag && env) {
      throw new Error('Parameter ' + name + ': flag and env are mutually exclusive');
    }
    const out = { name: name, kind: kind };
    if (flag) out.flag = flag;
    if (env) out.env = env;
    if (kind === 'enum') {
      const options = optionsRaw.split(',').map(function (s) { return s.trim(); }).filter(Boolean);
      if (!options.length) {
        throw new Error('Parameter ' + name + ': enum needs at least one option');
      }
      out.options = options;
    }
    if (defaultRaw !== '') {
      if (kind === 'int') {
        const n = parseInt(defaultRaw, 10);
        if (!Number.isFinite(n) || String(n) !== defaultRaw) {
          throw new Error('Parameter ' + name + ': default must be an integer');
        }
        out.default = n;
      } else if (kind === 'bool') {
        if (defaultRaw !== 'true' && defaultRaw !== 'false') {
          throw new Error('Parameter ' + name + ': default must be true or false');
        }
        out.default = defaultRaw === 'true';
      } else {
        out.default = defaultRaw;
      }
    }
    return out;
  });
}

// ------------------------------------------------ run-now dialog (#67)

let runDialogJob = null;

function openRunDialog(job, prefill, staleKeys) {
  runDialogJob = job;
  els.jobRunDialogTitle.textContent = '▶ ' + job.name;
  if (staleKeys && staleKeys.length) {
    els.jobRunDialogStaleNote.hidden = false;
    els.jobRunDialogStaleNote.textContent =
      'Note: ' + staleKeys.join(', ') + ' from the previous run ' +
      (staleKeys.length === 1 ? 'was' : 'were') +
      ' dropped (no longer declared on this job).';
  } else {
    els.jobRunDialogStaleNote.hidden = true;
    els.jobRunDialogStaleNote.textContent = '';
  }

  const host = els.jobRunDialogFields;
  host.innerHTML = '';
  (job.params || []).forEach(function (p) {
    host.appendChild(renderRunDialogField(p, prefill));
  });

  if (els.jobRunDialog.showModal) els.jobRunDialog.showModal();
}

function renderRunDialogField(param, prefill) {
  const label = document.createElement('label');
  label.className = 'stacked';

  const span = document.createElement('span');
  let title = param.name;
  if (param.flag) title += ' (' + param.flag + ')';
  else if (param.env) title += ' ($' + param.env + ')';
  if (param.required && (param.default === undefined || param.default === null)) {
    title += ' *';
  }
  span.textContent = title;
  label.appendChild(span);

  const initial = (prefill && Object.prototype.hasOwnProperty.call(prefill, param.name))
    ? prefill[param.name]
    : (param.default !== undefined && param.default !== null ? param.default : null);

  let input;
  if (param.kind === 'enum') {
    input = document.createElement('select');
    input.className = 'input-native';
    (param.options || []).forEach(function (opt) {
      const o = document.createElement('option');
      o.value = opt;
      o.textContent = opt;
      input.appendChild(o);
    });
    if (initial != null) input.value = String(initial);
  } else if (param.kind === 'bool') {
    input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = initial === true || initial === 'true';
  } else {
    input = document.createElement('input');
    input.type = param.kind === 'date' ? 'date'
      : param.kind === 'int' ? 'number'
      : 'text';
    input.className = 'input-native';
    if (initial != null) input.value = String(initial);
    if (param.required) input.required = true;
  }
  input.dataset.paramName = param.name;
  input.dataset.paramKind = param.kind;
  label.appendChild(input);
  return label;
}

function readRunDialogValues() {
  const out = {};
  const inputs = els.jobRunDialogFields.querySelectorAll('[data-param-name]');
  inputs.forEach(function (el) {
    const name = el.dataset.paramName;
    const kind = el.dataset.paramKind;
    if (kind === 'bool') {
      out[name] = !!el.checked;
      return;
    }
    const raw = (el.value || '').trim();
    if (raw === '') return;  // server applies defaults / enforces required
    if (kind === 'int') {
      const n = parseInt(raw, 10);
      if (!Number.isFinite(n)) throw new Error(name + ' must be an integer');
      out[name] = n;
    } else {
      out[name] = raw;
    }
  });
  return out;
}

async function submitRunDialog(ev) {
  ev.preventDefault();
  if (!runDialogJob) return;
  let values;
  try { values = readRunDialogValues(); } catch (exc) {
    toast(String(exc.message || exc), 'error');
    return;
  }
  const job = runDialogJob;
  if (els.jobRunDialog.close) els.jobRunDialog.close();
  runDialogJob = null;
  await runJobNow(job, { params: values, skipDialog: true });
}

async function removeJob(job) {
  if (!confirm('Remove ' + job.name + ' from the jobs registry?')) return;
  try {
    await jsonApi('/api/jobs/' + encodeURIComponent(job.id), { method: 'DELETE' });
    toast('Removed ' + job.name, 'good');
    await fetchJobs();
  } catch (exc) {
    toast('Remove failed: ' + (exc.message || exc), 'error');
  }
}

// ------------------------------------------------------------ dialog form

let dialogTargetId = null;

function openJobDialog(job) {
  dialogTargetId = job ? job.id : null;
  els.jobDialogTitle.textContent = job ? 'Edit job' : 'Add job';
  els.jobIdField.value = job ? job.id : '';
  els.jobNameInput.value = job ? job.name : '';
  els.jobScriptInput.value = job ? job.script_path : '';
  els.jobArgsInput.value = job ? (job.args || '') : '';

  const sched = (job && job.schedule) || { type: 'none' };
  els.jobScheduleType.value = sched.type || 'none';
  if (sched.type === 'minutes' || sched.type === 'hourly') {
    els.jobScheduleEvery.value = sched.every || 1;
  } else {
    els.jobScheduleEvery.value = 1;
  }
  if (sched.type === 'daily' || sched.type === 'weekly') {
    els.jobScheduleAt.value = typeof sched.at === 'string' ? sched.at : '';
  } else {
    els.jobScheduleAt.value = '';
  }
  if (sched.type === 'daily_times' && Array.isArray(sched.at)) {
    els.jobScheduleTimes.value = sched.at.join(', ');
  } else {
    els.jobScheduleTimes.value = '';
  }
  els.jobScheduleDay.value = sched.day || 'MON';
  if (els.jobScheduleOnceAt) {
    els.jobScheduleOnceAt.value =
      (sched.type === 'once' && typeof sched.at === 'string') ? sched.at : '';
  }
  syncScheduleFields();

  if (els.jobCooldownInput) {
    const cd = job && Number.isFinite(job.cooldown_seconds) ? job.cooldown_seconds : 0;
    els.jobCooldownInput.value = cd > 0 ? String(cd) : '';
  }
  if (els.jobMutexGroupInput) {
    els.jobMutexGroupInput.value = (job && job.mutex_group) || '';
  }
  populateChainList(
    els.jobOnSuccessList,
    job ? (job.on_success || []) : [],
    job ? job.id : null,
    'on_success',
  );
  populateChainList(
    els.jobOnFailureList,
    job ? (job.on_failure || []) : [],
    job ? job.id : null,
    'on_failure',
  );

  setParamsEditor(job ? job.params : []);

  clearPreflightProblems();
  if (els.jobDialog.showModal) els.jobDialog.showModal();
}

function syncScheduleFields() {
  const t = els.jobScheduleType.value;
  els.jobScheduleEveryRow.hidden = !(t === 'minutes' || t === 'hourly');
  els.jobScheduleAtRow.hidden = !(t === 'daily' || t === 'weekly');
  els.jobScheduleTimesRow.hidden = t !== 'daily_times';
  els.jobScheduleDayRow.hidden = t !== 'weekly';
  if (els.jobScheduleOnceRow) els.jobScheduleOnceRow.hidden = t !== 'once';
}

function buildSchedule() {
  const t = els.jobScheduleType.value;
  if (t === 'none') return { type: 'none' };
  if (t === 'minutes' || t === 'hourly') {
    const every = parseInt(els.jobScheduleEvery.value, 10);
    if (!Number.isFinite(every) || every <= 0) throw new Error('Every must be > 0');
    return { type: t, every: every };
  }
  if (t === 'daily') {
    const at = els.jobScheduleAt.value.trim();
    if (!/^[0-2]\d:[0-5]\d$/.test(at)) throw new Error('At must be HH:MM');
    return { type: 'daily', at: at };
  }
  if (t === 'daily_times') {
    const list = els.jobScheduleTimes.value
      .split(',')
      .map(function (s) { return s.trim(); })
      .filter(Boolean);
    if (!list.length) throw new Error('Provide at least one HH:MM');
    list.forEach(function (s) {
      if (!/^[0-2]\d:[0-5]\d$/.test(s)) throw new Error('Each time must be HH:MM');
    });
    return { type: 'daily_times', at: list };
  }
  if (t === 'weekly') {
    const at = els.jobScheduleAt.value.trim();
    if (!/^[0-2]\d:[0-5]\d$/.test(at)) throw new Error('At must be HH:MM');
    return { type: 'weekly', day: els.jobScheduleDay.value, at: at };
  }
  if (t === 'once') {
    const at = (els.jobScheduleOnceAt && els.jobScheduleOnceAt.value || '').trim();
    if (!/^\d{4}-\d{2}-\d{2}T[0-2]\d:[0-5]\d$/.test(at)) {
      throw new Error('Once: pick a date and time');
    }
    return { type: 'once', at: at };
  }
  return { type: 'none' };
}

// Pre-flight problems (issue #69). The last-submitted payload is held so
// "Save anyway" can re-POST it with acknowledge_warnings:true once the
// user has seen the warnings.
let lastJobPayload = null;

function clearPreflightProblems() {
  if (els.jobPreflightProblems) {
    els.jobPreflightProblems.innerHTML = '';
    els.jobPreflightProblems.hidden = true;
  }
  if (els.jobSaveAnyway) els.jobSaveAnyway.hidden = true;
  if (els.jobSaveBtn) {
    els.jobSaveBtn.textContent = dialogTargetId ? 'Save and verify' : 'Add and verify';
  }
}

function renderPreflightProblems(problems) {
  const host = els.jobPreflightProblems;
  if (!host) return;
  host.innerHTML = '';
  (problems || []).forEach(function (p) {
    const li = document.createElement('li');
    li.className = 'job-preflight-problem ' + (p.level === 'error' ? 'error' : 'warning');
    const tag = document.createElement('span');
    tag.className = 'job-preflight-tag';
    tag.textContent = p.level === 'error' ? '❌' : '⚠️';
    li.appendChild(tag);
    const text = document.createElement('span');
    text.textContent = (p.field ? p.field + ': ' : '') + p.message;
    li.appendChild(text);
    host.appendChild(li);
  });
  host.hidden = (problems || []).length === 0;
}

function buildJobPayload() {
  const schedule = buildSchedule();      // may throw
  const params = readParamsEditor();     // may throw
  const payload = {
    name: els.jobNameInput.value.trim(),
    script_path: els.jobScriptInput.value.trim(),
    args: els.jobArgsInput.value,
    schedule: schedule,
    params: params,
  };
  // Empty → omit (server stores null). "0" → omit too (treated as off).
  // Negative or non-numeric → tell the user; the server cap (>86400)
  // we let the server reject so the limit lives in one place.
  const cdRaw = els.jobCooldownInput ? els.jobCooldownInput.value.trim() : '';
  if (cdRaw) {
    const cd = parseInt(cdRaw, 10);
    if (!Number.isFinite(cd) || cd < 0) {
      throw new Error('Cooldown must be a non-negative integer');
    }
    if (cd > 0) payload.cooldown_seconds = cd;
  }
  // Empty → omit (server treats as null); the server validates the shape
  // (lowercase alnum + _/-, starts with letter, <=32 chars) so the
  // 400 surfaces here as a normal toast.
  const mg = els.jobMutexGroupInput ? els.jobMutexGroupInput.value.trim() : '';
  if (mg) payload.mutex_group = mg;
  else if (dialogTargetId) payload.mutex_group = null;  // clear on edit
  // Chain edges (issue #68 PR #3). Always send both keys on submit so a
  // user un-checking the last entry actually clears it server-side.
  payload.on_success = readChainList(els.jobOnSuccessList, 'on_success');
  payload.on_failure = readChainList(els.jobOnFailureList, 'on_failure');
  return payload;
}

async function postJobPayload(payload) {
  try {
    const opts = {
      method: dialogTargetId ? 'PUT' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    };
    const path = dialogTargetId
      ? '/api/jobs/' + encodeURIComponent(dialogTargetId)
      : '/api/jobs';
    const res = await jsonApi(path, opts);
    // Warnings-only, not acknowledged: the server didn't save. Keep the
    // dialog open, show the warnings, and offer "Save anyway".
    if (res && res.saved === false) {
      renderPreflightProblems(res.warnings || []);
      if (els.jobSaveAnyway) els.jobSaveAnyway.hidden = false;
      return;
    }
    if (els.jobDialog.close) els.jobDialog.close();
    clearPreflightProblems();
    const warned = res && Array.isArray(res.warnings) && res.warnings.length;
    toast(
      (dialogTargetId ? 'Job updated.' : 'Job added.') +
        (warned ? ' (saved with warnings)' : ''),
      'good',
    );
    await fetchJobs();
  } catch (exc) {
    // Pre-flight errors come back as a 400 with a structured problems
    // list — render them inline (red) and keep the dialog open.
    const detail = exc && exc.body && exc.body.detail;
    if (exc && exc.status === 400 && detail && detail.reason === 'preflight') {
      renderPreflightProblems(detail.problems || []);
      if (els.jobSaveAnyway) els.jobSaveAnyway.hidden = true;
      return;
    }
    toast('Save failed: ' + (exc.message || exc), 'error');
  }
}

async function submitJobDialog(ev) {
  ev.preventDefault();
  let payload;
  try { payload = buildJobPayload(); } catch (exc) {
    toast(String(exc.message || exc), 'error');
    return;
  }
  clearPreflightProblems();
  lastJobPayload = payload;
  await postJobPayload(payload);
}

async function saveJobAnyway() {
  if (!lastJobPayload) return;
  const payload = Object.assign({}, lastJobPayload, { acknowledge_warnings: true });
  await postJobPayload(payload);
}

// ------------------------------------------------------ in-place row patch

function patchRowsInPlace() {
  const host = els.jobsList;
  const existing = Array.from(host.querySelectorAll('li.app-item[data-id]'));
  if (existing.length !== state.jobs.length) { renderJobs(); return; }
  for (let i = 0; i < existing.length; i++) {
    const li = existing[i];
    const job = state.jobs[i];
    if (!job || li.dataset.id !== job.id) { renderJobs(); return; }
    const dot = li.querySelector('[data-role="status-dot"]');
    if (dot) dot.className = 'health-dot ' + statusClass(job);
    const meta = li.querySelector('[data-role="meta"]');
    if (meta) meta.textContent = describeLastRun(job);
    const runBtn = li.querySelector('[data-role="run-btn"]');
    if (runBtn) setRunBtnState(runBtn, job);
    // Sparkline + duration chip can change between polls (new run
    // finished, stats recomputed) — swap them in place so the rest of
    // the row doesn't flash. Both live on the "load" sub-row.
    const loadRow = li.querySelector('[data-role="job-load"]');
    if (loadRow) {
      const oldChip = loadRow.querySelector('[data-role="duration-chip"]');
      const freshChip = renderDurationChip(job);
      if (freshChip) freshChip.dataset.role = 'duration-chip';
      if (oldChip && freshChip) loadRow.replaceChild(freshChip, oldChip);
      else if (oldChip && !freshChip) oldChip.remove();
      else if (!oldChip && freshChip) {
        const ref = loadRow.querySelector('[data-role="sparkline"]');
        if (ref) loadRow.insertBefore(freshChip, ref);
        else loadRow.appendChild(freshChip);
      }
      const oldSpark = loadRow.querySelector('[data-role="sparkline"]');
      const freshSpark = renderSparkline(job);
      if (freshSpark) freshSpark.dataset.role = 'sparkline';
      if (oldSpark && freshSpark) loadRow.replaceChild(freshSpark, oldSpark);
      else if (oldSpark && !freshSpark) oldSpark.remove();
      else if (!oldSpark && freshSpark) loadRow.appendChild(freshSpark);
    }
  }
}

// ------------------------------------------------------------ fetch + wire

export async function fetchJobs() {
  if (state.tab !== 'jobs') return;
  // While a row is expanded, polling refreshes that one panel's content
  // in place — touching the row list would tear down the user's view.
  if (state.expandedJob) {
    await refreshExpandedContent(state.expandedJob);
    return;
  }
  try {
    const body = await jsonApi('/api/jobs');
    state.jobs = body.jobs || [];
    patchRowsInPlace();
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      console.warn('jobs fetch failed', exc);
    }
  }
}

export function wireJobs() {
  if (!els.tabJobs) return;
  els.tabJobs.addEventListener('click', function () {
    fetchJobs().catch(function () {});
  });
  if (els.refreshJobs) {
    els.refreshJobs.addEventListener('click', function () { fetchJobs().catch(function () {}); });
  }
  if (els.jobsAddBtn) {
    els.jobsAddBtn.addEventListener('click', function () { openJobDialog(null); });
  }
  if (els.jobForm) {
    els.jobForm.addEventListener('submit', submitJobDialog);
  }
  if (els.jobSaveAnyway) {
    els.jobSaveAnyway.addEventListener('click', function () {
      saveJobAnyway().catch(function () {});
    });
  }
  if (els.jobCancel) {
    els.jobCancel.addEventListener('click', function () {
      if (els.jobDialog.close) els.jobDialog.close();
    });
  }
  if (els.jobScheduleType) {
    els.jobScheduleType.addEventListener('change', syncScheduleFields);
  }
  if (els.jobParamsAdd) {
    els.jobParamsAdd.addEventListener('click', function () {
      if (els.jobParamsList) {
        els.jobParamsList.appendChild(renderParamRow(null));
      }
    });
  }
  if (els.jobRunForm) {
    els.jobRunForm.addEventListener('submit', submitRunDialog);
  }
  if (els.jobRunCancel) {
    els.jobRunCancel.addEventListener('click', function () {
      if (els.jobRunDialog && els.jobRunDialog.close) els.jobRunDialog.close();
      runDialogJob = null;
    });
  }
}
