/* Terminal image paste and drag-and-drop onto the xterm host.
 *
 * An image pasted or dropped straight onto the terminal goes the same way
 * as one picked through the composer's image button: it is uploaded to the
 * session-host, and the stored path is appended to the composer's text for
 * review before sending (issues #41/#366/#448/#450). The upload itself and
 * the append live in composer.js / terminal-compose.js since #980; this is
 * only the host's two extra entry points.
 *
 * Split out of terminal.js in issue #723, continuing the #315 split.
 */

import { els } from './state.js';
import { terminalComposer } from './terminal-compose.js';

export function wireTerminalImage() {
  els.terminalHost.addEventListener('paste', function (ev) {
    const items = (ev.clipboardData && ev.clipboardData.items) || [];
    for (let i = 0; i < items.length; i++) {
      if (items[i].type && items[i].type.indexOf('image') === 0) {
        const file = items[i].getAsFile();
        if (file) {
          ev.preventDefault();
          terminalComposer.attachFiles([file]);
          return;
        }
      }
    }
  });
  els.terminalHost.addEventListener('dragover', function (ev) {
    ev.preventDefault();
  });
  els.terminalHost.addEventListener('drop', function (ev) {
    const file = ev.dataTransfer && ev.dataTransfer.files &&
      ev.dataTransfer.files[0];
    if (file && file.type && file.type.indexOf('image') === 0) {
      ev.preventDefault();
      terminalComposer.attachFiles([file]);
    }
  });
}
