/* Launch defaults (Settings › Launch defaults): one sheet per agent — a Claude Code subsection
 * (model + effort + verbose/debug + flags preview),
 * an Antigravity subsection (skip-permissions + sandbox toggles), a
 * GitHub Copilot subsection (model picker + skip-permissions toggle), and a
 * Pi subsection (model picker + effort select + project-trust segmented control — Opus and
 * Sonnet run on the claude-agent-sdk subscription path, GPT on the openai-codex
 * ChatGPT-plan path, so the provider/model are always passed explicitly).
 *
 * `patchConfig` round-trips through GET /api/config so the SPA's view of
 * config stays a single source of truth — server-computed flags + the
 * `models_available` / `efforts_available` enums included.
 */

import { els, state } from './state.js';
import { apiFailToast, jsonApi } from './api.js';
import { wireModelCombo } from './dom-utils.js';
import { setBoardDispatchModelOptions } from './board-dispatch.js';
import { setLifeOsModelOptions } from './life-os.js';
import { setSwitch } from './_vendored/switch/switch.js';
import { setUsageShows } from './usage-meter.js';
import { renderSettingsValues } from './settings-sheets.js';

// Shared model-picker controllers, created once the DOM exists.
let codingModelCombo = null;
let claudeModelCombo = null;
let codexModelCombo = null;
let piModelCombo = null;
let codingModelSelectionSequence = 0;
let codingModelSaveQueue = Promise.resolve();

// Ordered persistence for the coding-model picker. Quota rows no
// longer follow this selection — both agents are always shown — so
// this is now purely about the selector settling on server truth.
async function selectCodingModel(patch) {
  const sequence = ++codingModelSelectionSequence;

  // Preserve click order at the server while letting the newest selection own
  // the UI immediately. An older completion must never repaint a newer click.
  const saved = await (
    codingModelSaveQueue = codingModelSaveQueue.then(function () {
      return saveCodingModelPatch(patch, sequence);
    })
  );
  if (sequence !== codingModelSelectionSequence) return saved;

  // A rejected save leaves the optimistic control ahead of server truth.
  // Read it back explicitly and settle the selector.
  if (!saved) {
    try {
      await fetchConfig(function () {
        return sequence === codingModelSelectionSequence;
      });
    } catch (_exc) {
      if (sequence === codingModelSelectionSequence) renderClaudeOptions();
    }
  }
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
  // Only the Board can dispatch Grok; the Coding tab and Life OS keep `options`.
  const extra = config.board_dispatch_models || {};
  setBoardDispatchModelOptions(options.concat(modelOptions(extra.grok, 'grok:', 'Grok · ')));
}

export async function fetchConfig(shouldApply) {
  const body = await jsonApi('/api/config');
  // A caller may own only one optimistic selection generation. Check after
  // the await, immediately before mutating shared state and repainting.
  if (shouldApply && !shouldApply()) return false;
  state.config = body;
  setFieldValue(els.projectsDir, body.projects_dir || '');
  setFieldValue(els.projectsIgnore, (body.projects_ignore || []).join('\n'));
  setFieldValue(els.appsScanRoot, body.apps_scan_root || '');
  setFieldValue(els.lifeOsDir, body.life_os_dir || '');
  setFieldValue(els.fleetConfigDir, body.fleet_config_dir || '');
  if (els.terminalHistoryLines) {
    if (body.terminal_history_lines_min != null) {
      els.terminalHistoryLines.min = body.terminal_history_lines_min;
    }
    if (body.terminal_history_lines_max != null) {
      els.terminalHistoryLines.max = body.terminal_history_lines_max;
    }
    setFieldValue(els.terminalHistoryLines, body.terminal_history_lines || '');
  }
  if (els.largeUploadMaxMb) {
    if (body.large_upload_max_mb_min != null) els.largeUploadMaxMb.min = body.large_upload_max_mb_min;
    if (body.large_upload_max_mb_max != null) els.largeUploadMaxMb.max = body.large_upload_max_mb_max;
    setFieldValue(els.largeUploadMaxMb, body.large_upload_max_mb || '');
  }
  if (els.bootAutostartToggle) {
    setSwitch(els.bootAutostartToggle, !!body.boot_autostart_enabled);
  }
  renderChiefAutoCompact(body);
  if (els.chiefDoorbellToggle) {
    setSwitch(els.chiefDoorbellToggle, body.chief_doorbell === true);
  }
  if (els.chiefJobFailuresToggle) {
    setSwitch(els.chiefJobFailuresToggle, body.chief_job_failures !== false);
  }
  if (els.grokAutoSwitchToggle) {
    setSwitch(els.grokAutoSwitchToggle, body.grok_auto_switch === true);
  }
  if (els.lifeOsRefreshSkillToggle) {
    setSwitch(els.lifeOsRefreshSkillToggle, body.life_os_refresh_skill_instructions !== false);
  }
  renderUsageShows(body);
  if (els.hideChannelSessionsToggle) {
    setSwitch(els.hideChannelSessionsToggle, body.hide_channel_sessions !== false);
  }
  if (els.autostartChannelSessionsToggle) {
    setSwitch(els.autostartChannelSessionsToggle, body.autostart_channel_sessions === true);
  }
  renderClaudeOptions();
  renderSettingsValues();
  return true;
}

