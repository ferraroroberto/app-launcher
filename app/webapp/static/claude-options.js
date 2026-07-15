/* Coding options card: a collapsible panel (collapsed by default) with a
 * Claude Code subsection (model + effort + verbose/debug + flags preview),
 * an Antigravity subsection (skip-permissions + sandbox toggles), a
 * GitHub Copilot subsection (model picker + skip-permissions toggle), and a
 * Pi subsection (segmented model / effort / project-trust controls — Opus and
 * Sonnet run on the claude-agent-sdk subscription path, GPT on the openai-codex
 * ChatGPT-plan path, so the provider/model are always passed explicitly).
 *
 * `patchConfig` round-trips through GET /api/config so the SPA's view of
 * config stays a single source of truth — server-computed flags + the
 * `models_available` / `efforts_available` enums included.
 */

import { els, state } from './state.js';
import { apiFailToast, jsonApi } from './api.js';
import { toggleAriaChecked } from './dom-utils.js';

export async function fetchConfig() {
  const body = await jsonApi('/api/config');
  state.config = body;
  els.projectsDir.value = body.projects_dir || '';
  els.projectsIgnore.value = (body.projects_ignore || []).join('\n');
  els.appsScanRoot.value = body.apps_scan_root || '';
  if (els.lifeOsDir) els.lifeOsDir.value = body.life_os_dir || '';
  if (els.claudeConfigDir) els.claudeConfigDir.value = body.claude_config_dir || '';
  if (els.terminalHistoryLines) {
    if (body.terminal_history_lines_min != null) {
      els.terminalHistoryLines.min = body.terminal_history_lines_min;
    }
    if (body.terminal_history_lines_max != null) {
      els.terminalHistoryLines.max = body.terminal_history_lines_max;
    }
    els.terminalHistoryLines.value = body.terminal_history_lines || '';
  }
  if (els.bootAutostartToggle) {
    els.bootAutostartToggle.setAttribute(
      'aria-checked', body.boot_autostart_enabled ? 'true' : 'false'
    );
  }
  renderClaudeOptions();
}

export function renderClaudeOptions() {
  renderClaudeSubsection();
  renderCodexSubsection();
  renderAntigravitySubsection();
  renderCopilotSubsection();
  renderPiSubsection();
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
  els.claudeVerbose.setAttribute('aria-checked', c.verbose ? 'true' : 'false');
  els.claudeDebug.setAttribute('aria-checked', c.debug ? 'true' : 'false');
  els.claudeFlagsPreview.textContent = 'claude ' + (c.computed_flags || '');
}

function renderCodexSubsection() {
  const c = state.config && state.config.codex;
  if (!c) return;
  // Reasoning tier — a segmented control mirroring Claude's Effort.
  // Codex has no model tiers, so this is the quality knob (mapped to
  // `model_reasoning_effort` server-side).
  els.codexEffort.innerHTML = '';
  (c.efforts_available || []).forEach(function (e) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = e.charAt(0).toUpperCase() + e.slice(1);
    b.dataset.value = e;
    if (e === c.effort) b.classList.add('active');
    b.addEventListener('click', function () {
      patchConfig({ codex_effort: e });
    });
    els.codexEffort.appendChild(b);
  });
  // Permission mode — auto (no prompts, still sandboxed) vs skip (the
  // all-bypass switch). Same two-state segmented control as Claude.
  els.codexPermission.innerHTML = '';
  (c.permission_modes_available || []).forEach(function (p) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = p === 'skip' ? 'Skip permissions' : 'Auto mode';
    b.dataset.value = p;
    if (p === c.permission_mode) b.classList.add('active');
    b.addEventListener('click', function () {
      patchConfig({ codex_permission_mode: p });
    });
    els.codexPermission.appendChild(b);
  });
  els.codexFlagsPreview.textContent = 'codex ' + (c.computed_flags || '');
}

