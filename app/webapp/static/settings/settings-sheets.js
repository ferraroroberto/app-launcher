/* Settings as inset groups.
 *
 * The Settings pane is a stack of rows: a lead icon, a label, the current
 * value in muted text and a chevron. Each row opens a sheet, a <dialog> on
 * the vendored modal shell with a header back + ✕ and one full-width Done
 * Text size and Start at log on stay inline on their rows.
 *
 * This module owns three things:
 * - the sheets' navigation: Launch defaults opens one agent's sheet on top
 *   of itself, so the open sheets are a stack. Back pops one level, ✕ and
 *   Done close them all, Escape goes back. Only the top sheet is shown;
 *   its parent reopens on Back.
 * - the rows' values, recomputed from state whenever the data behind them
 *   lands (config, passkeys, Telegram, tokens, screen time) and whenever a
 *   sheet closes.
 * - the fields that used to wait for a Save button: every one saves as it
 *   changes (a text or number field on blur), with a toast, and an inline
 *   error under the field when it is out of range or the server refuses it.
 *
 * The sheets' other controls keep their own modules: claude-options.js and
 * apps-coding.js (Launch defaults), telegram-setup.js / life-os.js
 * (Telegram), context-filter.js, tokens.js and webauthn.js (Passkeys).
 */

import { els, state } from '../state.js';
import { apiFailToast, jsonApi, toast } from '../api.js';
import { revealInCard } from '../dom-utils.js';
import { wireModelCombo } from '../model-combo.js';
import { fetchConfig, patchConfig, USAGE_PROVIDER_LABELS, wireConfigSwitch } from './claude-options.js';
import { terminalJsonApi } from '../webauthn.js';
import { fetchApps } from '../apps.js';
import { fetchSkills } from '../life-os.js';
import { profileNeedsSetup } from './telegram-setup.js';
import { formatMinutes } from '../screen/screen-time.js';

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
export function showPath(ids) {
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

// Land on one Settings field or sheet (via tabs.js's
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

// `tone` (optional) colours an exception, e.g. 'attention'; a plain value
// clears it.
function setValue(key, text, tone) {
  document.querySelectorAll('[data-settings-value="' + key + '"]').forEach(function (el) {
    el.textContent = text;
    if (tone) el.dataset.tone = tone;
    else delete el.dataset.tone;
  });
}

function launchValue(cfg) {
  const fav = String(cfg.coding_favorite_agent || 'claude');
  const label = document.querySelector('[data-agent-label="' + fav + '"]');
  const model = AGENT_MODEL[fav] && cfg[fav] ? AGENT_MODEL[fav](cfg[fav]) : '';
  return joined([label ? label.textContent : fav, model]);
}

// "3 profiles", or "1 needs setup" in attention while one does: a
// healthy state is a plain count, never a "ready" word.
function renderTelegramValue() {
  const ch = state.lifeOsChannels;
  if (!ch || (!ch.setup && !(ch.profiles || []).length)) {
    setValue('telegram', '');
    return;
  }
  const profiles = ch.profiles || [];
  if (!profiles.length) {
    setValue('telegram', 'Not set up');
    return;
  }
  const needs = profiles.filter(profileNeedsSetup).length;
  if (needs) setValue('telegram', needs + ' needs setup', 'attention');
  else setValue('telegram', plural(profiles.length, 'profile', 'profiles'));
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
  const active = document.querySelector('#contextFilterMode .segmented-item[aria-pressed="true"]');
  return active ? active.textContent : '';
}

export function renderSettingsValues() {
  const cfg = state.config;
  setValue('screen-time', state.screenTimeToday == null ? '' : formatMinutes(state.screenTimeToday));
  renderTelegramValue();
  setValue('context-filter', contextFilterValue());
  setValue('tokens', tokensValue());
  setValue('passkeys', passkeysValue());
  if (!cfg) return;
  const providers = Array.isArray(cfg.usage_providers) ? cfg.usage_providers : [];
  setValue('usage', providers.length
    ? joined(providers.map(function (p) { return USAGE_PROVIDER_LABELS[p] || p; }))
    : 'None');
  setValue('launch', launchValue(cfg));
  Object.keys(AGENT_SUMMARY).forEach(function (id) {
    setValue('agent-' + id, cfg[id] ? AGENT_SUMMARY[id](cfg[id]) : '');
  });
  const cap = Number(cfg.chief_worker_cap) || 0;
  setValue('chief', joined([capitalise(cfg.chief_model), cap ? plural(cap, 'worker', 'workers') : '']));
  const folders = [cfg.projects_dir, cfg.apps_scan_root, cfg.life_os_dir, cfg.fleet_config_dir]
    .filter(function (v) { return v && String(v).trim(); }).length;
  setValue('folders', folders + ' set');
  renderOutboxRoots(cfg.outbox_scratch_roots || []);
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

// ---------------------------------------------------- chat file folders
// The folders an agent may send Chat files from, one row each. Every
// add or remove posts the whole new list and the rows are redrawn from the
// server's answer (fetchConfig), so a refused entry never lingers on screen.
// The last row cannot be removed: an empty list would read as unset on the
// server and bring the default back, a state this editor must not offer.
function renderOutboxRoots(roots) {
  const list = els.outboxRootsList;
  if (!list) return;
  list.textContent = '';
  roots.forEach(function (root) {
    const li = document.createElement('li');
    const path = document.createElement('span');
    path.className = 'outbox-root-path';
    path.textContent = root;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'icon-button';
    remove.setAttribute('aria-label', 'Remove ' + root);
    remove.title = roots.length > 1 ? 'Remove' : 'Keep at least one folder';
    remove.disabled = roots.length < 2;
    remove.innerHTML = '<svg class="icon" aria-hidden="true" focusable="false"><use href="#i-trash-2"></use></svg>';
    remove.addEventListener('click', function () {
      saveOutboxRoots(roots.filter(function (r) { return r !== root; }), 'Chat file folder removed.');
    });
    li.append(path, remove);
    list.appendChild(li);
  });
}

async function saveOutboxRoots(roots, message) {
  const input = els.outboxRootInput;
  setFieldError(input, '');
  try {
    await jsonApi('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ outbox_scratch_roots: roots }),
    });
  } catch (exc) {
    // The server words each refusal (network path, drive root, missing,
    // duplicate...): show it under the field as well as in the toast.
    setFieldError(input, (exc && exc.message) || 'Not saved. Check the folder and try again.');
    apiFailToast('Folder not saved', exc);
    return;
  }
  input.value = '';
  await fetchConfig().catch(function () {});
  toast(message, 'good');
}

function wireOutboxRoots() {
  const input = els.outboxRootInput;
  const add = els.outboxRootAdd;
  if (!input || !add) return;
  function submit() {
    const value = input.value.trim();
    if (!value) {
      setFieldError(input, 'Enter a folder path.');
      return;
    }
    const current = (state.config && state.config.outbox_scratch_roots) || [];
    saveOutboxRoots(current.concat(value), 'Chat file folder added.');
  }
  add.addEventListener('click', submit);
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); submit(); }
  });
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
  wireOutboxRoots();
}

