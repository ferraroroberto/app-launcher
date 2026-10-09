/* Jobs tab: list registered jobs, fire run-now, view history (issue #47).
 *
 * Tapping a job opens its sheet (#1438, decision 7 of #1432; an inline
 * accordion before): details, flags, Kill stuck run, the runs list and one
 * selected run's output, with Run now as the sheet's one primary action.
 *   - The sheet opens on the newest run; tap a run to switch the log.
 *   - Polling refreshes the cheap runs list. A selected live run uses one
 *     WebSocket snapshot plus deltas; finalized output is fetched once.
 *   - Scroll position is preserved on update; auto-follow-bottom kicks
 *     in only when the user was already at the bottom (classic tail -f).
 *   - On the wide desktop layout the sheet docks beside the list as the
 *     detail pane (design.md master-detail) instead of covering it.
 *
 * This is the residual module after the audit #315/#408 splits: list
 * orchestration, the poller and the job sheet. Row rendering lives in
 * jobs-row.js; the edit/add and run-now dialogs in jobs-dialog.js; the Next
 * up card and the agenda sheet in jobs-agenda.js.
 */

import { els, state } from './state.js';
import { renderHomeHead } from './home-head.js';
import {
  apiFailToast,
  AuthRequiredError,
  escapeHtml,
  jsonApi,
  logPollFailure,
  readToken,
  toast,
} from './api.js';
import { fmtAgo } from './sessions.js';
import { confirmDialog } from './confirm-dialog.js';
import { isWideLayout } from './layout.js';
import { openJobDialog, openRunDialog, removeJob, wireJobDialogs } from './jobs-dialog.js';
import { fetchAgenda, wireJobsAgenda } from './jobs-agenda.js';
import {
  endJobRowRender,
  formatBytes,
  formatDuration,
  renderJobDetails,
  renderJobRow,
  runBlockedReason,
  runOutcome,
  statusIcon,
  toEpoch,
} from './jobs-row.js';
import { icon } from './_vendored/icons/icons.js';

let searchTimer = null;
let liveSocket = null;
let liveSocketKey = null;
const runExtras = new Map();

function findJob(jobId) {
  return state.jobs.find(function (j) { return j.id === jobId; }) || null;
}

// --------------------------------------------------------------- render

// The empty-state block's visibility. Until the first /api/jobs answer, the
// list is loading rather than empty: the loading line says so, and the
// empty state waits for a real zero (#1133, #1176).
function syncJobsEmpty() {
  if (els.jobsLoading) els.jobsLoading.hidden = state.jobsLoaded;
  els.jobsEmpty.hidden = !state.jobsLoaded || !!state.jobsSearchQuery ||
    state.jobs.length !== 0;
  if (!state.jobsSearchQuery && els.jobsFilterEmpty) els.jobsFilterEmpty.hidden = true;
}

// The job row's callbacks, shared by the full list and a search's name hits.
// Each is called through, never bound at load: jobs-dialog.js imports this
// module too, and a cycle must not hand a row an unset binding.
const rowHandlers = {
  onOpen: function (job) { openJobSheet(job.id); },
  onRun: function (job, options) { runJobNow(job, options); },
  onPause: function (job) { togglePause(job); },
  onEdit: function (job) { openJobDialog(job); },
  onRemove: function (job) { removeJob(job); },
};

function appendRow(host, job) {
  const li = renderJobRow(job, rowHandlers);
  // The row whose job is in the docked detail pane keeps the accent-soft
  // tint (design.md master-detail).
  if (state.sheetJob === job.id) li.setAttribute('aria-current', 'true');
  host.appendChild(li);
}

export function renderJobs() {
  const host = els.jobsList;
  host.replaceChildren();
  renderHomeHead();
  syncJobsEmpty();
  syncSortBtn();

  if (state.jobsSearchQuery) {
    // Jobs whose name matches come first, as ordinary rows (#1132: the
    // search used to match run output only, never the job you were looking
    // for by name), then the run-output hits.
    const query = state.jobsSearchQuery.toLowerCase();
    const named = sortedJobs().filter(function (job) {
      return String(job.name || '').toLowerCase().indexOf(query) !== -1 ||
        String(job.id || '').toLowerCase().indexOf(query) !== -1;
    });
    named.forEach(function (job) { appendRow(host, job); });
    renderSearchMatches(host, named.length);
  } else {
    sortedJobs().forEach(function (job) { appendRow(host, job); });
  }
  // An open ⋯ menu whose row is gone drops its state (row-menu contract).
  endJobRowRender();
}

