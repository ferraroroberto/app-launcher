/* Settings as inset groups (#1435, step 3 of #1432).
 *
 * The Settings pane is a stack of rows: a lead icon, a label, the current
 * value in muted text and a chevron. Each row opens a sheet, a <dialog> on
 * the vendored modal shell with a header back + ✕ and one full-width Done
 * (#545). Text size and Start at log on stay inline on their rows.
 *
 * This module owns three things:
 * - the sheets' navigation: Launch defaults opens one agent's sheet on top
 *   of itself, so the open sheets are a stack. Back pops one level, ✕ and
 *   Done close them all, Escape goes back. Only the top sheet is shown;
 *   its parent reopens on Back.
 * - the rows' values, recomputed from state whenever the data behind them
 *   lands (config, passkeys, Telegram, tokens) and whenever a sheet closes.
 * - the fields that used to wait for a Save button: every one saves as it
 *   changes (a text or number field on blur), with a toast, and an inline
 *   error under the field when it is out of range or the server refuses it.
 *
 * The sheets' other controls keep their own modules: claude-options.js and
 * apps-coding.js (Launch defaults), telegram-setup.js / life-os.js
 * (Telegram), context-filter.js, tokens.js and webauthn.js (Passkeys).
 */

import { els, state } from './state.js';
import { apiFailToast, toast } from './api.js';
import { revealInCard, wireModelCombo } from './dom-utils.js';
import { fetchConfig, patchConfig, USAGE_SHOWS_LABELS } from './claude-options.js';
import { terminalJsonApi } from './webauthn.js';
import { fetchApps } from './apps.js';
import { fetchSkills } from './life-os.js';

// ------------------------------------------------------------ navigation
// The open sheets, the shown one last.
const stack = [];

function sheet(id) {
  return document.getElementById(id);
}

function closeTop() {
  const top = sheet(stack[stack.length - 1]);
  if (top && top.open) top.close();
}

// Show the sheet path `ids` (a top-level sheet, or Launch defaults then the
// agent sheet): the current top closes and the path's last sheet opens.
function showPath(ids) {
  const target = ids[ids.length - 1];
  if (stack[stack.length - 1] !== target) closeTop();
  stack.splice(0, stack.length, ...ids);
  const d = sheet(target);
  if (d && !d.open) d.showModal();
}

function back() {
  closeTop();
  stack.pop();
  const parent = sheet(stack[stack.length - 1]);
  if (parent) {
    parent.showModal();
  } else {
    renderSettingsValues();
  }
}

function closeSheets() {
  closeTop();
  stack.length = 0;
  renderSettingsValues();
}

// Launch defaults' per-agent rows share one sheet: only the chosen agent's
// group shows, under that agent's name.
function selectAgent(agentId) {
  document.querySelectorAll('#agentSheet [data-agent-group]').forEach(function (group) {
    group.hidden = group.dataset.agentGroup !== agentId;
  });
  const label = document.querySelector('[data-agent-label="' + agentId + '"]');
  const title = document.getElementById('agentSheetTitle');
  if (title) title.textContent = label ? label.textContent : agentId;
}

function openAgentSheet(agentId) {
  selectAgent(agentId);
  showPath(['launchDefaultsSheet', 'agentSheet']);
}

// Land on one Settings field or sheet (#1238 J-09, via tabs.js's
// openSettingsAt): open the sheet that holds it, then scroll to and focus
// the field. A sheet id opens just that sheet.
export function revealSettingsField(el) {
  if (!el) return;
  const host = el.matches('dialog.settings-sheet') ? el : el.closest('dialog.settings-sheet');
  if (!host) {
    revealInCard(el);
    return;
  }
  const group = el.closest('[data-agent-group]');
  if (host.id === 'agentSheet' && group) {
    openAgentSheet(group.dataset.agentGroup);
  } else {
    showPath([host.id]);
  }
  if (el !== host) revealInCard(el);
}

// ----------------------------------------------------------------- values
function capitalise(text) {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : '';
}

function effortWord(effort) {
  if (!effort || effort === 'off') return '';
  return effort === 'xhigh' ? 'extra high' : effort;
}

function permissionWord(mode) {
  return mode === 'skip' ? 'skip' : 'auto';
}

