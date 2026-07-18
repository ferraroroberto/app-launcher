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

// The Coding tab surfaces the Claude launch model twice — the compact
// board-style dropdown in the Projects summary (#codingModelCombo) and the
// segmented control in the options card (#claudeModel) — and keeps them in
// sync. Both offer exactly these three (Board-combo parity, #540): Haiku stays
// a valid `claude_model` config value server-side but is not surfaced here.
// Order matches the dropdown's option order.
const CODING_MODELS = ['sonnet', 'opus', 'fable'];

// The Projects-summary model dropdown controller ({setValue, getValue}),
// created in wireClaudeOptions once the DOM exists.
let codingModelCombo = null;

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
    setSwitch(els.bootAutostartToggle, !!body.boot_autostart_enabled);
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
  // same CODING_MODELS subset so the two stay a true mirror of each other
  // (#540) — filter server truth to those actually offered, in combo order.
  const models = CODING_MODELS.filter(function (m) {
    return (c.models_available || []).includes(m);
  });
  renderSegmentedControl(
    els.claudeModel,
    models,
    c.model,
    function (m) { return m.charAt(0).toUpperCase() + m.slice(1); },
    function (m) { patchConfig({ claude_model: m }); }
  );
  // Keep the compact dropdown in lockstep. patchConfig() round-trips through
  // GET /api/config and re-renders this whole subsection, so a change from
  // either control lands here and updates both — no explicit cross-wiring.
  // setValue never fires onChange, so this can't loop.
  if (codingModelCombo && CODING_MODELS.includes(c.model)) {
    codingModelCombo.setValue(c.model);
  }
  renderSegmentedControl(
    els.claudeEffort,
    c.efforts_available,
    c.effort,
    function (e) { return e === 'off' ? 'Off' : e.charAt(0).toUpperCase() + e.slice(1); },
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
  // Reasoning tier — a segmented control mirroring Claude's Effort.
  // Codex has no model tiers, so this is the quality knob (mapped to
  // `model_reasoning_effort` server-side).
  renderSegmentedControl(
    els.codexEffort,
    c.efforts_available,
    c.effort,
    function (e) { return e.charAt(0).toUpperCase() + e.slice(1); },
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
  renderSegmentedControl(
    els.piModel,
    p.models_available,
    p.model,
    function (m) { return m.label; },
    function (v) { patchConfig({ pi_model: v }); },
    function (m) { return m.value; }
  );
  // Effort — segmented control mapped to `--thinking`, mirroring Claude.
  renderSegmentedControl(
    els.piEffort,
    p.efforts_available,
    p.effort,
    function (e) { return e.charAt(0).toUpperCase() + e.slice(1); },
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
    function (v) { patchConfig({ claude_model: v }); }
  );
}
