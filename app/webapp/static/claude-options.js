/* Claude options card: model + effort + verbose/debug toggles + flags preview.
 *
 * `patchConfig` rounds-trips through GET /api/config so the SPA's view of
 * config stays a single source of truth — server-computed flags + the
 * `models_available` / `efforts_available` enums included.
 */

import { els, state } from './state.js';
import { jsonApi, toast } from './api.js';

export async function fetchConfig() {
  const body = await jsonApi('/api/config');
  state.config = body;
  els.projectsDir.value = body.projects_dir || '';
  els.appsScanRoot.value = body.apps_scan_root || '';
  renderClaudeOptions();
}

export function renderClaudeOptions() {
  const c = state.config && state.config.claude;
  if (!c) return;
  els.claudeModel.innerHTML = '';
  (c.models_available || []).forEach(function (m) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = m.charAt(0).toUpperCase() + m.slice(1);
    b.dataset.value = m;
    if (m === c.model) b.classList.add('active');
    b.addEventListener('click', function () {
      patchConfig({ claude_model: m });
    });
    els.claudeModel.appendChild(b);
  });
  els.claudeEffort.innerHTML = '';
  (c.efforts_available || []).forEach(function (e) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = e === 'off' ? 'Off' : e.charAt(0).toUpperCase() + e.slice(1);
    b.dataset.value = e;
    if (e === c.effort) b.classList.add('active');
    b.addEventListener('click', function () {
      patchConfig({ claude_effort: e });
    });
    els.claudeEffort.appendChild(b);
  });
  els.claudeVerbose.checked = !!c.verbose;
  els.claudeDebug.checked = !!c.debug;
  els.claudeFlagsPreview.textContent = 'claude ' + (c.computed_flags || '');
}

export async function patchConfig(patch) {
  try {
    await jsonApi('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
    await fetchConfig();
  } catch (exc) {
    toast('Save failed: ' + (exc.message || exc), 'error');
  }
}

export function wireClaudeOptions() {
  els.claudeVerbose.addEventListener('change', function () {
    patchConfig({ claude_verbose: els.claudeVerbose.checked });
  });
  els.claudeDebug.addEventListener('change', function () {
    patchConfig({ claude_debug: els.claudeDebug.checked });
  });
}
