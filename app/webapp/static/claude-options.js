/* Coding options card: a collapsible panel (collapsed by default) with a
 * Claude Code subsection (model + effort + verbose/debug + flags preview),
 * an Antigravity subsection (skip-permissions + sandbox toggles), a
 * GitHub Copilot subsection (model picker + skip-permissions toggle), and a
 * Pi subsection (model picker + segmented effort / project-trust controls — Opus and
 * Sonnet run on the claude-agent-sdk subscription path, GPT on the openai-codex
 * ChatGPT-plan path, so the provider/model are always passed explicitly).
 *
 * `patchConfig` round-trips through GET /api/config so the SPA's view of
 * config stays a single source of truth — server-computed flags + the
 * `models_available` / `efforts_available` enums included.
 */

import { els, state } from './state.js';
import { apiFailToast, jsonApi } from './api.js';
import { toggleAriaChecked, wireModelCombo } from './dom-utils.js';
import { setBoardDispatchModelOptions } from './board-dispatch.js';
import { setLifeOsModelOptions } from './life-os.js';
import { setSwitch } from './_vendored/switch/switch.js';

// Shared model-picker controllers, created once the DOM exists.
let codingModelCombo = null;
let claudeModelCombo = null;
let codexModelCombo = null;
let copilotModelCombo = null;
let piModelCombo = null;
let codingQuotaSelectionSequence = 0;
let codingQuotaSaveQueue = Promise.resolve();

function announceCodingQuotaSelection(selection, pending) {
  // Clear synchronously before either the config save or provider fetch can
  // finish, so a Claude badge never lingers under a newly-selected Codex row.
  window.dispatchEvent(new CustomEvent('quota-selection-changed', {
    detail: { selection: selection, pending: pending },
  }));
}

async function selectCodingQuota(patch, selection) {
  const sequence = ++codingQuotaSelectionSequence;
  announceCodingQuotaSelection(selection, true);

  // Preserve click order at the server while letting the newest selection own
  // the UI immediately. An older completion must never repaint a newer click.
  const saved = await (
    codingQuotaSaveQueue = codingQuotaSaveQueue.then(function () {
      return saveCodingQuotaPatch(patch, sequence);
    })
  );
  if (sequence !== codingQuotaSelectionSequence) return saved;

  // A rejected save leaves the optimistic control ahead of server truth.
  // Read it back explicitly and settle both the selector and quota owner.
  if (!saved) {
    try {
      await fetchConfig(function () {
        return sequence === codingQuotaSelectionSequence;
      });
    } catch (_exc) {
      if (sequence === codingQuotaSelectionSequence) renderClaudeOptions();
    }
  }
  if (sequence !== codingQuotaSelectionSequence) return saved;
  const persisted = (state.config && state.config.coding_model_choice) || selection;
  announceCodingQuotaSelection(persisted, false);
  return saved;
}

function effortLabel(value) {
  if (value === 'xhigh') return 'Extra high';
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function modelOptions(models, valuePrefix, labelPrefix) {
  return (models || []).map(function (model) {
    const data = typeof model === 'string'
      ? { value: model, label: model, available: true }
      : model;
    return {
      ...data,
      value: (valuePrefix || '') + data.value,
      label: (labelPrefix || '') + (data.label || data.value),
    };
  });
}

function renderSharedModelSelectors() {
  const config = state.config || {};
  const catalog = config.model_catalog || {};
  const options = modelOptions(catalog.claude, 'claude:', 'Claude · ')
    .concat(modelOptions(catalog.codex, 'codex:', 'Codex · '));
  if (codingModelCombo) codingModelCombo.setOptions(options);
  if (codingModelCombo) codingModelCombo.setValue(config.coding_model_choice);
  setLifeOsModelOptions(options);
  setBoardDispatchModelOptions(options);
}

export async function fetchConfig(shouldApply) {
  const body = await jsonApi('/api/config');
  // A caller may own only one optimistic selection generation. Check after
  // the await, immediately before mutating shared state and repainting.
  if (shouldApply && !shouldApply()) return false;
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
    setSwitch(els.bootAutostartToggle, !!body.boot_autostart_enabled);
  }
  renderClaudeOptions();
  return true;
}

export function renderClaudeOptions() {
  renderSharedModelSelectors();
  renderClaudeSubsection();
  renderCodexSubsection();
  renderAntigravitySubsection();
  renderCopilotSubsection();
  renderPiSubsection();
  renderGrokSubsection();
}