// Settings fields save as they change, and every save re-reads the
// config. Leave the field being typed in alone: the user has moved on to it,
// and the stored value would overwrite what they typed so far.
function setFieldValue(el, value) {
  if (el && el !== document.activeElement) el.value = value;
}

// "Usage shows": which providers the usage meter draws, one switch each.
// A switch saves at once; the meter repaints from the server's value, so
// another device's change shows here too.
export const USAGE_PROVIDER_LABELS = { claude: 'Claude', codex: 'Codex', grok: 'Grok' };

function usageSwitches() {
  return els.usageShows ? Array.from(els.usageShows.querySelectorAll('[data-provider]')) : [];
}

function wireUsageShows() {
  if (!els.usageShows || els.usageShows.dataset.wired) return;
  els.usageShows.dataset.wired = 'true';
  usageSwitches().forEach(function (sw) {
    sw.addEventListener('click', async function () {
      const next = sw.getAttribute('aria-checked') !== 'true';
      setSwitch(sw, next);
      const providers = usageSwitches()
        .filter(function (s) { return s.getAttribute('aria-checked') === 'true'; })
        .map(function (s) { return s.dataset.provider; });
      if (!(await patchConfig({ usage_providers: providers }))) {
        setSwitch(sw, !next);   // patchConfig already fired the failure toast
      }
    });
  });
}

function renderUsageShows(body) {
  const current = Array.isArray(body.usage_providers) ? body.usage_providers : ['claude', 'codex'];
  wireUsageShows();
  usageSwitches().forEach(function (sw) {
    setSwitch(sw, current.indexOf(sw.dataset.provider) !== -1);
  });
  setUsageShows(current);
}

// Settings › Chief's auto-compact: 0 is off, shown as the switch off and the percent
// field disabled at the default, since 0 keeps no earlier percent.
const _DEFAULT_CHIEF_AUTO_COMPACT = 30;