// A run-output hit: the run's outcome glyph, "job · run", and the matching
// line, opening the job's sheet on that run.
function renderSearchMatches(host, namedCount) {
  const matches = state.jobsSearchMatches || [];
  // Nothing by name and nothing in the output: the canonical empty state.
  if (els.jobsFilterEmpty) els.jobsFilterEmpty.hidden = !!(matches.length || namedCount);
  matches.forEach(function (match) {
    const li = document.createElement('li');
    li.className = 'action-row job-search-hit';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'action-row-main';
    const title = document.createElement('span');
    title.className = 'action-row-title';
    title.innerHTML = icon(statusIcon(runOutcome(match))) + ' ';
    title.append(match.job_id + ' · ' + match.run_id);
    button.appendChild(title);
    const snippet = document.createElement('span');
    snippet.className = 'action-row-meta jobs-search-snippet';
    snippet.textContent = match.snippet || '(match in empty output)';
    button.appendChild(snippet);
    button.addEventListener('click', function () { openSearchMatch(match); });
    li.appendChild(button);
    host.appendChild(li);
  });
}

async function runJobsSearch() {
  const query = (els.jobsSearchInput && els.jobsSearchInput.value || '').trim();
  state.jobsSearchQuery = query;
  if (els.jobsSearchClear) els.jobsSearchClear.hidden = !query;
  if (!query) {
    state.jobsSearchMatches = [];
    renderJobs();
    return;
  }
  els.jobsList.innerHTML = '<li class="jobs-search-empty muted small">Searching run output…</li>';
  if (els.jobsFilterEmpty) els.jobsFilterEmpty.hidden = true;
  try {
    const body = await jsonApi('/api/jobs/runs/search?q=' + encodeURIComponent(query));
    if (state.jobsSearchQuery !== query) return;
    state.jobsSearchMatches = body.matches || [];
    renderJobs();
  } catch (exc) {
    if (exc instanceof AuthRequiredError) return;
    if (state.jobsSearchQuery !== query) return;
    els.jobsList.innerHTML = '<li class="jobs-search-empty muted small">Run search unavailable.</li>';
  }
}

