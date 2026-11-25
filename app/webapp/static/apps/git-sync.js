/* The Projects card's remote side: fetch, Pull, Sync all and the pull-first
 * gate in front of a launch on a repo that is behind.
 *
 * The server owns every rule (src/git_upstream.py): only a clean repo on its
 * default branch that is only behind is fast-forwarded, every other one is
 * skipped with its reason. This module asks and reports; it decides nothing
 * the server would not. The wording of a run is git-sync-model.js.
 */

import { state } from '../state.js';
import { apiFailToast, jsonApi, toast } from '../api.js';
import { chip } from '../glance.js';
import { confirmDialog } from '../confirm-dialog.js';
import { setAsyncState } from '../async-state.js';
import { plural } from '../shared/format.js';
import { isRunning, sortResults, syncProgress, syncSummary } from './git-sync-model.js';


// A repo the server would fast-forward: clean, on its default branch, behind
// and not ahead.
export function canPull(gs) {
  const up = gs && gs.upstream;
  return !!(up && up.behind > 0 && !up.ahead && !gs.dirty && gs.on_default_branch && gs.branch);
}

// Why a behind repo cannot be pulled from here, in the words the sheet uses.
function whyNotPullable(gs) {
  if (gs.dirty) return 'it has uncommitted changes';
  if (!gs.branch || !gs.on_default_branch) return 'it is not on ' + (gs.default_branch || 'its default branch');
  if (gs.upstream.ahead) return 'it has local commits too (diverged)';
  return 'the server would skip it';
}

// The fetch behind the Projects refresh: every repo's origin, then the
// caller re-reads git status. A failed fetch shows on its tile as unknown.
export async function fetchAll() {
  const body = await jsonApi('/api/claude-code/git-fetch', { method: 'POST' });
  if (body && body.failed) toast(plural(body.failed, 'repo', 'repos') + ' could not be fetched', 'warn');
  return body;
}

// Fast-forward one project. Resolves true only when it ends up current.
export async function pullProject(a) {
  try {
    const r = await jsonApi('/api/claude-code/pull/' + encodeURIComponent(a.id), { method: 'POST' });
    if (r.outcome === 'pulled') {
      toast('Pulled ' + plural(r.commits, 'commit', 'commits') + ' into ' + a.name, 'good');
      return true;
    }
    if (r.outcome === 'up-to-date') {
      toast(a.name + ' is already up to date', 'ok');
      return true;
    }
    toast('Did not pull ' + a.name + ': ' + r.reason, 'warn');
    return false;
  } catch (exc) {
    apiFailToast('Pull failed', exc);
    return false;
  }
}

// In front of a Coding launch. Resolves null to cancel, else the launch's
// extra payload: {} for a repo that is not behind (or was just pulled), or
// {ignore_behind: true} when the owner chose to launch on a behind repo that
// cannot be pulled here. The server refuses the launch either way without it.
export async function behindGate(a) {
  const gs = state.gitStatus && state.gitStatus[a.id];
  const up = gs && gs.upstream;
  if (!up || !(up.behind > 0)) return {};
  const branch = gs.default_branch || 'main';
  const lag = branch + ' is ' + plural(up.behind, 'commit', 'commits') + ' behind origin/' + branch + '.';
  if (canPull(gs)) {
    const ok = await confirmDialog({
      title: 'Pull ' + a.name + ' first?',
      message: lag + ' Pull it, then launch.',
      action: 'Pull and launch',
      tone: 'primary',
    });
    if (!ok) return null;
    return (await pullProject(a)) ? {} : null;
  }
  const anyway = await confirmDialog({
    title: a.name + ' is behind',
    message: lag + ' It cannot be pulled here: ' + whyNotPullable(gs) + '.',
    action: 'Launch anyway',
  });
  return anyway ? { ignore_behind: true } : null;
}

const OUTCOME_CHIP = {
  pulled: function (r) { return chip('pulled ' + r.commits, 'accent'); },
  'up-to-date': function () { return chip('up to date', 'neutral'); },
  skipped: function (r) { return chip(r.reason, 'attention'); },
  failed: function () { return chip('failed', 'danger'); },
};

