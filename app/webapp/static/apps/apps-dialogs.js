/* Apps tab dialogs: rename an existing registry entry, and the scan dialog
 * that offers newly discovered launcher bats for adoption.
 *
 * Split out of apps.js. Both dialogs are self-contained —
 * open, submit, re-fetch the registry — and share nothing with the list
 * rendering beyond fetchApps.
 */

import { els, state } from '../state.js';
import { apiFailToast, jsonApi, toast } from '../api.js';
import { setSwitch, switchEl } from '../_vendored/switch/switch.js';
import { fetchApps } from '../apps.js';
import { nameLabel } from '../dom-utils.js';

// ----------------------------------------------------------- rename dialog
let renameTargetId = null;

export function openRename(a) {
  renameTargetId = a.id;
  els.renameInput.value = a.name;
  els.renameDialog.showModal();
}

export function wireRenameDialog() {
  els.renameCancel.addEventListener('click', function () {
    els.renameDialog.close();
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
      els.renameDialog.close();
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
    els.scanDialog.showModal();
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
    h.textContent = nameLabel(kind);
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
  // The Apps card's last row (moved here from Settings › Folders),
  // and the empty registries straight from their empty state.
  els.appsScanRow.addEventListener('click', runScan);
  ['appsEmptyAction', 'registeredTraysEmptyAction'].forEach(function (id) {
    const btn = document.getElementById(id);
    if (btn) btn.addEventListener('click', runScan);
  });
  els.scanCancel.addEventListener('click', function () {
    els.scanDialog.close();
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
      els.scanDialog.close();
      await fetchApps();
    } catch (exc) {
      apiFailToast('Save failed', exc);
    }
  });
}