// A catalog entry is a bare id or {value, label}; show its label.
function modelLabel(models, value) {
  const hit = (models || []).find(function (m) {
    return (typeof m === 'string' ? m : m.value) === value;
  });
  if (hit && typeof hit === 'object') return hit.label || hit.value;
  return capitalise(value || '');
}

function joined(parts) {
  return parts.filter(Boolean).join(' · ');
}

function plural(n, one, many) {
  return n + ' ' + (n === 1 ? one : many);
}

// One line per agent row ("Sonnet · high · auto"), the model first so
// Launch defaults' own value can reuse it.
const AGENT_MODEL = {
  claude: function (c) { return capitalise(c.model); },
  codex: function (c) { return modelLabel(c.models_available, c.model); },
  pi: function (c) { return modelLabel(c.models_available, c.model); },
};
const AGENT_SUMMARY = {
  claude: function (c) {
    return joined([AGENT_MODEL.claude(c), effortWord(c.effort), permissionWord(c.permission_mode)]);
  },
  codex: function (c) {
    return joined([AGENT_MODEL.codex(c), effortWord(c.effort), permissionWord(c.permission_mode)]);
  },
  antigravity: function (c) {
    return joined([c.skip_permissions ? 'skip prompts' : 'asks', c.sandbox ? 'sandbox' : '']);
  },
  copilot: function (c) { return c.skip_permissions ? 'skip prompts' : 'asks'; },
  pi: function (c) {
    return joined([AGENT_MODEL.pi(c), effortWord(c.effort), c.trust_mode === 'trust' ? 'trust' : 'ask']);
  },
  grok: function (c) {
    return joined([effortWord(c.effort), permissionWord(c.permission_mode)]);
  },
};

function setValue(key, text) {
  document.querySelectorAll('[data-settings-value="' + key + '"]').forEach(function (el) {
    el.textContent = text;
  });
}

function launchValue(cfg) {
  const fav = String(cfg.coding_favorite_agent || 'claude');
  const label = document.querySelector('[data-agent-label="' + fav + '"]');
  const model = AGENT_MODEL[fav] && cfg[fav] ? AGENT_MODEL[fav](cfg[fav]) : '';
  return joined([label ? label.textContent : fav, model]);
}

function telegramValue() {
  const ch = state.lifeOsChannels;
  if (!ch || (!ch.setup && !(ch.profiles || []).length)) return '';
  const profiles = ch.profiles || [];
  if (!profiles.length) return 'Not set up';
  const ready = profiles.filter(function (p) { return p.skill_found && p.env_present; }).length;
  return ready + ' ready';
}

function passkeysValue() {
  const w = state.webauthn || {};
  if (!w.configured) return 'Off';
  const n = (w.devices || []).length;
  return n ? plural(n, 'device', 'devices') : 'None';
}

function tokensValue() {
  const n = state.apiTokenCount;
  if (n == null) return '';
  return n ? plural(n, 'token', 'tokens') : 'None';
}

// The context filter's sheet is context-filter.js's: read the mode it shows.
function contextFilterValue() {
  const active = document.querySelector('#contextFilterMode .range-tab.active');
  return active ? active.textContent : '';
}

export function renderSettingsValues() {
  const cfg = state.config;
  setValue('telegram', telegramValue());
  setValue('context-filter', contextFilterValue());
  setValue('tokens', tokensValue());
  setValue('passkeys', passkeysValue());
  if (!cfg) return;
  setValue('usage', USAGE_SHOWS_LABELS[cfg.usage_shows || 'both'] || '');
  setValue('launch', launchValue(cfg));
  Object.keys(AGENT_SUMMARY).forEach(function (id) {
    setValue('agent-' + id, cfg[id] ? AGENT_SUMMARY[id](cfg[id]) : '');
  });
  const cap = Number(cfg.chief_worker_cap) || 0;
  setValue('chief', joined([capitalise(cfg.chief_model), cap ? plural(cap, 'worker', 'workers') : '']));
  const folders = [cfg.projects_dir, cfg.apps_scan_root, cfg.life_os_dir, cfg.fleet_config_dir]
    .filter(function (v) { return v && String(v).trim(); }).length;
  setValue('folders', folders + ' set');
  setValue('terminal', joined([
    cfg.terminal_history_lines ? cfg.terminal_history_lines + ' lines' : '',
    cfg.large_upload_max_mb ? cfg.large_upload_max_mb + ' MB' : '',
  ]));
  renderChiefFields(cfg);
}