function openSearchMatch(match) {
  if (!findJob(match.job_id)) {
    toast('That job is no longer registered.', 'error');
    return;
  }
  if (els.jobsSearchInput) els.jobsSearchInput.value = '';
  if (els.jobsSearchClear) els.jobsSearchClear.hidden = true;
  state.jobsSearchQuery = '';
  state.jobsSearchMatches = [];
  openJobSheet(match.job_id, match.run_id);
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

// An icon button (#1438): the glyph is the current order, and the name says
// what a tap does.
function syncSortBtn() {
  const btn = els.jobsSortBtn;
  if (!btn) return;
  const byNameNow = state.jobsSort === 'name';
  btn.innerHTML = icon(byNameNow ? 'arrow-down-up' : 'timer');
  btn.title = byNameNow
    ? 'Sorted A–Z — tap to sort by next run'
    : 'Sorted by next run — tap to sort A–Z';
  btn.setAttribute('aria-label', btn.title);
  btn.dataset.sort = state.jobsSort;
}

function toggleSort() {
  state.jobsSort = state.jobsSort === 'next' ? 'name' : 'next';
  try { localStorage.setItem('launcher.jobsSort', state.jobsSort); } catch (_) { /* storage blocked */ }
  renderJobs();
}

// ------------------------------------------------------------- job sheet

function sheetOpen() {
  return !!(els.jobSheet && els.jobSheet.open);
}

/* Open a job's sheet, on its newest run or on `runId` (a search hit). On a
 * phone it is a modal; on the wide layout it docks as the detail pane, so
 * another row's tap swaps the job in place. */
export function openJobSheet(jobId, runId) {
  const job = findJob(jobId);
  if (!job || !els.jobSheet) return;
  stopLiveStream();
  state.sheetJob = jobId;
  state.selectedRun = runId ? { jobId: jobId, runId: runId } : null;
  buildSheet(job);
  if (!sheetOpen()) {
    if (isWideLayout()) els.jobSheet.show();
    else els.jobSheet.showModal();
  }
  renderJobs();
  refreshSheetContent(jobId, { fetchOutput: true }).catch(function () {});
}

function closeJobSheet() {
  if (sheetOpen()) els.jobSheet.close();
}

// The sheet closed, by ✕, Escape or a tab change: forget the job and its
// stream, and drop the row's selected tint.
function onSheetClosed() {
  stopLiveStream();
  state.sheetJob = null;
  state.selectedRun = null;
  renderJobs();
}

// The sheet's body, in the accordion's order: details and flags, Kill stuck
// run (renderKillButton adds it), the runs, the output, artifacts and the
// webhook payload.
function buildSheet(job) {
  els.jobSheetTitle.textContent = job.name;
  els.jobSheetTitle.title = job.name;
  const body = els.jobSheetBody;
  body.replaceChildren();
  body.dataset.jobId = job.id;

  body.appendChild(renderJobDetails(job));

  const runsHead = document.createElement('h3');
  runsHead.className = 'job-sheet-section';
  runsHead.dataset.role = 'runs-head';
  runsHead.textContent = 'Recent runs';
  body.appendChild(runsHead);

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

  const artifacts = document.createElement('section');
  artifacts.className = 'jobs-artifacts';
  artifacts.dataset.role = 'artifacts';
  artifacts.hidden = true;
  body.appendChild(artifacts);

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

  syncSheetRun(job);
}

// The poll's refresh of the facts above the runs: the job object is new on
// every poll, so the detail block and Run now follow it.
function refreshSheetDetails(job) {
  const body = els.jobSheetBody;
  const old = body.querySelector('[data-role="job-details"]');
  if (old) old.replaceWith(renderJobDetails(job));
  els.jobSheetTitle.textContent = job.name;
  syncSheetRun(job);
}

// Run now, the sheet's one primary action: disabled while the job runs or
// when the job cannot be run from here, the reason in its name.
function syncSheetRun(job) {
  const btn = els.jobSheetRun;
  const blocked = runBlockedReason(job);
  btn.disabled = !!blocked;
  btn.innerHTML = job.running
    ? icon('hourglass') + ' Running…'
    : icon('play') + ' Run now';
  btn.title = blocked || 'Run ' + job.name + ' now';
  btn.setAttribute('aria-label', btn.title);
}

// The sheet's body while it shows `jobId`, else null: every writer below
// goes through this, so a late response for another job writes nothing.
function panelEl(jobId) {
  if (!sheetOpen() || state.sheetJob !== jobId) return null;
  return els.jobSheetBody;
}

/* Provenance chip (issue #72): compact "who fired this" label from the
 * run's trigger_source (+ token label when a scoped token was used). Falls
 * back to the raw trigger string for pre-provenance records. */
function triggerChip(r) {
  const src = r.trigger_source || '';
  if (src === 'schtasks') return icon('clock') + ' schedule';
  if (src.indexOf('webhook:') === 0) {
    return icon('webhook') + ' ' + escapeHtml(src.slice('webhook:'.length));
  }
  if (src === 'api') {
    if (r.trigger_token_label) return icon('sliders-horizontal') + ' ' + escapeHtml(r.trigger_token_label);
    return icon('smartphone') + ' ' + escapeHtml(r.trigger || 'manual');
  }
  if ((r.trigger || '').indexOf('chain') === 0) return icon('link') + ' ' + escapeHtml(r.trigger);
  return escapeHtml(r.trigger || '?');
}

/* "Who ended this run" chip (issue #695, #747). A bare `failed` covers
 * several genuinely different outcomes and the row used to render them
 * identically: the job exited non-zero on its own, an operator tapped
 * Kill (`killed`), the executor's last-resort watchdog tore down a
 * wedged run (`watchdog` + `watchdog_reason`), or the launcher discovered
 * the executor itself had died and reconciled the record after the fact
 * (`reaped`) — `end_time_unknown` further flags that even the reconciled
 * `finished_at` could not be established from real evidence. Only one can
 * apply, and a run that ended by itself gets no chip at all. */
function endedChip(r) {
  if (r.watchdog) {
    const reason = String(r.watchdog_reason || '').replace(/_/g, ' ');
    return ' · ' + icon('hourglass') + ' watchdog' +
      (reason ? ' ' + escapeHtml(reason) : '');
  }
  if (r.killed) return ' · ' + icon('octagon-x') + ' killed';
  if (r.reaped) {
    return ' · reaped' + (r.end_time_unknown ? ' (end time unknown)' : '');
  }
  return '';
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
  const job = findJob(jobId);
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
    // `outcome` (issue #916), not `status`: an exit code the scheduled-run
    // adapter reserves for "delivery was never established" is not a failure,
    // and drawing it with the red ✗ is what buried the genuine ones.
    const outcome = runOutcome(r);
    iconEl.className = 'jobs-run-icon jobs-run-' + (outcome || 'unknown');
    iconEl.innerHTML = icon(statusIcon(outcome));
    btn.appendChild(iconEl);
    const meta = document.createElement('span');
    meta.className = 'jobs-run-meta';
    const ago = fmtAgo(toEpoch(r.started_at));
    const exitText = (r.exit_code === undefined || r.exit_code === null)
      ? '' : ' · exit ' + r.exit_code;
    const paramsChip = formatRunParams(r.params);
    meta.innerHTML = escapeHtml(
      outcome === 'unconfirmed' ? 'not confirmed' : (outcome || '?')
    ) +
      (ago ? ' · ' + escapeHtml(ago) + ' ago' : '') +
      ' · ' + triggerChip(r) + escapeHtml(exitText) +
      (r.dry_run ? ' · ' + icon('flask-conical') + ' dry' : '') +
      endedChip(r) +
      (paramsChip ? ' · ' + escapeHtml(paramsChip) : '');
    // A failed run says nothing about *who* ended it. The note carries
    // the watchdog's own one-liner ("watchdog: no output for 68min"); absent
    // one, the exit code's own meaning (#916) is the next best explanation,
    // and for 122/124 it is the whole answer.
    if (r.note) meta.title = r.note;
    else if (r.outcome_reason) meta.title = r.outcome_reason;
    btn.appendChild(meta);
    btn.addEventListener('click', function () { selectRun(jobId, r.run_id); });
    li.appendChild(btn);

    const pin = document.createElement('button');
    pin.type = 'button';
    pin.className = 'icon-button jobs-pin-btn' + (r.pinned ? ' selected' : '');
    pin.innerHTML = icon('pin');
    pin.title = r.pinned ? 'Unpin run' : 'Pin run — keep forever';
    pin.setAttribute('aria-label', pin.title);
    pin.setAttribute('aria-pressed', r.pinned ? 'true' : 'false');
    pin.addEventListener('click', function (ev) {
      ev.stopPropagation();
      toggleRunPin(jobId, r);
    });
    li.appendChild(pin);

    // Re-run button (issue #67) — only meaningful when the job declares
    // params now. Opens the run dialog pre-filled with this run's values.
    if (job && declaredNames.size && r.params && typeof r.params === 'object') {
      const rerun = document.createElement('button');
      rerun.type = 'button';
      rerun.className = 'icon-button';
      rerun.innerHTML = icon('refresh-cw');
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
  const key = jobId + '/' + runId;
  if (extras) runExtras.set(key, extras);
  extras = extras || runExtras.get(key) || {};
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
  renderKillButton(jobId, runId, status);
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

  renderArtifacts(jobId, runId, extras.artifacts || []);

  const webhookDetails = panel.querySelector('[data-role="webhook-payload-details"]');
  const webhookPre = panel.querySelector('[data-role="webhook-payload-body"]');
  if (webhookDetails && webhookPre) {
    const wh = extras && extras.webhook_payload;
    webhookDetails.hidden = !wh;
    webhookPre.textContent = wh ? JSON.stringify(wh, null, 2) : '';
  }
}

function appendOutput(jobId, runId, chunk, status) {
  const panel = panelEl(jobId);
  if (!panel || !state.selectedRun || state.selectedRun.runId !== runId) return;
  const tail = panel.querySelector('[data-role="output-tail"]');
  if (!tail) return;
  const wasAtBottom = tail.scrollTop + tail.clientHeight >= tail.scrollHeight - 4;
  if (tail.textContent === '(no output)') tail.textContent = '';
  tail.textContent += chunk;
  if (wasAtBottom) tail.scrollTop = tail.scrollHeight;
  const label = panel.querySelector('[data-role="output-label"]');
  if (label) label.textContent = 'Output · ' + runId + ' · ' + status + ' (live)';
}

function renderArtifacts(jobId, runId, artifacts) {
  const panel = panelEl(jobId);
  const host = panel && panel.querySelector('[data-role="artifacts"]');
  if (!host) return;
  host.innerHTML = '';
  host.hidden = !artifacts.length;
  if (!artifacts.length) return;
  const heading = document.createElement('h4');
  heading.textContent = 'Artifacts';
  host.appendChild(heading);
  const list = document.createElement('ul');
  artifacts.forEach(function (artifact) {
    const li = document.createElement('li');
    const link = document.createElement('a');
    let href = '/api/jobs/' + encodeURIComponent(jobId) + '/runs/' +
      encodeURIComponent(runId) + '/artifacts/' + encodeURIComponent(artifact.name);
    const token = readToken();
    if (token) href += '?token=' + encodeURIComponent(token);
    link.href = href;
    link.download = artifact.name;
    link.innerHTML = icon('download') + ' ' + escapeHtml(artifact.name);
    const size = document.createElement('span');
    size.textContent = formatBytes(artifact.size) || '';
    li.append(link, size);
    list.appendChild(li);
  });
  host.appendChild(list);
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
    toast('Copied log', 'good', { icon: 'clipboard' });
  } catch (exc) {
    toast('Clipboard unavailable — copy manually', 'error');
  }
}

// ---------------------------------------------------------- interactions

function selectRun(jobId, runId) {
  stopLiveStream();
  state.selectedRun = { jobId: jobId, runId: runId };
  // Re-render the runs list so the highlight moves immediately, then
  // load the chosen run's output (always — even if static).
  redrawRunsList(jobId, state.jobRuns[jobId] || []);
  refreshOutputForRun(jobId, runId).catch(function () {});
}

async function refreshSheetContent(jobId, opts) {
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
  if (state.sheetJob !== jobId) return;

  // Default selection on first paint: newest run.
  if (!state.selectedRun || state.selectedRun.jobId !== jobId) {
    if (runs.length) state.selectedRun = { jobId: jobId, runId: runs[0].run_id };
  }
  // If selection no longer exists (pruned), fall back to newest — but only on
  // an explicit/initial refresh, never on the poll. Snapping the poll back
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
  if (isLive) {
    if (opts.fetchOutput || isFirstPaint) {
      await refreshOutputForRun(jobId, selectedRunId);
    }
    openLiveStream(jobId, selectedRunId);
  } else if (opts.fetchOutput || isFirstPaint) {
    stopLiveStream();
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
    artifacts: record.artifacts || [],
  });
}