// ------------------------------------------------------------------ chief
// Model and worker cap are the chief's own settings (PUT
// /api/board/chief/settings, passkey-gated: they steer it); auto-compact is
// a config field the chief reads. GET /api/config carries all four,
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
  wireChiefDoorbell();
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

// The doorbell is read by its 3 s tick, the job-failure event by the next
// failed run, the Life OS skill refresh by the next resume.
function wireChiefDoorbell() {
  wireConfigSwitch(els.chiefDoorbellToggle, 'chief_doorbell', { label: 'Chief doorbell' });
  wireConfigSwitch(els.chiefJobFailuresToggle, 'chief_job_failures', { label: 'Chief job failures' });
  wireConfigSwitch(els.grokAutoSwitchToggle, 'grok_auto_switch', { label: 'Grok auto-switch' });
}

// ------------------------------------------------------------------- wire
export function wireSettingsSheets() {
  wireConfigSwitch(els.lifeOsRefreshSkillToggle, 'life_os_refresh_skill_instructions',
    { label: 'Skill refresh on resume' });
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
    // and its top is another sheet, or nothing. It can also be stale for the
    // top sheet itself: Back reopens the parent, and the parent's earlier
    // close event may land after that. An open dialog was not closed,
    // or the path would be wiped under a sheet that is still showing.
    d.addEventListener('close', function () {
      if (d.open || stack[stack.length - 1] !== d.id) return;
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
