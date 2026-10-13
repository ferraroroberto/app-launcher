/* Usage log wiring (#1524): which views and controls the owner uses, on which
 * device class — see _vendored/usage-log/README.md for the contract.
 *
 * The vendored client is never edited, and it sends no Authorization header.
 * The loopback PC bypasses the bearer token, a phone on the tunnel or tailnet
 * does not, so without a header the phone's events would 401 silently and the
 * log would only ever describe the desktop. This module therefore adds the
 * token to the client's one endpoint (and turns its page-hidden sendBeacon,
 * which cannot carry a header, into a keepalive fetch that can). Both
 * wrappers match that exact same-origin path and nothing else, so no other
 * request is touched and nothing is sent to any other origin.
 */

import { initUsageLog } from './_vendored/usage-log/usage-log.js';
import { authHeaders } from './api.js';

export const USAGE_ENDPOINT = '/api/usage/events';

function authorizeUsagePosts() {
  const realFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    if (input !== USAGE_ENDPOINT) return realFetch(input, init);
    const opts = Object.assign({}, init);
    opts.headers = Object.assign({}, opts.headers, authHeaders());
    return realFetch(input, opts);
  };
  if (typeof navigator.sendBeacon === 'function') {
    const realBeacon = navigator.sendBeacon.bind(navigator);
    navigator.sendBeacon = function (url, data) {
      if (url !== USAGE_ENDPOINT) return realBeacon(url, data);
      window.fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: data,
        credentials: 'same-origin',
        keepalive: true,
      }).catch(function () { /* instrumentation never breaks the app */ });
      return true;
    };
  }
}

// Start the logger once, before the tabs are wired so the restored tab counts
// as the first view. tabs.js announces every switch, Settings included, as a
// `launcher:tab` event; its tab name is the view id.
export function wireUsageLog() {
  authorizeUsagePosts();
  const usage = initUsageLog();
  document.addEventListener('launcher:tab', function (e) {
    usage.trackView(e.detail.tab);
  });
}