function openLiveStream(jobId, runId) {
  const key = jobId + '/' + runId;
  if (liveSocket && liveSocketKey === key &&
      (liveSocket.readyState === WebSocket.CONNECTING ||
       liveSocket.readyState === WebSocket.OPEN)) return;
  stopLiveStream();
  const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  let url = scheme + '//' + window.location.host + '/api/jobs/' +
    encodeURIComponent(jobId) + '/runs/' + encodeURIComponent(runId) + '/stream';
  const token = readToken();
  if (token) url += '?token=' + encodeURIComponent(token);
  const socket = new WebSocket(url);
  liveSocket = socket;
  liveSocketKey = key;
  socket.addEventListener('message', function (event) {
    if (liveSocket !== socket) return;
    let frame;
    try { frame = JSON.parse(event.data); } catch (_) { return; }
    if (frame.type === 'snapshot') {
      writeOutput(jobId, runId, frame.output || '', frame.status || 'running');
    } else if (frame.type === 'chunk') {
      appendOutput(jobId, runId, frame.output || '', frame.status || 'running');
    } else if (frame.type === 'status') {
      stopLiveStream();
      refreshOutputForRun(jobId, runId).catch(function () {});
      refreshSheetContent(jobId, {}).catch(function () {});
    }
  });
  socket.addEventListener('close', function () {
    if (liveSocket === socket) {
      liveSocket = null;
      liveSocketKey = null;
    }
  });
}