// --------------------------------------------------------- saving fields
// An inline error line under a field: created on first use, after the
// field's help line, and named by the field's aria-describedby.
function setFieldError(input, message) {
  const id = input.id + 'Error';
  let err = document.getElementById(id);
  if (!err && message) {
    err = document.createElement('p');
    err.id = id;
    err.className = 'field-error';
    err.setAttribute('role', 'alert');
    const helpId = (input.getAttribute('aria-describedby') || '').split(' ')[0];
    const anchor = document.getElementById(helpId) || input.closest('label') || input;
    anchor.after(err);
    input.setAttribute('aria-describedby',
      ((input.getAttribute('aria-describedby') || '') + ' ' + id).trim());
  }
  if (err) {
    err.textContent = message || '';
    err.hidden = !message;
  }
  if (message) {
    input.setAttribute('aria-invalid', 'true');
  } else {
    input.removeAttribute('aria-invalid');
  }
}

// A whole number inside the field's own min/max (the server enforces the
// same range); null after saying what is wrong.
function wholeNumberIn(input, name) {
  const min = Number(input.min);
  const max = Number(input.max);
  const value = Number(input.value);
  if (input.value.trim() === '' || !Number.isInteger(value) || value < min || value > max) {
    const message = name + ' must be a whole number between ' + min + ' and ' + max + '.';
    setFieldError(input, message);
    toast(message, 'error');
    return null;
  }
  return value;
}

async function saveConfigField(input, patch, message, after) {
  setFieldError(input, '');
  if (!(await patchConfig(patch))) {
    // patchConfig already toasted the server's reason.
    setFieldError(input, 'Not saved. Check the value and try again.');
    return;
  }
  toast(message, 'good');
  if (after) await after().catch(function () {});
}

// [field id, config key, name for the toast, refresh after a save]
const TEXT_FIELDS = [
  ['projectsDir', 'projects_dir', 'Projects folder', function () { return fetchApps(); }],
  ['appsScanRoot', 'apps_scan_root', 'Apps folder', null],
  ['lifeOsDir', 'life_os_dir', 'Life OS folder', function () { return fetchSkills(); }],
  ['fleetConfigDir', 'fleet_config_dir', 'fleet-config folder', null],
];
const NUMBER_FIELDS = [
  ['terminalHistoryLines', 'terminal_history_lines', 'Terminal history'],
  ['largeUploadMaxMb', 'large_upload_max_mb', 'Large file limit'],
];

// `change` fires once an edited field loses focus (or on Enter), never per
// keystroke, so a half-typed path is never saved.
function wireFields() {
  TEXT_FIELDS.forEach(function (f) {
    const input = document.getElementById(f[0]);
    if (!input) return;
    input.addEventListener('change', function () {
      saveConfigField(input, { [f[1]]: input.value.trim() }, f[2] + ' saved.', f[3]);
    });
  });
  if (els.projectsIgnore) {
    els.projectsIgnore.addEventListener('change', function () {
      const ignore = els.projectsIgnore.value
        .split('\n')
        .map(function (s) { return s.trim(); })
        .filter(Boolean);
      saveConfigField(els.projectsIgnore, { projects_ignore: ignore }, 'Hidden projects saved.',
        function () { return fetchApps(); });
    });
  }
  NUMBER_FIELDS.forEach(function (f) {
    const input = document.getElementById(f[0]);
    if (!input) return;
    input.addEventListener('change', function () {
      const value = wholeNumberIn(input, f[2]);
      if (value === null) return;
      saveConfigField(input, { [f[1]]: value }, f[2] + ' saved.');
    });
  });
}

// ------------------------------------------------------------------ chief
// Model and worker cap are the chief's own settings (PUT
// /api/board/chief/settings, passkey-gated: they steer it); auto-compact is
// a config field (#1298) the chief reads. GET /api/config carries all four,
// so the row's value needs no passkey.
let chiefModelCombo = null;
const CHIEF_MODELS = [
  { value: 'sonnet', label: 'Sonnet' },
  { value: 'opus', label: 'Opus' },
  { value: 'fable', label: 'Fable' },
];

