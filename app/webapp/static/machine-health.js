/* The machine rows of the PC load sheet: "which process is eating the ports?"
 * one tap from the header's CPU reading.
 *
 * The hub's verdict (GET /api/system/health, read by system-load.js on the
 * page's one poll) reaches the header as a dot and the sheet's Health row
 * there. This module adds what the hub measured under that row: the ports and
 * commit percents, the load on each physical disk, the top port holders, the
 * last exhaustion event and a link to the hub (appendMachineRows). The hub does the judging and alerting.
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

// A disk's state as a value tone; ok and unknown carry none, and a disk the
// hub could not judge says so in words rather than reading as healthy.
const DISK_TONES = { watch: 'attention', alert: 'danger' };

function msText(v) {
  return (v < 10 ? Math.round(v * 10) / 10 : Math.round(v)) + ' ms';
}

function diskLabel(d) {
  return (d.drives && d.drives.length ? d.drives.join(', ') : d.disk) || 'Disk';
}

// The row's figure stays short so a phone keeps it on one line next to the
// drive letter; the latencies wrap in a note under it.
function diskText(d) {
  if (d.state === 'unknown') return 'Not confirmed';
  const parts = [];
  if (isNum(d.busy_percent)) parts.push(pctText(d.busy_percent) + ' busy');
  if (isNum(d.queue)) parts.push('queue ' + (Math.round(d.queue * 10) / 10));
  return parts.join(' · ') || 'Not confirmed';
}

function diskLatency(d) {
  if (d.state === 'unknown') return '';
  const parts = [];
  if (isNum(d.read_ms)) parts.push('read ' + msText(d.read_ms));
  if (isNum(d.write_ms)) parts.push('write ' + msText(d.write_ms));
  return parts.join(' · ');
}

// The sheet's rows under the verdict: the percents, one row per disk, the top
// port holders, the last exhaustion event the hub logged, and the hub link on
// the PC itself.
export function appendMachineRows(body, health, nowMs) {
  const m = health && health.machine;
  if (!m) return;
  if (isNum(m.ports_percent)) body.appendChild(sheetRow('Ports in use', pctText(m.ports_percent), 'ports'));
  if (isNum(m.commit_percent)) body.appendChild(sheetRow('Commit', pctText(m.commit_percent), 'commit'));
  if (m.disks && m.disks.length) {
    body.appendChild(el('p', 'cpu-sheet-caption', 'Disk load'));
    m.disks.forEach(function (d, i) {
      const row = sheetRow(diskLabel(d), diskText(d), 'disk-' + i);
      if (DISK_TONES[d.state]) row.lastChild.dataset.tone = DISK_TONES[d.state];
      body.appendChild(row);
      const latency = diskLatency(d);
      if (latency) {
        const note = el('p', 'cpu-sheet-note', latency);
        note.dataset.row = 'disk-' + i + '-latency';
        body.appendChild(note);
      }
    });
  }
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