function renderResults(host, results) {
  host.replaceChildren();
  sortResults(results).forEach(function (r) {
    const li = document.createElement('li');
    li.dataset.outcome = r.outcome;
    const name = document.createElement('span');
    name.className = 'git-sync-name';
    name.textContent = r.name;
    li.appendChild(name);
    li.appendChild((OUTCOME_CHIP[r.outcome] || OUTCOME_CHIP.skipped)(r));
    // A failure's error is the one thing a person can act on, so it reads in
    // full on its own line rather than clipped inside a chip.
    if (r.outcome === 'failed' && r.reason) {
      const why = document.createElement('span');
      why.className = 'git-sync-why';
      why.textContent = r.reason;
      li.appendChild(why);
    }
    host.appendChild(li);
  });
}

// How often the run is re-read while it works (the count moves at this pace),
// how many reads in a row may fail before the dialog gives up, and how long
// the closing toast stays (longer when it is red).
const POLL_MS = 2000;
const MAX_READ_FAILURES = 3;
const SUMMARY_MS = 8000;
const SUMMARY_ERROR_MS = 15000;

let syncing = false;

function pause(ms) {
  return new Promise(function (resolve) { setTimeout(resolve, ms); });
}

// The neutral toast keeps the count on screen when the dialog is closed.
function progressToast(message) {
  toast(message, '', { icon: 'refresh-cw', ms: 0 });
}

// Follow the run the server started until it finishes. The dialog and the toast
// say the same line each read; a read that fails is retried a few times before
// the run is reported as lost (it may still be working on the server).
async function followRun(first, status) {
  let run = first;
  let failures = 0;
  while (isRunning(run)) {
    const line = syncProgress(run);
    setAsyncState(status, 'loading', { message: line });
    progressToast(line);
    await pause(POLL_MS);
    try {
      run = (await jsonApi('/api/claude-code/sync-all')).run;
      failures = 0;
    } catch (exc) {
      failures += 1;
      if (failures >= MAX_READ_FAILURES) throw exc;
    }
  }
  return run;
}

// Sync all: the server fetches every repo and fast-forwards the safe ones in
// the background; the dialog and a toast count along ("Syncing 3 of 26 · name"),
// then the dialog holds the report until Done. `onDone` re-reads git status.
// Tapping Sync all again while it runs just brings the dialog back.
export async function syncAll(onDone) {
  const sheet = document.getElementById('gitSyncSheet');
  if (!sheet) return;
  if (syncing) {
    if (!sheet.open) sheet.showModal();
    return;
  }
  const status = document.getElementById('gitSyncState');
  const summary = document.getElementById('gitSyncSummary');
  const body = document.getElementById('gitSyncBody');
  syncing = true;
  body.replaceChildren();
  summary.hidden = true;
  setAsyncState(status, 'loading', { message: syncProgress(null) });
  progressToast(syncProgress(null));
  if (!sheet.open) sheet.showModal();
  try {
    const started = await jsonApi('/api/claude-code/sync-all', { method: 'POST' });
    const run = await followRun(started.run, status);
    const done = syncSummary(run);
    const results = run.results || [];
    if (run.error) {
      setAsyncState(status, 'error', { message: done.text });
    } else if (!results.length) {
      setAsyncState(status, 'empty', { message: 'No git repos found.' });
    } else {
      setAsyncState(status, 'ready');
      summary.textContent = done.text;
      summary.dataset.kind = done.kind;
      summary.hidden = false;
      renderResults(body, results);
    }
    const red = done.kind === 'error';
    toast(done.text, done.kind, { icon: red ? 'triangle-alert' : 'refresh-cw', ms: red ? SUMMARY_ERROR_MS : SUMMARY_MS });
  } catch (exc) {
    const message = 'Sync all failed: ' + exc.message;
    setAsyncState(status, 'error', { message: message });
    toast(message, 'error', { ms: SUMMARY_ERROR_MS });
  } finally {
    syncing = false;
    if (onDone) onDone();
  }
}

export function wireGitSync() {
  const sheet = document.getElementById('gitSyncSheet');
  if (!sheet) return;
  const close = function () { if (sheet.open) sheet.close(); };
  document.getElementById('gitSyncClose').addEventListener('click', close);
  document.getElementById('gitSyncDone').addEventListener('click', close);
}