function renderChiefFields(cfg) {
  if (chiefModelCombo && cfg.chief_model) chiefModelCombo.setValue(cfg.chief_model);
  const cap = els.chiefWorkerCap;
  if (!cap) return;
  if (cfg.chief_worker_cap_min != null) cap.min = cfg.chief_worker_cap_min;
  if (cfg.chief_worker_cap_max != null) cap.max = cfg.chief_worker_cap_max;
  if (cap !== document.activeElement && cfg.chief_worker_cap != null) {
    cap.value = String(cfg.chief_worker_cap);
  }
}

async function putChiefSettings(patch, message, input) {
  try {
    await terminalJsonApi('/api/board/chief/settings', { method: 'PUT', body: patch });
  } catch (exc) {
    if (input) setFieldError(input, 'Not saved. Check the value and try again.');
    apiFailToast('Chief settings save failed', exc);
    // Put the controls back on what is stored.
    await fetchConfig().catch(function () {});
    return;
  }
  if (input) setFieldError(input, '');
  await fetchConfig().catch(function () {});
  toast(message, 'good');
}

function wireChief() {
  chiefModelCombo = wireModelCombo(els.chiefModelSelect, function (model) {
    putChiefSettings({ model: model }, 'Chief model: ' + capitalise(model) + '.');
  });
  if (chiefModelCombo) {
    chiefModelCombo.setOptions(CHIEF_MODELS);
    chiefModelCombo.setValue('fable');
  }
  if (els.chiefWorkerCap) {
    els.chiefWorkerCap.addEventListener('change', function () {
      const value = wholeNumberIn(els.chiefWorkerCap, 'Worker cap');
      if (value === null) return;
      putChiefSettings({ worker_cap: value }, 'Worker cap saved.', els.chiefWorkerCap);
    });
  }
  const toggle = els.chiefAutoCompactToggle;
  const field = els.chiefAutoCompactThreshold;
  if (!toggle || !field) return;
  // Off stores 0; on stores the percent in the field.
  toggle.addEventListener('click', async function () {
    const on = toggle.getAttribute('aria-checked') !== 'true';
    const value = on ? wholeNumberIn(field, 'Compact at context use') : 0;
    if (value === null) return;
    setFieldError(field, '');
    if (await patchConfig({ chief_auto_compact_threshold: value })) {
      toast(on ? 'Chief auto-compact on at ' + value + '%.' : 'Chief auto-compact off.', 'good');
    }
  });
  field.addEventListener('change', function () {
    if (toggle.getAttribute('aria-checked') !== 'true') return;
    const value = wholeNumberIn(field, 'Compact at context use');
    if (value === null) return;
    saveConfigField(field, { chief_auto_compact_threshold: value },
      'Chief auto-compact at ' + value + '%.');
  });
}

// ------------------------------------------------------------------- wire
export function wireSettingsSheets() {
  document.querySelectorAll('#paneSettings [data-sheet]').forEach(function (btn) {
    btn.addEventListener('click', function () { showPath([btn.dataset.sheet]); });
  });
  document.querySelectorAll('[data-agent-sheet]').forEach(function (btn) {
    btn.addEventListener('click', function () { openAgentSheet(btn.dataset.agentSheet); });
  });
  document.querySelectorAll('dialog.settings-sheet').forEach(function (d) {
    d.querySelectorAll('[data-sheet-back]').forEach(function (b) {
      b.addEventListener('click', back);
    });
    d.querySelectorAll('[data-sheet-close]').forEach(function (b) {
      b.addEventListener('click', closeSheets);
    });
    // Escape steps back one level, like the header's back.
    d.addEventListener('cancel', function (e) {
      e.preventDefault();
      back();
    });
    // Closed some other way (a browser forcing a repeated Escape through):
    // nothing is left open, so forget the path. The event is queued, so by
    // the time it runs after this module's own close the path has moved on
    // and its top is another sheet, or nothing.
    d.addEventListener('close', function () {
      if (stack[stack.length - 1] !== d.id) return;
      stack.length = 0;
      renderSettingsValues();
    });
  });
  wireFields();
  wireChief();
  document.addEventListener('launcher:tab', function (e) {
    if (e.detail && e.detail.tab === 'settings') renderSettingsValues();
  });
}