function stopLiveStream() {
  const socket = liveSocket;
  liveSocket = null;
  liveSocketKey = null;
  if (socket && (socket.readyState === WebSocket.CONNECTING ||
                 socket.readyState === WebSocket.OPEN)) {
    socket.close(1000, 'view changed');
  }
}

async function toggleRunPin(jobId, run) {
  const next = !run.pinned;
  try {
    await jsonApi(
      '/api/jobs/' + encodeURIComponent(jobId) + '/runs/' + encodeURIComponent(run.run_id),
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pinned: next }),
      }
    );
    run.pinned = next;
    const job = findJob(jobId);
    if (job) {
      job.pinned_count = Math.max(0, (job.pinned_count || 0) + (next ? 1 : -1));
      refreshSheetDetails(job);
    }
    redrawRunsList(jobId, state.jobRuns[jobId] || []);
    toast(next ? 'Run pinned.' : 'Run unpinned.', 'good', next ? { icon: 'pin' } : undefined);
  } catch (exc) {
    apiFailToast('Pin update failed', exc);
  }
}

// Kill stuck run, above the runs: only for the selected live run of a job
// the server flags stuck (an older run cannot be running).
function renderKillButton(jobId, runId, status) {
  const panel = panelEl(jobId);
  if (!panel) return;
  let killBtn = panel.querySelector('[data-role="kill-btn"]');
  const job = findJob(jobId);
  const isLive = status === 'running' || status === 'pending';
  const showKill = !!(job && job.stuck && isLive);
  if (!showKill) {
    if (killBtn) killBtn.remove();
    return;
  }
  if (!killBtn) {
    killBtn = document.createElement('button');
    killBtn.type = 'button';
    killBtn.className = 'button-tint danger jobs-kill-btn';
    killBtn.dataset.role = 'kill-btn';
    killBtn.innerHTML = icon('octagon-x') + ' Kill stuck run';
    // Exactly one listener for the button's lifetime, reading the run id the
    // latest render stamped below — re-binding per render stacked a second
    // handler on a stale run id, so one tap killed twice (#881).
    killBtn.addEventListener('click', function () {
      killRun(jobId, killBtn.dataset.runId);
    });
    panel.insertBefore(killBtn, panel.querySelector('[data-role="runs-head"]'));
  }
  killBtn.dataset.runId = runId;
}

