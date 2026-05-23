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
  li.className = 'app-item';
  li.dataset.id = job.id;

  const main = document.createElement('div');
  main.className = 'app-main';

  const info = document.createElement('button');
  info.type = 'button';
  info.className = 'launch-btn session-open';

  const head = document.createElement('div');
  head.className = 'session-head';

  const dot = document.createElement('span');
  dot.className = 'health-dot ' + statusClass(job);
  dot.dataset.role = 'status-dot';
  head.appendChild(dot);

  const pill = document.createElement('span');
  pill.className = 'kind-pill';
  pill.textContent = job.target_kind || '?';
  head.appendChild(pill);

  if (job.schedule_chip) {
    const chip = document.createElement('span');
    chip.className = 'kind-pill';
    chip.textContent = job.schedule_chip;
    head.appendChild(chip);
  }

  const name = document.createElement('span');
  name.className = 'name';
  name.textContent = job.name;
  head.appendChild(name);

  info.appendChild(head);

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
  if (job.running || (job.last_run && job.last_run.status === 'running')) return 'up';
  if (job.last_run && job.last_run.status === 'success') return 'up';
  if (job.last_run && job.last_run.status === 'failed') return 'down';
  return '';
}

function statusIcon(status) {
  if (status === 'running' || status === 'pending') return '⏳';
  if (status === 'success') return '✅';
  if (status === 'failed') return '❌';
  return '•';
}

function describeLastRun(job) {
  const bits = [];
  if (job.last_run) {
    const ago = fmtAgo(toEpoch(job.last_run.started_at));
    const tail = (job.last_run.status || '?') + (ago ? ' · ' + ago + ' ago' : '');
    bits.push('last: ' + tail);
  } else {
    bits.push('never run');
  }
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
    meta.textContent = (r.status || '?') +
      (ago ? ' · ' + ago + ' ago' : '') +
      ' · ' + (r.trigger || '?') + exitText;
    btn.appendChild(meta);
    btn.addEventListener('click', function () { selectRun(jobId, r.run_id); });
    li.appendChild(btn);
    list.appendChild(li);
  });
}

function writeOutput(jobId, runId, text, status) {
  const panel = panelEl(jobId);
  if (!panel) return;
  const label = panel.querySelector('[data-role="output-label"]');
  const tail = panel.querySelector('[data-role="output-tail"]');
  if (!tail) return;
  if (label) {
    label.textContent = 'Output · ' + runId +
      (status ? ' · ' + status : '') +
      (status === 'running' || status === 'pending' ? ' (live)' : '');
  }
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
  writeOutput(jobId, runId, record.output_tail || '', record.status);
}

async function runJobNow(job) {
  try {
    await jsonApi('/api/jobs/' + encodeURIComponent(job.id) + '/run', { method: 'POST' });
    toast('🚀 Started ' + job.name + '.', 'good');
    job.running = true;
    renderJobs();
    // Brief delayed nudge so the new run shows up promptly without
    // waiting for the next poll tick.
    setTimeout(function () { fetchJobs().catch(function () {}); }, 1500);
  } catch (exc) {
    toast('Run failed: ' + (exc.message || exc), 'error');
  }
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
  syncScheduleFields();

  if (els.jobDialog.showModal) els.jobDialog.showModal();
}

function syncScheduleFields() {
  const t = els.jobScheduleType.value;
  els.jobScheduleEveryRow.hidden = !(t === 'minutes' || t === 'hourly');
  els.jobScheduleAtRow.hidden = !(t === 'daily' || t === 'weekly');
  els.jobScheduleTimesRow.hidden = t !== 'daily_times';
  els.jobScheduleDayRow.hidden = t !== 'weekly';
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
  return { type: 'none' };
}

async function submitJobDialog(ev) {
  ev.preventDefault();
  let schedule;
  try { schedule = buildSchedule(); } catch (exc) {
    toast('Schedule: ' + exc.message, 'error');
    return;
  }
  const payload = {
    name: els.jobNameInput.value.trim(),
    script_path: els.jobScriptInput.value.trim(),
    args: els.jobArgsInput.value,
    schedule: schedule,
  };
  try {
    const opts = {
      method: dialogTargetId ? 'PUT' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    };
    const path = dialogTargetId
      ? '/api/jobs/' + encodeURIComponent(dialogTargetId)
      : '/api/jobs';
    await jsonApi(path, opts);
    if (els.jobDialog.close) els.jobDialog.close();
    toast(dialogTargetId ? 'Job updated.' : 'Job added.', 'good');
    await fetchJobs();
  } catch (exc) {
    toast('Save failed: ' + (exc.message || exc), 'error');
  }
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
  if (els.jobCancel) {
    els.jobCancel.addEventListener('click', function () {
      if (els.jobDialog.close) els.jobDialog.close();
    });
  }
  if (els.jobScheduleType) {
    els.jobScheduleType.addEventListener('change', syncScheduleFields);
  }
}
