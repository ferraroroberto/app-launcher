/* Apps tab dialogs: rename an existing registry entry, and the scan dialog
 * that offers newly discovered launcher bats for adoption.
 *
 * Split out of apps.js in issue #723. Both dialogs are self-contained —
 * open, submit, re-fetch the registry — and share nothing with the list
 * rendering beyond fetchApps.
 */

import { els, state } from './state.js';
import { apiFailToast, jsonApi, toast } from './api.js';
import { setSwitch, switchEl } from './_vendored/switch/switch.js';
import { fetchApps } from './apps.js';

// ----------------------------------------------------------- rename dialog
let renameTargetId = null;

export function openRename(a) {
  renameTargetId = a.id;
  els.renameInput.value = a.name;
  if (els.renameDialog.showModal) els.renameDialog.showModal();
}

export function wireRenameDialog() {
  els.renameCancel.addEventListener('click', function () {
    if (els.renameDialog.close) els.renameDialog.close();
  });
  els.renameForm.addEventListener('submit', async function (ev) {
    ev.preventDefault();
    const name = els.renameInput.value.trim();
    if (!name || !renameTargetId) return;
    try {
      await jsonApi('/api/apps/' + encodeURIComponent(renameTargetId), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      if (els.renameDialog.close) els.renameDialog.close();
      await fetchApps();
    } catch (exc) {
      apiFailToast('Rename failed', exc);
    }
  });
}

// ----------------------------------------------------------- scan dialog
async function runScan() {
  try {
    const body = await jsonApi('/api/apps/scan', { method: 'POST' });
    state.pendingScan = body.new || [];
    renderScanResults();
    if (els.scanDialog.showModal) els.scanDialog.showModal();
    else els.scanDialog.hidden = false;
  } catch (exc) {
    apiFailToast('Scan failed', exc);
  }
}

function renderScanResults() {
  els.scanResults.innerHTML = '';
  if (!state.pendingScan.length) {
    const p = document.createElement('p');
    p.className = 'muted small';
    p.textContent = 'No new entries.';
    els.scanResults.appendChild(p);
    return;
  }
  const byKind = {};
  state.pendingScan.forEach(function (c) {
    (byKind[c.kind] = byKind[c.kind] || []).push(c);
  });
  Object.keys(byKind).sort().forEach(function (kind) {
    const section = document.createElement('div');
    section.className = 'scan-section';
    const h = document.createElement('h3');
    h.textContent = kind;
    section.appendChild(h);
    byKind[kind].forEach(function (c) {
      const row = document.createElement('div');
      row.className = 'scan-row';
      const body = document.createElement('div');
      const name = document.createElement('div');
      name.textContent = c.name;
      const meta = document.createElement('div');
      meta.className = 'meta';
      meta.textContent = c.bat_path || c.project_dir || '';
      body.appendChild(name);
      body.appendChild(meta);
      row.appendChild(body);
      const toggleBtn = switchEl(true, {
        label: 'Include ' + c.name,
        onToggle: function (next, btn) { setSwitch(btn, next); },
      });
      toggleBtn.dataset.value = c.id;
      row.appendChild(toggleBtn);
      section.appendChild(row);
    });
    els.scanResults.appendChild(section);
  });
}

export function wireScanDialog() {
  els.rescanBtn.addEventListener('click', runScan);
  els.scanCancel.addEventListener('click', function () {
    if (els.scanDialog.close) els.scanDialog.close();
  });
  els.scanSave.addEventListener('click', async function () {
    const checked = Array.from(
      els.scanResults.querySelectorAll('.scan-row .toggle[aria-checked="true"]')
    );
    const ids = checked.map(function (btn) { return btn.dataset.value; });
    if (!ids.length) {
      toast('Nothing selected.', 'error');
      return;
    }
    try {
      const body = await jsonApi('/api/apps/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids }),
      });
      toast('Added ' + (body.added || []).length + ' entry(ies).', 'good');
      if (els.scanDialog.close) els.scanDialog.close();
      await fetchApps();
    } catch (exc) {
      apiFailToast('Save failed', exc);
    }
  });
}