async function killRun(jobId, runId) {
  const ok = await confirmDialog({
    title: 'Kill this run?',
    message: 'This ends the running process tree for ' + runId + '.',
    action: 'Kill run',
  });
  if (!ok) return;
  try {
    await jsonApi(
      '/api/jobs/' + encodeURIComponent(jobId) + '/runs/' + encodeURIComponent(runId) + '/kill',
      { method: 'POST' }
    );
    toast('Kill signal sent.', 'good', { icon: 'octagon-x' });
    await refreshSheetContent(jobId, { fetchOutput: true });
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
    toast(job.paused ? 'Resumed ' + job.name : 'Paused ' + job.name, 'good',
      { icon: job.paused ? 'play' : 'pause' });
    await fetchJobs();
    // A paused job's fires leave Next up, a resumed one's come back.
    fetchAgenda().catch(function () {});
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
  // Confirm-on-fire (issue #69), through the vendored confirm sheet since
  // #1438. A flagged job needs explicit confirmation before a real fire; a
  // dry-run "check" is exempt (no side effects). The ?confirmed=1 keeps the
  // server gate honest.
  const isCheck = opts.dryRun === 'check';
  const needConfirm = !!(job && job.confirm) && !isCheck;
  if (needConfirm) {
    const ok = await confirmDialog({
      title: 'Run ' + job.name + '?',
      message: 'This job requires confirmation before running.',
      action: 'Run',
    });
    if (!ok) return;
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
    let started = false;
    if (res && res.dry_run) {
      if (res.status === 'dry_run_failed') {
        toast('Dry-run check failed for ' + job.name + ' — see history.', 'error', { icon: 'flask-conical' });
      } else if (res.status === 'dry_run_success') {
        toast('Dry-run check passed for ' + job.name + '.', 'good', { icon: 'flask-conical' });
      } else {
        toast('Dry-run started for ' + job.name + '.', 'good', { icon: 'flask-conical' });
        job.running = true;
        started = true;
      }
    } else if (res && res.status === 'queued') {
      const blocker = res.mutex_blocked_by ? ' (behind ' + res.mutex_blocked_by + ')' : '';
      toast('Queued ' + job.name + blocker + '.', 'good', { icon: 'hourglass' });
    } else {
      toast('Started ' + job.name + '.', 'good', { icon: 'rocket' });
      job.running = true;
      started = true;
    }
    renderJobs();
    // The sheet showing this job follows the run it just started, so its
    // output streams there (#1438).
    if (state.sheetJob === job.id && sheetOpen()) {
      syncSheetRun(job);
      if (res && res.run_id && (started || res.status === 'queued')) {
        stopLiveStream();
        state.selectedRun = { jobId: job.id, runId: res.run_id };
      }
      refreshSheetContent(job.id, { fetchOutput: true }).catch(function () {});
    }
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
        toast('Skipped — cooled down for ' + remaining + ' more s' + suffix + '.', undefined, { icon: 'timer' });
        return;
      }
    }
    apiFailToast('Run failed', exc);
  }
}

