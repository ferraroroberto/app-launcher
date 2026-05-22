/* Coding options card: a collapsible panel (collapsed by default) with a
 * Claude Code subsection (model + effort + verbose/debug + flags preview),
 * an Antigravity subsection (skip-permissions + sandbox toggles), and a
 * GitHub Copilot subsection (model picker + skip-permissions toggle).
 *
 * `patchConfig` round-trips through GET /api/config so the SPA's view of
 * config stays a single source of truth — server-computed flags + the
 * `models_available` / `efforts_available` enums included.
 */

import { els, state } from './state.js';
import { jsonApi, toast } from './api.js';

export async function fetchConfig() {
  const body = await jsonApi('/api/config');
  state.config = body;
  els.projectsDir.value = body.projects_dir || '';
  els.projectsIgnore.value = (body.projects_ignore || []).join('\n');
  els.appsScanRoot.value = body.apps_scan_root || '';
  renderClaudeOptions();
}

export function renderClaudeOptions() {
  renderClaudeSubsection();
  renderAntigravitySubsection();
  renderCopilotSubsection();
}

function renderClaudeSubsection() {
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
  els.claudePermission.innerHTML = '';
  (c.permission_modes_available || []).forEach(function (p) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = p === 'skip' ? 'Skip permissions' : 'Auto mode';
    b.dataset.value = p;
    if (p === c.permission_mode) b.classList.add('active');
    b.addEventListener('click', function () {
      patchConfig({ claude_permission_mode: p });
    });
    els.claudePermission.appendChild(b);
  });
  els.claudeVerbose.checked = !!c.verbose;
  els.claudeDebug.checked = !!c.debug;
  els.claudeFlagsPreview.textContent = 'claude ' + (c.computed_flags || '');
}

function renderAntigravitySubsection() {
  const a = state.config && state.config.antigravity;
  if (!a) return;
  els.antigravitySkipPerms.checked = !!a.skip_permissions;
  els.antigravitySandbox.checked = !!a.sandbox;
  // The Antigravity CLI has no model/effort flags — the preview is just
  // the bare command plus whichever of the two toggles are on.
  els.antigravityFlagsPreview.textContent =
    'agy' + (a.computed_flags ? ' ' + a.computed_flags : '');
}

function renderCopilotSubsection() {
  const c = state.config && state.config.copilot;
  if (!c) return;
  // Model picker — a <select>: the Copilot CLI offers ~15 models, too
  // many for a segmented control. The empty-value "Default" option
  // launches without --model (the CLI uses its own configured model).
  els.copilotModel.innerHTML = '';
  const optDefault = document.createElement('option');
  optDefault.value = '';
  optDefault.textContent = 'Default';
  els.copilotModel.appendChild(optDefault);
  (c.models_available || []).forEach(function (m) {
    const opt = document.createElement('option');
    opt.value = m;
    opt.textContent = m;
    els.copilotModel.appendChild(opt);
  });
  els.copilotModel.value = c.model || '';
  els.copilotSkipPerms.checked = !!c.skip_permissions;
  els.copilotFlagsPreview.textContent =
    'copilot' + (c.computed_flags ? ' ' + c.computed_flags : '');
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
  els.antigravitySkipPerms.addEventListener('change', function () {
    patchConfig({ antigravity_skip_permissions: els.antigravitySkipPerms.checked });
  });
  els.antigravitySandbox.addEventListener('change', function () {
    patchConfig({ antigravity_sandbox: els.antigravitySandbox.checked });
  });
  els.copilotSkipPerms.addEventListener('change', function () {
    patchConfig({ copilot_skip_permissions: els.copilotSkipPerms.checked });
  });
  els.copilotModel.addEventListener('change', function () {
    patchConfig({ copilot_model: els.copilotModel.value });
  });
  // The ☁️ Detached toggle lives in the card's <summary> so it stays
  // visible when the panel is collapsed — but a click there would also
  // expand/collapse the <details>. Stop the click at the toggle so it
  // only flips the checkbox.
  const detachedLabel = els.claudeDetached.closest('.detached-toggle');
  if (detachedLabel) {
    detachedLabel.addEventListener('click', function (ev) {
      ev.stopPropagation();
    });
  }
}
