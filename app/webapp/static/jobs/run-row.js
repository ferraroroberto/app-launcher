/* One row of the job sheet's Recent runs list: the run's outcome glyph and
 * its meta line (outcome, age, who fired it, exit code, dry run, who ended
 * it, its params), then Pin and, for a job that declares params, Re-run.
 *
 * The sheet (jobs.js) owns what a tap does; it passes `handlers` in, so this
 * module builds DOM and never imports the sheet back.
 */

import { state } from '../state.js';
import { escapeHtml } from '../api.js';
import { fmtAgo } from '../sessions.js';
import { runOutcome, statusIcon, toEpoch } from './jobs-row.js';
import { icon } from '../_vendored/icons/icons.js';

/* Provenance chip: compact "who fired this" label from the
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

/* "Who ended this run" chip. A bare `failed` covers
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

function formatRunParams(params) {
  if (!params || typeof params !== 'object') return '';
  const keys = Object.keys(params);
  if (!keys.length) return '';
  return keys.map(function (k) {
    const v = params[k];
    return k + '=' + (typeof v === 'string' ? v : JSON.stringify(v));
  }).join(' ');
}

// The run's button: its outcome glyph and meta line; a tap shows its log.
function runButton(jobId, r, onSelect) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'jobs-run-btn';
  if (state.selectedRun && state.selectedRun.jobId === jobId
      && state.selectedRun.runId === r.run_id) {
    btn.classList.add('selected');
  }
  const iconEl = document.createElement('span');
  // `outcome`, not `status`: an exit code the scheduled-run
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
  // one, the exit code's own meaning is the next best explanation,
  // and for 122/124 it is the whole answer.
  if (r.note) meta.title = r.note;
  else if (r.outcome_reason) meta.title = r.outcome_reason;
  btn.appendChild(meta);
  btn.addEventListener('click', function () { onSelect(r.run_id); });
  return btn;
}

/* `job` is the job's current registry entry (null once removed) and
 * `declaredNames` the param names it declares now, so Re-run can drop keys
 * the job no longer takes. `handlers` is { onSelect(runId), onPin(run),
 * onRerun(job, options) }. */
export function renderRunRow(jobId, r, job, declaredNames, handlers) {
  const li = document.createElement('li');
  li.appendChild(runButton(jobId, r, handlers.onSelect));

  const pin = document.createElement('button');
  pin.type = 'button';
  pin.className = 'icon-button jobs-pin-btn' + (r.pinned ? ' selected' : '');
  pin.innerHTML = icon('pin');
  pin.title = r.pinned ? 'Unpin run' : 'Pin run — keep forever';
  pin.setAttribute('aria-label', pin.title);
  pin.setAttribute('aria-pressed', r.pinned ? 'true' : 'false');
  pin.addEventListener('click', function (ev) {
    ev.stopPropagation();
    handlers.onPin(r);
  });
  li.appendChild(pin);

  // Re-run button — only meaningful when the job declares
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
      handlers.onRerun(job, { prefill: prefill, staleKeys: stale });
    });
    li.appendChild(rerun);
  }
  return li;
}
