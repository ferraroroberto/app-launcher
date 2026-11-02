/* Settings > Hide shut down over Remote Desktop.
 *
 * The state is GET /api/config's live `remote_power_guard` block (Task
 * Scheduler and the registry, read on every fetch), so the switch never
 * claims a position it cannot back: an unreadable state paints as `mixed`
 * with a reason, never as off. A tap tries the change; the webapp runs
 * unelevated, so Windows usually refuses and the 409 carries the one
 * command to run in an elevated terminal, which opens the command sheet
 * instead of a failure toast.
 */
import { els, state } from '../state.js';
import { apiFailToast, escapeHtml, jsonApi, toast } from '../api.js';
import { setSwitch } from '../_vendored/switch/switch.js';
import { showPath } from './settings-sheets.js';

const SHEET_ID = 'remotePowerGuardSheet';

function guardOf(body) {
  return (body && body.remote_power_guard) || null;
}

function statusHtml(guard) {
  if (!guard || guard.state === 'unknown') {
    return 'The guard’s state could not be read from Task Scheduler, so it is shown as unknown.';
  }
  if (guard.state === 'off' && guard.no_close === 1) {
    return 'The guard is off, but Shut down and Restart are still hidden for your Windows user. ' +
      'To bring them back, run this in an elevated terminal: <code>' +
      escapeHtml(guard.command_disable || '') + '</code>';
  }
  return '';
}

export function renderRemotePowerGuard(body) {
  const btn = els.remotePowerGuardToggle;
  if (!btn) return;
  const guard = guardOf(body);
  const known = guard && guard.state !== 'unknown';
  setSwitch(btn, !!known && guard.state === 'on');
  if (!known) {
    btn.setAttribute('aria-checked', 'mixed');
    btn.querySelector('.toggle-label').textContent = 'UNKNOWN';
  }
  const status = els.remotePowerGuardStatus;
  if (!status) return;
  const html = statusHtml(guard);
  status.innerHTML = html;
  status.hidden = !html;
}

function openCommandSheet(command, turningOn) {
  const code = document.getElementById('remotePowerGuardCommand');
  const lead = document.getElementById('remotePowerGuardSheetLead');
  if (code) code.textContent = command || '';
  if (lead) {
    lead.textContent = 'Windows lets only an administrator ' +
      (turningOn ? 'turn this on' : 'turn this off') +
      '. On the PC, open PowerShell with Run as administrator and paste:';
  }
  showPath([SHEET_ID]);
}

async function copyCommand() {
  const code = document.getElementById('remotePowerGuardCommand');
  const host = document.getElementById('remotePowerGuardToast');
  try {
    await navigator.clipboard.writeText(code ? code.textContent : '');
    toast('Command copied', 'good', { host: host });
  } catch (exc) {
    toast('Could not copy: select the command and copy it by hand', 'error', { host: host });
  }
}

async function toggle(refresh) {
  const guard = guardOf(state.config);
  const next = !(guard && guard.state === 'on');
  try {
    await jsonApi('/api/settings/remote-power-guard', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: next }),
    });
    await refresh();
    toast(next ? 'Shut down is hidden over Remote Desktop.' : 'Remote power guard is off.', 'good');
  } catch (exc) {
    const detail = exc && exc.body && exc.body.detail;
    if (exc && exc.status === 409 && detail && detail.elevation_required) {
      openCommandSheet(detail.command, next);
      return;
    }
    apiFailToast('Hide shut down failed', exc);
  }
}

// `refresh` re-fetches /api/config after a change that did land, so the
// switch shows what Task Scheduler now says rather than an optimistic flip.
export function wireRemotePowerGuard(refresh) {
  if (els.remotePowerGuardToggle) {
    els.remotePowerGuardToggle.addEventListener('click', function () { toggle(refresh); });
  }
  const copy = document.getElementById('remotePowerGuardCopy');
  if (copy) copy.addEventListener('click', copyCommand);
}