// One host, one array of items, the currently-active value, a label
// renderer, and a select callback — every model/effort/permission/trust
// segmented control below (effort, permission, and trust) is this same shape
// (issue #520). `valueFn` defaults to identity for plain-string items.
function renderSegmentedControl(host, items, currentValue, labelFn, onSelect, valueFn) {
  host.innerHTML = '';
  (items || []).forEach(function (item) {
    const value = valueFn ? valueFn(item) : item;
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = labelFn(item);
    b.dataset.value = value;
    if (value === currentValue) b.classList.add('active');
    b.addEventListener('click', function () {
      onSelect(value);
    });
    host.appendChild(b);
  });
}

function renderClaudeSubsection() {
  const c = state.config && state.config.claude;
  if (!c) return;
  // Filter the legacy-compatible Claude enum to the curated phone catalog.
  const surfaced = ((state.config.model_catalog || {}).claude || []).map(function (m) { return m.value; });
  const models = (c.models_available || []).filter(function (model) {
    return surfaced.includes(model);
  });
  if (claudeModelCombo) {
    claudeModelCombo.setOptions(modelOptions(models.map(function (model) {
      return { value: model, label: model.charAt(0).toUpperCase() + model.slice(1) };
    })));
    claudeModelCombo.setValue(c.model);
  }
  // Keep the compact dropdown in lockstep. patchConfig() round-trips through
  // GET /api/config and re-renders this whole subsection, so a change from
  // either control lands here and updates both — no explicit cross-wiring.
  // setValue never fires onChange, so this can't loop.
  if (codingModelCombo && state.config.coding_model_choice) {
    codingModelCombo.setValue(state.config.coding_model_choice);
  }
  renderSegmentedControl(
    els.claudeEffort,
    c.efforts_available,
    c.effort,
    function (e) { return e === 'off' ? 'Default' : effortLabel(e); },
    function (e) { patchConfig({ claude_effort: e }); }
  );
  renderSegmentedControl(
    els.claudePermission,
    c.permission_modes_available,
    c.permission_mode,
    function (p) { return p === 'skip' ? 'Skip permissions' : 'Auto mode'; },
    function (p) { patchConfig({ claude_permission_mode: p }); }
  );
  setSwitch(els.claudeVerbose, !!c.verbose);
  setSwitch(els.claudeDebug, !!c.debug);
  els.claudeFlagsPreview.textContent = 'claude ' + (c.computed_flags || '');
}

function renderCodexSubsection() {
  const c = state.config && state.config.codex;
  if (!c) return;
  if (codexModelCombo) {
    codexModelCombo.setOptions(modelOptions(c.models_available));
    codexModelCombo.setValue(c.model);
  }
  renderSegmentedControl(
    els.codexEffort,
    c.efforts_available,
    c.effort,
    effortLabel,
    function (e) { patchConfig({ codex_effort: e }); }
  );
  // Permission mode — auto (no prompts, still sandboxed) vs skip (the
  // all-bypass switch). Same two-state segmented control as Claude.
  renderSegmentedControl(
    els.codexPermission,
    c.permission_modes_available,
    c.permission_mode,
    function (p) { return p === 'skip' ? 'Skip permissions' : 'Auto mode'; },
    function (p) { patchConfig({ codex_permission_mode: p }); }
  );
  els.codexFlagsPreview.textContent = 'codex ' + (c.computed_flags || '');
}

function renderAntigravitySubsection() {
  const a = state.config && state.config.antigravity;
  if (!a) return;
  setSwitch(els.antigravitySkipPerms, !!a.skip_permissions);
  setSwitch(els.antigravitySandbox, !!a.sandbox);
  // The Antigravity CLI has no model/effort flags — the preview is just
  // the bare command plus whichever of the two toggles are on.
  els.antigravityFlagsPreview.textContent =
    'agy' + (a.computed_flags ? ' ' + a.computed_flags : '');
}

function renderCopilotSubsection() {
  const c = state.config && state.config.copilot;
  if (!c) return;
  // Copilot offers a long catalog; the portaled shared menu stays viewport
  // constrained. Empty-value Default still omits --model at launch.
  if (copilotModelCombo) {
    copilotModelCombo.setOptions(
      [{ value: '', label: 'Default' }].concat(modelOptions(c.models_available))
    );
    copilotModelCombo.setValue(c.model || '');
  }
  setSwitch(els.copilotSkipPerms, !!c.skip_permissions);
  els.copilotFlagsPreview.textContent =
    'copilot' + (c.computed_flags ? ' ' + c.computed_flags : '');
}

