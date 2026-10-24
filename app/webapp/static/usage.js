/* Usage log wiring: which views and controls the owner uses, on which
 * device class — see _vendored/usage-log/README.md for the contract.
 *
 * The loopback PC bypasses the bearer token, a phone on the tunnel or tailnet
 * does not, so the client's one endpoint needs the token or the phone's events
 * would 401 silently and the log would only ever describe the desktop. The
 * vendored client takes it through its `headers` option (a function, so a
 * rotated token is read on every send); nothing here replaces window.fetch or
 * navigator.sendBeacon, and no other request is touched.
 */

import { initUsageLog } from './_vendored/usage-log/usage-log.js';
import { authHeaders } from './api.js';

export const USAGE_ENDPOINT = '/api/usage/events';

// Start the logger once, before the tabs are wired so the restored tab counts
// as the first view. tabs.js announces every switch, Settings included, as a
// `launcher:tab` event; its tab name is the view id.
export function wireUsageLog() {
  const usage = initUsageLog({ headers: function () { return authHeaders(); } });
  document.addEventListener('launcher:tab', function (e) {
    usage.trackView(e.detail.tab);
  });
}
