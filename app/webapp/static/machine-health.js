/* The machine-health row: "is the box short of ports or memory?" at a glance,
 * beside the usage meter, and "which process?" one tap away.
 *
 * It draws the local-llm-hub's verdict (GET /api/system/health, read by
 * system-load.js on the page's one poll): the verdict word and the worse of
 * the ports and commit percents. Tone follows the verdict: ok, watch
 * (attention), act (danger). A verdict that could not be read is the words
 * "Not confirmed", never a 0%; before the first answer it reads "Reading…".
 *
 * Tapping the row opens the PC load sheet, whose Health section gains the
 * port and commit rows, the top port holders, the last exhaustion event and a
 * link to the hub (appendMachineRows). The hub does the judging and alerting.
 *
 * machineReading is pure and touches no DOM, so tests/js/machine_health.test.mjs
 * pins it under plain Node.
 */

import { el, sheetRow } from './dom-utils.js';
import { agoFromSeconds, pctText, plural } from './shared/format.js';

const VERDICT_WORDS = { ok: 'OK', watch: 'Watch', act: 'Needs action' };
const VERDICT_TONES = { ok: 'ok', watch: 'attention', act: 'danger' };

function isNum(v) {
  return typeof v === 'number' && !isNaN(v);
}

// The worse of the two measured percents, as { name, pct }; null when neither
// was measured (a stale port block or a missing commit reading is not 0%).
export function worstMetric(machine) {
  const m = machine || {};
  const found = [];
  if (isNum(m.ports_percent)) found.push({ name: 'Ports', pct: m.ports_percent });
  if (isNum(m.commit_percent)) found.push({ name: 'Commit', pct: m.commit_percent });
  if (!found.length) return null;
  return found.reduce(function (a, b) { return b.pct > a.pct ? b : a; });
}

// What the row shows for the page's last answer: undefined before the first
// answer, null when the launcher itself could not be asked.
export function machineReading(health) {
  if (health === undefined) return { tone: 'none', value: 'Reading…' };
  if (!health || !VERDICT_WORDS[health.verdict]) {
    return { tone: 'none', value: 'Not confirmed' };
  }
  const word = VERDICT_WORDS[health.verdict];
  const worst = worstMetric(health.machine);
  return {
    tone: VERDICT_TONES[health.verdict],
    value: worst ? word + ' · ' + worst.name + ' ' + pctText(worst.pct) : word,
  };
}

export function renderMachineRows(health) {
  const r = machineReading(health);
  document.querySelectorAll('.machine-health').forEach(function (btn) {
    btn.dataset.tone = r.tone;
    const value = btn.querySelector('.mh-value');
    if (value) value.textContent = r.value;
    btn.setAttribute('aria-label', 'Machine health: ' + r.value.toLowerCase() + '. Show details');
  });
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
