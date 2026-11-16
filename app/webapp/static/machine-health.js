/* The machine rows of the PC load sheet: "which process is eating the ports?"
 * one tap from the header's CPU reading.
 *
 * The hub's verdict (GET /api/system/health, read by system-load.js on the
 * page's one poll) reaches the header as a dot and the sheet's Health row
 * there. This module adds what the hub measured under that row: the ports and
 * commit percents, the top port holders, the last exhaustion event and a link
 * to the hub (appendMachineRows). The hub does the judging and alerting.
 */

import { el, sheetRow } from './dom-utils.js';
import { agoFromSeconds, pctText, plural } from './shared/format.js';

function isNum(v) {
  return typeof v === 'number' && !isNaN(v);
}

function holderText(h) {
  const n = isNum(h.value) ? h.value : 0;
  const split = isNum(h.tcp) || isNum(h.udp)
    ? ' (' + (h.tcp || 0) + ' TCP, ' + (h.udp || 0) + ' UDP)' : '';
  return plural(n, 'port', 'ports') + split;
}

function holderLabel(h) {
  return (h.name || 'Unknown') + (isNum(h.pid) ? ' · ' + h.pid : '');
}

// The sheet's rows under the verdict: the percents, the top port holders, the
// last exhaustion event the hub logged, and the hub link on the PC itself.
export function appendMachineRows(body, health, nowMs) {
  const m = health && health.machine;
  if (!m) return;
  if (isNum(m.ports_percent)) body.appendChild(sheetRow('Ports in use', pctText(m.ports_percent), 'ports'));
  if (isNum(m.commit_percent)) body.appendChild(sheetRow('Commit', pctText(m.commit_percent), 'commit'));
  if (m.holders && m.holders.length) {
    body.appendChild(el('p', 'cpu-sheet-caption', 'Top port holders'));
    m.holders.forEach(function (h, i) {
      body.appendChild(sheetRow(holderLabel(h), holderText(h), 'holder-' + i));
    });
  }
  const ev = m.last_event;
  if (ev && ev.reason) {
    const now = nowMs === undefined ? Date.now() : nowMs;
    const ago = isNum(ev.ts) ? agoFromSeconds(now / 1000 - ev.ts) : '';
    body.appendChild(sheetRow('Last exhaustion', ago || 'Unknown time', 'last-event'));
    body.appendChild(el('p', 'cpu-sheet-note', ev.reason));
  }
  if (health.hub_url) {
    const link = el('a', 'cpu-sheet-link', "Open the hub's Machines tab");
    link.href = health.hub_url;
    link.target = '_blank';
    link.rel = 'noopener';
    link.dataset.track = 'machine.hub-link';
    body.appendChild(link);
  }
}