function renderChiefAutoCompact(body) {
  if (!els.chiefAutoCompactToggle) return;
  const field = els.chiefAutoCompactThreshold;
  const value = Number(body.chief_auto_compact_threshold) || 0;
  if (body.chief_auto_compact_threshold_min != null) field.min = body.chief_auto_compact_threshold_min;
  if (body.chief_auto_compact_threshold_max != null) field.max = body.chief_auto_compact_threshold_max;
  setSwitch(els.chiefAutoCompactToggle, value > 0);
  setFieldValue(field, String(value > 0 ? value : _DEFAULT_CHIEF_AUTO_COMPACT));
  field.disabled = value === 0;
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
// renderer, and a select callback — every permission/trust choice below
// is this same shape, on the vendored segmented control.
// `valueFn` defaults to identity for plain-string items.
function renderSegmented(host, items, currentValue, labelFn, onSelect, valueFn) {
  host.innerHTML = '';
  (items || []).forEach(function (item) {
    const value = valueFn ? valueFn(item) : item;
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'segmented-item';
    b.textContent = labelFn(item);
    b.dataset.value = value;
    b.setAttribute('aria-pressed', value === currentValue ? 'true' : 'false');
    b.addEventListener('click', function () {
      onSelect(value);
    });
    host.appendChild(b);
  });
}

// Effort / reasoning is a select-native, not pills: the lists run
// three to seven options and "Extra high" won't fit a segment, past segmented's
// five-option, one-line contract. onchange is assigned, not added, because
// every config readback re-renders the options into the same element.
function renderEffortSelect(select, items, currentValue, labelFn, onSelect) {
  select.innerHTML = '';
  (items || []).forEach(function (item) {
    const opt = document.createElement('option');
    opt.value = item;
    opt.textContent = labelFn(item);
    select.appendChild(opt);
  });
  select.value = currentValue;
  select.onchange = function () { onSelect(select.value); };
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
  // The Effort row follows the Model combo above it: it shows and
  // edits the selected model's override. The empty first choice clears it, so
  // the model runs at its catalog default, named in the label. "Don't send"
  // (off) goes last: it omits --effort rather than naming a level.
  const claudeEfforts = (c.efforts_available || []).filter(function (e) { return e !== 'off'; });
  renderEffortSelect(
    els.claudeEffort,
    [''].concat(claudeEfforts, (c.efforts_available || []).includes('off') ? ['off'] : []),
    (c.model_efforts || {})[c.model] || '',
    function (e) {
      if (e === '') return 'Model default (' + effortLabel(c.default_effort || 'high') + ')';
      return e === 'off' ? 'Don’t send' : effortLabel(e);
    },
    function (e) { patchConfig({ claude_effort: e }); }
  );
  renderSegmented(
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
  renderEffortSelect(
    els.codexEffort,
    c.efforts_available,
    c.effort,
    effortLabel,
    function (e) { patchConfig({ codex_effort: e }); }
  );
  // Permission mode — auto (no prompts, still sandboxed) vs skip (the
  // all-bypass switch). Same two-state segmented control as Claude.
  renderSegmented(
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
  // No model picker: Copilot resolves --model against the
  // account at launch and silently falls back, so a launcher-side choice
  // could only ever assert a model the session might not be running.
  // `/model` in-session owns it, as it does for the Antigravity CLI.
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
  // Effort — mapped to `--thinking`. Like Claude's it follows the Model
  // combo above it and edits that model's override; the empty first
  // choice clears it, so the model runs at its catalog default, named in the
  // label. `off` is a real `--thinking` level here, so it keeps its place.
  renderEffortSelect(
    els.piEffort,
    [''].concat(p.efforts_available || []),
    (p.model_efforts || {})[p.model] || '',
    function (e) {
      return e === '' ? 'Model default (' + effortLabel(p.default_effort || 'high') + ')' : effortLabel(e);
    },
    function (e) { patchConfig({ pi_effort: e }); }
  );
  // Project trust — `--approve` (Trust) vs `--no-approve` (Ask). NOT a
  // tool-permission gate (pi has no sandbox); it governs whether pi loads
  // project-local `.pi/` resources.
  renderSegmented(
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
  renderEffortSelect(
    els.grokEffort,
    g.efforts_available,
    g.effort,
    function (e) { return e.charAt(0).toUpperCase() + e.slice(1); },
    function (e) { patchConfig({ grok_effort: e }); }
  );
  // Permission mode — auto (no prompts, guard rails intact) vs skip
  // (bypassPermissions). Same two-state shape as Claude and Codex, rather
  // than grok's own six-value flag space.
  renderSegmented(
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

async function saveCodingModelPatch(patch, sequence) {
  try {
    await postConfigPatch(patch);
    if (sequence === codingModelSelectionSequence) {
      await fetchConfig(function () {
        return sequence === codingModelSelectionSequence;
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
// apps-coding.js's agent-visibility switches) branch on this.
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

// role="switch" buttons: click reads the current aria-checked,
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
    function (v) { selectCodingModel({ coding_model_choice: v }); }
  );
  claudeModelCombo = wireModelCombo(els.claudeModel, function (model) {
    selectCodingModel({ claude_model: model, coding_model_choice: 'claude:' + model });
  });
  codexModelCombo = wireModelCombo(els.codexModel, function (model) {
    selectCodingModel({ codex_model: model, coding_model_choice: 'codex:' + model });
  });
  piModelCombo = wireModelCombo(els.piModel, function (model) {
    patchConfig({ pi_model: model });
  });
  wireBoolSwitch(els.claudeVerbose, 'claude_verbose');
  wireBoolSwitch(els.claudeDebug, 'claude_debug');
  wireBoolSwitch(els.antigravitySkipPerms, 'antigravity_skip_permissions');
  wireBoolSwitch(els.antigravitySandbox, 'antigravity_sandbox');
  wireBoolSwitch(els.copilotSkipPerms, 'copilot_skip_permissions');
  // Pi's effort select and trust control wire their own handlers in
  // renderPiSubsection(), so there are no static listeners for those controls.
  // The Detached and Resume switches wire themselves: they are part of the
  // launch toolbar component (launch-toolbar.js).
}
