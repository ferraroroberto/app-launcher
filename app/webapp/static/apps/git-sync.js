/* The Projects card's remote side: fetch, Pull, Sync all and the pull-first
 * gate in front of a launch on a repo that is behind.
 *
 * The server owns every rule (src/git_upstream.py): only a clean repo on its
 * default branch that is only behind is fast-forwarded, every other one is
 * skipped with its reason. This module asks and reports; it decides nothing
 * the server would not.
 */

import { state } from '../state.js';
import { apiFailToast, jsonApi, toast } from '../api.js';
import { chip } from '../glance.js';
import { confirmDialog } from '../confirm-dialog.js';
import { setAsyncState } from '../async-state.js';
import { plural } from '../shared/format.js';


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
  failed: function (r) { return chip('failed: ' + r.reason, 'danger'); },
};

function renderResults(host, results) {
  host.replaceChildren();
  results.slice().sort(function (x, y) { return x.name.localeCompare(y.name); }).forEach(function (r) {
    const li = document.createElement('li');
    li.dataset.outcome = r.outcome;
    const name = document.createElement('span');
    name.className = 'git-sync-name';
    name.textContent = r.name;
    li.appendChild(name);
    li.appendChild((OUTCOME_CHIP[r.outcome] || OUTCOME_CHIP.skipped)(r));
    host.appendChild(li);
  });
}

let syncing = false;

// Sync all: fetch every repo and fast-forward the safe ones, then the sheet
// lists every repo with its outcome. `onDone` re-reads git status.
export async function syncAll(onDone) {
  const sheet = document.getElementById('gitSyncSheet');
  if (!sheet || syncing) return;
  const status = document.getElementById('gitSyncState');
  const body = document.getElementById('gitSyncBody');
  syncing = true;
  body.replaceChildren();
  setAsyncState(status, 'loading', { message: 'Fetching and fast-forwarding…' });
  if (!sheet.open) sheet.showModal();
  try {
    const reply = await jsonApi('/api/claude-code/sync-all', { method: 'POST' });
    const results = reply.results || [];
    if (!results.length) {
      setAsyncState(status, 'empty', { message: 'No git repos found.' });
    } else {
      const pulled = results.filter(function (r) { return r.outcome === 'pulled'; }).length;
      setAsyncState(status, 'ready');
      renderResults(body, results);
      toast(pulled ? 'Pulled ' + plural(pulled, 'repo', 'repos') : 'Nothing to pull', pulled ? 'good' : 'ok');
    }
  } catch (exc) {
    setAsyncState(status, 'error', { message: 'Sync all failed: ' + exc.message });
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