function renderPiSubsection() {
  const p = state.config && state.config.pi;
  if (!p || !els.piModel) return;
  // `models_available` carries {value,label} so the shared picker reads
  // "Opus/Sonnet/GPT" rather than raw provider model ids.
  if (piModelCombo) {
    piModelCombo.setOptions(modelOptions(p.models_available));
    piModelCombo.setValue(p.model);
  }
  // Effort — segmented control mapped to `--thinking`, mirroring Claude.
  renderSegmentedControl(
    els.piEffort,
    p.efforts_available,
    p.effort,
    effortLabel,
    function (e) { patchConfig({ pi_effort: e }); }
  );
  // Project trust — `--approve` (Trust) vs `--no-approve` (Ask). NOT a
  // tool-permission gate (pi has no sandbox); it governs whether pi loads
  // project-local `.pi/` resources.
  renderSegmentedControl(
    els.piTrust,
    p.trust_modes_available,
    p.trust_mode,
    function (t) { return t === 'trust' ? 'Trust' : 'Ask'; },
    function (t) { patchConfig({ pi_trust_mode: t }); }
  );
  els.piFlagsPreview.textContent =
    'pi' + (p.computed_flags ? ' ' + p.computed_flags : '');
}

function renderGrokSubsection() {
  const g = state.config && state.config.grok;
  if (!g) return;
  // Reasoning tier — mirrors Codex's Effort control. Grok has one model
  // (`grok models` lists only grok-4.5), so this is the only quality knob
  // and there is deliberately no model picker to render.
  renderSegmentedControl(
    els.grokEffort,
    g.efforts_available,
    g.effort,
    function (e) { return e.charAt(0).toUpperCase() + e.slice(1); },
    function (e) { patchConfig({ grok_effort: e }); }
  );
  // Permission mode — auto (no prompts, guard rails intact) vs skip
  // (bypassPermissions). Same two-state shape as Claude and Codex, rather
  // than grok's own six-value flag space.
  renderSegmentedControl(
    els.grokPermission,
    g.permission_modes_available,
    g.permission_mode,
    function (p) { return p === 'skip' ? 'Skip permissions' : 'Auto mode'; },
    function (p) { patchConfig({ grok_permission_mode: p }); }
  );
  els.grokFlagsPreview.textContent = 'grok ' + (g.computed_flags || '');
}

async function postConfigPatch(patch) {
  await jsonApi('/api/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
}

async function saveCodingQuotaPatch(patch, sequence) {
  try {
    await postConfigPatch(patch);
    if (sequence === codingQuotaSelectionSequence) {
      await fetchConfig(function () {
        return sequence === codingQuotaSelectionSequence;
      });
    }
    return true;
  } catch (exc) {
    apiFailToast('Save failed', exc);
    return false;
  }
}

// Resolves true on a saved-and-refreshed patch, false on a failed one (the
// toast already fired) — callers that only self-correct on failure (e.g.
// apps-coding.js's agent-visibility switches, issue #732) branch on this.
export async function patchConfig(patch) {
  try {
    await postConfigPatch(patch);
    await fetchConfig();
    return true;
  } catch (exc) {
    apiFailToast('Save failed', exc);
    return false;
  }
}

// role="switch" buttons (issue #355): click reads the current aria-checked,
// flips it, applies it optimistically, then patchConfig() round-trips
// through GET /api/config, which re-renders from server truth anyway.
function wireBoolSwitch(el, patchKey) {
  el.addEventListener('click', function () {
    const next = el.getAttribute('aria-checked') !== 'true';
    setSwitch(el, next);
    patchConfig({ [patchKey]: next });
  });
}

export function wireClaudeOptions() {
  codingModelCombo = wireModelCombo(
    document.getElementById('codingModelCombo'),
    function (v) { selectCodingQuota({ coding_model_choice: v }, v); }
  );
  claudeModelCombo = wireModelCombo(els.claudeModel, function (model) {
    selectCodingQuota(
      { claude_model: model, coding_model_choice: 'claude:' + model },
      'claude:' + model
    );
  });
  codexModelCombo = wireModelCombo(els.codexModel, function (model) {
    const selection = 'codex:' + model;
    selectCodingQuota({
      codex_model: model, coding_model_choice: selection,
    }, selection);
  });
  copilotModelCombo = wireModelCombo(els.copilotModel, function (model) {
    patchConfig({ copilot_model: model });
  });
  piModelCombo = wireModelCombo(els.piModel, function (model) {
    patchConfig({ pi_model: model });
  });
  wireBoolSwitch(els.claudeVerbose, 'claude_verbose');
  wireBoolSwitch(els.claudeDebug, 'claude_debug');
  wireBoolSwitch(els.antigravitySkipPerms, 'antigravity_skip_permissions');
  wireBoolSwitch(els.antigravitySandbox, 'antigravity_sandbox');
  wireBoolSwitch(els.copilotSkipPerms, 'copilot_skip_permissions');
  // Pi's effort and trust segmented buttons wire their own click handlers in
  // renderPiSubsection(), so there are no static listeners for those controls.
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
