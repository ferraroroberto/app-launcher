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
 * This is the residual module after the audit #315/#408 splits: list
 * orchestration + poller + the expanded run-history panel. Compact row
 * rendering and patching live in jobs-row.js; the edit/add and run-now
 * dialogs live in jobs-dialog.js; the foldable Schedule agenda panel lives
 * in jobs-agenda.js.
 */

import { els, state } from './state.js';
import { apiFailToast, AuthRequiredError, jsonApi, logPollFailure, toast } from './api.js';
import { fmtAgo } from './sessions.js';
import { openJobDialog, openRunDialog, removeJob, wireJobDialogs } from './jobs-dialog.js';
import { wireJobsAgenda } from './jobs-agenda.js';
import {
  formatBytes,
  formatDuration,
  patchRowNodes,
  renderJobRow,
  statusIcon,
  toEpoch,
} from './jobs-row.js';
import { icon } from './_vendored/icons/icons.js';

// --------------------------------------------------------------- render

export function renderJobs() {
  const host = els.jobsList;
  host.innerHTML = '';
  els.jobsEmpty.hidden = state.jobs.length !== 0;
  if (els.jobsAddBtn) els.jobsAddBtn.hidden = !state.editMode;
  syncSortBtn();

  sortedJobs().forEach(function (job) {
    host.appendChild(renderJobRow(job, {
      editMode: state.editMode,
      onToggle: toggleExpanded,
      onRun: runJobNow,
      onPause: togglePause,
      onEdit: openJobDialog,
      onRemove: removeJob,
    }).li);
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

// Poll the residual list in place through jobs-row.js's shared DOM contract.
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