// ------------------------------------------------------------ fetch + wire

export async function fetchJobs() {
  if (state.tab !== 'jobs') return;
  if (state.jobsSearchQuery) {
    await runJobsSearch();
  } else {
    try {
      const body = await jsonApi('/api/jobs');
      state.jobs = body.jobs || [];
      state.jobsLoaded = true;
      renderJobs();
    } catch (exc) {
      logPollFailure('jobs fetch failed', exc);
      if (!state.jobsLoaded && els.jobsLoading) {
        els.jobsLoading.querySelector('.empty-state-message').textContent =
          'Could not load jobs — retrying while this tab is open.';
      }
    }
  }
  // The open sheet refreshes in place the way the accordion did: its facts
  // from the fresh job, then its runs, without stealing the user's run
  // selection (the `opts.poll` guard in refreshSheetContent, #316).
  if (state.sheetJob && sheetOpen()) {
    const job = findJob(state.sheetJob);
    // Removed (from its kebab, beside the docked pane): nothing left to show.
    if (!job) {
      if (state.jobsLoaded && !state.jobsSearchQuery) closeJobSheet();
      return;
    }
    refreshSheetDetails(job);
    await refreshSheetContent(state.sheetJob, { poll: true });
  }
}

export function wireJobs() {
  if (!els.tabJobs) return;
  // Empty-state actions (#1238 J-09): the Add job flow, and the search
  // field's own clear.
  const jobsEmptyAction = document.getElementById('jobsEmptyAction');
  if (jobsEmptyAction) {
    jobsEmptyAction.addEventListener('click', function () { openJobDialog(null); });
  }
  const jobsFilterEmptyAction = document.getElementById('jobsFilterEmptyAction');
  if (jobsFilterEmptyAction) {
    jobsFilterEmptyAction.addEventListener('click', function () {
      if (els.jobsSearchClear) els.jobsSearchClear.click();
    });
  }
  els.tabJobs.addEventListener('click', function () {
    fetchJobs().catch(function () {});
  });
  if (els.jobsSortBtn) {
    els.jobsSortBtn.addEventListener('click', toggleSort);
    // Labelled from the saved order now, not on the first jobs render — the
    // button was blank while the tab loaded (#1176).
    syncSortBtn();
  }
  if (els.jobsSearchInput) {
    els.jobsSearchInput.addEventListener('input', function () {
      if (searchTimer) clearTimeout(searchTimer);
      searchTimer = setTimeout(function () { runJobsSearch(); }, 250);
    });
  }
  if (els.jobsSearchClear) {
    els.jobsSearchClear.addEventListener('click', function (ev) {
      // Inside the field's <label>: keep the click from focusing through it.
      ev.preventDefault();
      if (els.jobsSearchInput) els.jobsSearchInput.value = '';
      runJobsSearch();
      if (els.jobsSearchInput) els.jobsSearchInput.focus();
    });
  }
  if (els.jobSheet) {
    els.jobSheetClose.addEventListener('click', closeJobSheet);
    els.jobSheet.addEventListener('close', onSheetClosed);
    els.jobSheetRun.addEventListener('click', function () {
      const job = findJob(state.sheetJob);
      if (job) runJobNow(job);
    });
    // The docked pane belongs to the Jobs list: leaving the tab closes it.
    document.addEventListener('launcher:tab', function (ev) {
      if (ev.detail && ev.detail.tab !== 'jobs') closeJobSheet();
    });
  }
  wireJobsAgenda();
  wireJobDialogs();
}
