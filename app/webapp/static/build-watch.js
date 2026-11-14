/* Notices that the server moved to a newer build while this page stayed open.
 *
 * Every /static URL carries the fleet content hash as ?v=, so this module's own
 * URL says which build the page booted on; /api/version's asset_hash says which
 * build is live now. A phone PWA that sat in memory across a restart keeps the
 * old shell and modules until it is reloaded, so a differing pair offers a
 * Reload. Nothing reloads by itself: an unsent composer draft must survive.
 *
 * Unknown is never stale: a missing boot stamp (a dev tree), an empty
 * asset_hash, a failed read or a 401 all leave the prompt down.
 */

import { jsonApi } from './api.js';
import { icon } from './_vendored/icons/icons.js';

// A visibility check is skipped when the last good read is younger than this,
// so flicking between apps does not hammer /api/version.
export const CHECK_THROTTLE_MS = 60 * 1000;
// A page left visible and untouched still checks, this often.
export const CHECK_INTERVAL_MS = 5 * 60 * 1000;

// 'stale' only when both hashes are known and differ.
export function verdict(bootHash, liveHash) {
  const known = (h) => typeof h === 'string' && h !== '';
  if (!known(bootHash) || !known(liveHash)) return 'unknown';
  return bootHash === liveHash ? 'current' : 'stale';
}

// Whether a trigger at nowMs should read the server: never read ok, or the
// last ok read is at least the throttle old. Only an ok read counts, so a
// read that landed mid-restart is retried on the next trigger.
export function shouldCheck(nowMs, lastOkMs, throttleMs) {
  if (typeof lastOkMs !== 'number') return true;
  return nowMs - lastOkMs >= throttleMs;
}

// The ?v= stamp the server put on this module's URL.
function bootStamp() {
  try { return new URL(import.meta.url).searchParams.get('v'); } catch (_) { return null; }
}

function buildPrompt(onReload, onDismiss) {
  const root = document.createElement('div');
  root.id = 'buildPrompt';
  root.className = 'build-prompt';
  root.setAttribute('role', 'status');
  root.setAttribute('aria-live', 'polite');

  const text = document.createElement('span');
  text.className = 'build-prompt-text';
  text.textContent = 'New version available';

  const reload = document.createElement('button');
  reload.type = 'button';
  reload.className = 'button-primary build-prompt-reload';
  reload.dataset.track = 'build.reload';
  reload.textContent = 'Reload';
  reload.addEventListener('click', onReload);

  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'icon-button build-prompt-dismiss';
  close.dataset.track = 'build.dismiss';
  close.title = 'Dismiss';
  close.setAttribute('aria-label', 'Dismiss');
  close.innerHTML = icon('x');
  close.addEventListener('click', onDismiss);

  root.append(text, reload, close);
  return root;
}

export function initBuildWatch() {
  const boot = bootStamp();
  if (!boot) return;
  let lastOk = null;
  let dismissedFor = null;
  let prompt = null;
  let inFlight = false;

  function hidePrompt() {
    if (prompt) prompt.hidden = true;
  }

  function showPrompt() {
    if (!prompt) {
      prompt = buildPrompt(
        function () { location.reload(); },
        function () { dismissedFor = prompt.dataset.live; hidePrompt(); },
      );
      document.body.appendChild(prompt);
    }
    prompt.hidden = false;
  }

  async function check() {
    if (inFlight) return;
    inFlight = true;
    try {
      const body = await jsonApi('/api/version');
      lastOk = Date.now();
      const live = body && body.asset_hash;
      if (verdict(boot, live) !== 'stale') {
        hidePrompt();
        return;
      }
      // A dismissed prompt stays down until the server moves to another build.
      if (live === dismissedFor) return;
      showPrompt();
      prompt.dataset.live = live;
    } catch (_) {
      // Unknown, not stale: say nothing and try again on the next trigger.
    } finally {
      inFlight = false;
    }
  }

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) return;
    if (shouldCheck(Date.now(), lastOk, CHECK_THROTTLE_MS)) check();
  });
  setInterval(function () {
    if (!document.hidden) check();
  }, CHECK_INTERVAL_MS);
}