function renderAntigravitySubsection() {
  const a = state.config && state.config.antigravity;
  if (!a) return;
  els.antigravitySkipPerms.setAttribute('aria-checked', a.skip_permissions ? 'true' : 'false');
  els.antigravitySandbox.setAttribute('aria-checked', a.sandbox ? 'true' : 'false');
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
  els.copilotSkipPerms.setAttribute('aria-checked', c.skip_permissions ? 'true' : 'false');
  els.copilotFlagsPreview.textContent =
    'copilot' + (c.computed_flags ? ' ' + c.computed_flags : '');
}

function renderPiSubsection() {
  const p = state.config && state.config.pi;
  if (!p || !els.piModel) return;
  // Model — a segmented control over three options spanning two subscription
  // providers (Opus/Sonnet on claude-agent-sdk, GPT on openai-codex), mirroring
  // the other agents' button rows. `models_available` carries {value,label} so
  // the buttons read "Opus/Sonnet/GPT" rather than the raw model ids.
  els.piModel.innerHTML = '';
  (p.models_available || []).forEach(function (m) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = m.label;
    b.dataset.value = m.value;
    if (m.value === p.model) b.classList.add('active');
    b.addEventListener('click', function () {
      patchConfig({ pi_model: m.value });
    });
    els.piModel.appendChild(b);
  });
  // Effort — segmented control mapped to `--thinking`, mirroring Claude.
  els.piEffort.innerHTML = '';
  (p.efforts_available || []).forEach(function (e) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = e.charAt(0).toUpperCase() + e.slice(1);
    b.dataset.value = e;
    if (e === p.effort) b.classList.add('active');
    b.addEventListener('click', function () {
      patchConfig({ pi_effort: e });
    });
    els.piEffort.appendChild(b);
  });
  // Project trust — `--approve` (Trust) vs `--no-approve` (Ask). NOT a
  // tool-permission gate (pi has no sandbox); it governs whether pi loads
  // project-local `.pi/` resources.
  els.piTrust.innerHTML = '';
  (p.trust_modes_available || []).forEach(function (t) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = t === 'trust' ? 'Trust' : 'Ask';
    b.dataset.value = t;
    if (t === p.trust_mode) b.classList.add('active');
    b.addEventListener('click', function () {
      patchConfig({ pi_trust_mode: t });
    });
    els.piTrust.appendChild(b);
  });
  els.piFlagsPreview.textContent =
    'pi' + (p.computed_flags ? ' ' + p.computed_flags : '');
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
    apiFailToast('Save failed', exc);
  }
}

// role="switch" buttons (issue #355): click reads the current aria-checked,
// flips it, applies it optimistically, then patchConfig() round-trips
// through GET /api/config, which re-renders from server truth anyway.
function wireBoolSwitch(el, patchKey) {
  el.addEventListener('click', function () {
    const next = toggleAriaChecked(el);
    patchConfig({ [patchKey]: next });
  });
}

export function wireClaudeOptions() {
  wireBoolSwitch(els.claudeVerbose, 'claude_verbose');
  wireBoolSwitch(els.claudeDebug, 'claude_debug');
  wireBoolSwitch(els.antigravitySkipPerms, 'antigravity_skip_permissions');
  wireBoolSwitch(els.antigravitySandbox, 'antigravity_sandbox');
  wireBoolSwitch(els.copilotSkipPerms, 'copilot_skip_permissions');
  els.copilotModel.addEventListener('change', function () {
    patchConfig({ copilot_model: els.copilotModel.value });
  });
  // Pi's model/effort/trust are segmented buttons that wire their own click
  // handlers in renderPiSubsection(), so there's no static listener here.
  // The ☁️ Detached and ↺ Resume toggles are plain client-side switches
  // (no server config — read at session-launch time in apps.js). They live
  // in the Projects card's <summary> (#496 — the launch surface) so they
  // stay visible when the panel is collapsed — but a click there would
  // also expand/collapse the <details>, so stopPropagation lives alongside
  // the flip.
  [els.claudeDetached, els.claudeResume].forEach(function (btn) {
    btn.addEventListener('click', function (ev) {
      ev.stopPropagation();
      toggleAriaChecked(btn);
    });
  });
}
