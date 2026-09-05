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
import { toggleAriaChecked, wireModelCombo } from './dom-utils.js';
import { setSwitch } from './_vendored/switch/switch.js';

// The Projects-summary model dropdown controller ({setValue, getValue}),
// created in wireClaudeOptions once the DOM exists.
let codingModelCombo = null;
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

function fillSelect(select, models, current, includeDefault) {
  select.innerHTML = '';
  if (includeDefault) {
    const fallback = document.createElement('option');
    fallback.value = '';
    fallback.textContent = 'Default';
    select.appendChild(fallback);
  }
  (models || []).forEach(function (model) {
    const opt = document.createElement('option');
    opt.value = model.value || model;
    opt.textContent = model.label || model;
    opt.disabled = model.available === false;
    if (opt.disabled) {
      opt.textContent += ' — unavailable';
      opt.title = model.unavailable_reason || 'Unavailable';
    }
    select.appendChild(opt);
  });
  select.value = current || '';
}

function fillCombo(root, models, valuePrefix, append) {
  if (!root) return;
  const menu = root.querySelector('.model-combo-menu');
  if (!menu) return;
  if (!append) menu.innerHTML = '';
  (models || []).forEach(function (model) {
    const option = document.createElement('button');
    option.type = 'button';
    option.setAttribute('role', 'option');
    option.setAttribute('aria-selected', 'false');
    option.dataset.value = (valuePrefix || '') + model.value;
    const providerLabel = valuePrefix
      ? valuePrefix.replace(':', '').replace(/^./, function (c) { return c.toUpperCase(); }) + ' · '
      : '';
    option.textContent = providerLabel + model.label;
    option.disabled = model.available === false;
    if (option.disabled) option.title = model.unavailable_reason || 'Unavailable';
    menu.appendChild(option);
  });
  const selected = menu.querySelector('[data-value="' + root.dataset.value + '"]');
  if (selected) {
    selected.setAttribute('aria-selected', 'true');
    const trigger = root.querySelector('.model-combo-trigger');
    if (trigger) trigger.textContent = selected.textContent;
  }
}

function renderSharedModelSelectors() {
  const config = state.config || {};
  const catalog = config.model_catalog || {};
  const coding = document.getElementById('codingModelCombo');
  fillCombo(coding, catalog.claude, 'claude:');
  fillCombo(coding, catalog.codex, 'codex:', true);
  if (codingModelCombo) codingModelCombo.setValue(config.coding_model_choice);

  const lifeOs = document.getElementById('lifeOsModelCombo');
  fillCombo(lifeOs, catalog.claude, 'claude:');
  fillCombo(lifeOs, catalog.codex, 'codex:', true);

  // Stored Life OS conversation ids belong to Claude, so targeted resume
  // remains provider-safe even though fresh skill launches now offer Codex.
  fillCombo(
    document.getElementById('lifeOsConvosModelCombo'),
    catalog.claude,
    'claude:'
  );

  const board = document.getElementById('boardDispatchModel');
  if (board) {
    const previous = board.value || 'claude:sonnet';
    fillSelect(
      board,
      (catalog.claude || []).map(function (m) { return { ...m, value: 'claude:' + m.value, label: 'Claude · ' + m.label }; })
        .concat((catalog.codex || []).map(function (m) { return { ...m, value: 'codex:' + m.value, label: 'Codex · ' + m.label }; })),
      previous,
      false
    );
  }
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
// segmented control below (Claude, Codex, Pi) is this same shape (issue
// #520). `valueFn` defaults to identity (plain string items); Pi's model
// row is the one case with {value,label} objects, so it passes a `valueFn`
// to pull `value` out for the dataset/click-handler/active-comparison while
// `labelFn` still renders `label`.
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
  // Both the segmented control and the Projects-summary combo render the
  // Filter the legacy-compatible Claude enum to the curated phone catalog.
  const surfaced = ((state.config.model_catalog || {}).claude || []).map(function (m) { return m.value; });
  const models = (c.models_available || []).filter(function (model) {
    return surfaced.includes(model);
  });
  renderSegmentedControl(
    els.claudeModel,
    models,
    c.model,
    function (m) { return m.charAt(0).toUpperCase() + m.slice(1); },
    function (m) {
      selectCodingQuota(
        { claude_model: m, coding_model_choice: 'claude:' + m },
        'claude:' + m
      );
    }
  );
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
  fillSelect(els.codexModel, c.models_available, c.model, false);
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
  setSwitch(els.copilotSkipPerms, !!c.skip_permissions);
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
  fillSelect(els.piModel, p.models_available, p.model, false);
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
  wireBoolSwitch(els.claudeVerbose, 'claude_verbose');
  wireBoolSwitch(els.claudeDebug, 'claude_debug');
  wireBoolSwitch(els.antigravitySkipPerms, 'antigravity_skip_permissions');
  wireBoolSwitch(els.antigravitySandbox, 'antigravity_sandbox');
  wireBoolSwitch(els.copilotSkipPerms, 'copilot_skip_permissions');
  els.copilotModel.addEventListener('change', function () {
    patchConfig({ copilot_model: els.copilotModel.value });
  });
  els.codexModel.addEventListener('change', function () {
    const selection = 'codex:' + els.codexModel.value;
    selectCodingQuota({
      codex_model: els.codexModel.value, coding_model_choice: selection,
    }, selection);
  });
  els.piModel.addEventListener('change', function () {
    patchConfig({ pi_model: els.piModel.value });
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
  // The launch-model dropdown (#540) lives in the Projects <summary>. A user
  // pick persists claude_model; the config round-trip re-renders both this
  // dropdown and the options-card segmented control, keeping them in sync
  // (#claudeModel follows too). wireModelCombo handles the summary-tap guard.
  codingModelCombo = wireModelCombo(
    document.getElementById('codingModelCombo'),
    function (v) { selectCodingQuota({ coding_model_choice: v }, v); }
  );
}
