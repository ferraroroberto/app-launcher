/* One lifecycle-state block for a data-backed list (design.md "Async
 * data & feedback"). A list declares loading / empty / error / stale / ready
 * through one host element beside it, never a blank pane:
 *
 *   loading -> the empty-state block saying what it reads ("Reading apps…")
 *   empty   -> the same block, the true-empty reason
 *   error   -> the same block, a one-line reason and at most one Retry; for a
 *              first fetch that failed, so the list has nothing to show
 *   stale   -> one muted line, "Last updated 14:05 · live data unavailable",
 *              while the list keeps its last good data (a later poll failed)
 *   ready   -> the host hides
 *
 * The block is the vendored empty-state's markup (glyph, message, action)
 * drawn into the host, so the host carries the .empty-state look itself.
 * Repeating the same state is a no-op: a poll that fails every 5 s must not
 * rebuild the Retry button under a keyboard user's focus. Life's
 * Conversations group uses it too; the jobs-agenda block
 * (setNextUpState) predates it and moves onto it with its own step.
 */

import { icon } from './_vendored/icons/icons.js';

const GLYPH = { loading: 'hourglass', empty: 'inbox', error: 'triangle-alert' };

// "Last updated 14:05 · live data unavailable" for a stale list.
export function staleMessage(fetchedAt) {
  if (!fetchedAt) return 'Live data unavailable';
  const t = new Date(fetchedAt);
  const hh = String(t.getHours()).padStart(2, '0');
  const mm = String(t.getMinutes()).padStart(2, '0');
  return 'Last updated ' + hh + ':' + mm + ' · live data unavailable';
}

// host: the state element; name: one of the five states; opts: { message,
// glyph, retry } (retry -> a Retry action, error only).
export function setAsyncState(host, name, opts) {
  if (!host) return;
  const o = opts || {};
  host.dataset.state = name;
  host.hidden = name === 'ready';
  if (name === 'ready') {
    delete host.dataset.sig;
    host.replaceChildren();
    return;
  }
  const retry = name === 'error' && o.retry;
  const sig = [name, o.message || '', retry ? 'retry' : ''].join('|');
  if (host.dataset.sig === sig) return;
  host.dataset.sig = sig;
  host.classList.toggle('empty-state', name !== 'stale');
  host.classList.toggle('async-stale', name === 'stale');
  if (name === 'stale') {
    host.textContent = o.message || '';
    return;
  }
  host.innerHTML = icon(o.glyph || GLYPH[name] || 'inbox', 'empty-state-icon');
  const msg = document.createElement('p');
  msg.className = 'empty-state-message';
  msg.textContent = o.message || '';
  host.appendChild(msg);
  if (retry) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'empty-state-action async-retry';
    btn.textContent = 'Retry';
    btn.addEventListener('click', o.retry);
    host.appendChild(btn);
  }
}

// The usual wiring around one fetch: loading on a first read, ready after a
// good one, and after a failure either the error (nothing read yet) or the
// stale line over the last good data. `loaded` says whether a good read has
// ever landed; `fetchedAt` is its time.
export function failedFetchState(host, loaded, fetchedAt, what, retry) {
  if (loaded) setAsyncState(host, 'stale', { message: staleMessage(fetchedAt) });
  else setAsyncState(host, 'error', { message: 'Could not load ' + what + '.', retry: retry });
}
