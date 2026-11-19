/* Settings › Launch defaults › Launch presets.
 *
 * The editor for the named model + effort presets the Coding and Life OS
 * launch dropdowns offer (launch-presets.js). Each saved preset is a row: its
 * name (edit it in place, saved when the field loses focus), the model, effort
 * and context tier it launches with, and a kebab holding Remove. Under the
 * list, a name, a model, an effort and (for a model that has tiers) a context
 * add a new one.
 *
 * Every change posts the whole list to /api/config and the rows are redrawn
 * from the server's answer, as the chat-folder list does, so a refused entry
 * never lingers. Models, their efforts and their context tiers come from the
 * config's catalog.
 */

import { el } from '../dom-utils.js';
import { state } from '../state.js';
import { modelChoices } from '../shared/model-choices.js';
import { toast } from '../api.js';
import { icon } from '../_vendored/icons/icons.js';
import { ACTION_ROW_MENU } from '../action-rows.js';
import { createRowMenu } from '../_vendored/row-menu/row-menu.js';

// Only these agents take a per-launch model, and so can carry a preset.
const PRESET_AGENTS = [
  { agent: 'claude', label: 'Claude' },
  { agent: 'codex', label: 'Codex' },
];

// The most presets the server keeps (src/launching/presets.py MAX_PRESETS).
const MAX_PRESETS = 12;
const presetMenu = createRowMenu({ className: 'project-menu', placement: ACTION_ROW_MENU });
let save = null;
let modelsSignature = '';

function config() {
  return state.config || {};
}

// [{ agent, model, value, label, efforts, defaultEffort, contexts }] for the model select.
function presetModels() {
  const cfg = config();
  const catalog = cfg.model_catalog || {};
  const rows = [];
  PRESET_AGENTS.forEach(function (group) {
    const entries = catalog[group.agent] || [];
    modelChoices(group.agent, cfg).forEach(function (choice) {
      const entry = entries.find(function (m) { return m.value === choice.id; });
      if (!entry || !(entry.efforts || []).length) return;
      rows.push({
        agent: group.agent, model: choice.id, value: choice.value,
        label: group.label + ' · ' + choice.label,
        efforts: entry.efforts, defaultEffort: entry.default_effort || entry.efforts[0],
        contexts: entry.contexts || [],
      });
    });
  });
  return rows;
}

function modelLabel(preset) {
  const row = presetModels().find(function (m) { return m.value === preset.agent + ':' + preset.model; });
  return row ? row.label : preset.agent + ' · ' + preset.model;
}

// The label of `preset`'s context tier, '' when it runs the model's own window.
function contextLabel(preset) {
  if (!preset.context) return '';
  const row = presetModels().find(function (m) { return m.value === preset.agent + ':' + preset.model; });
  const tier = row && row.contexts.find(function (c) { return c.value === preset.context; });
  return tier ? tier.label : preset.context;
}

function effortLabel(value) {
  return value === 'xhigh' ? 'Extra high' : value.charAt(0).toUpperCase() + value.slice(1);
}

function fillEffortSelect() {
  const modelSelect = document.getElementById('launchPresetModel');
  const effortSelect = document.getElementById('launchPresetEffort');
  if (!modelSelect || !effortSelect) return;
  const row = presetModels().find(function (m) { return m.value === modelSelect.value; });
  const keep = effortSelect.value;
  effortSelect.replaceChildren();
  ((row && row.efforts) || []).forEach(function (effort) {
    const option = el('option', null, effortLabel(effort));
    option.value = effort;
    effortSelect.appendChild(option);
  });
  if (row) effortSelect.value = row.efforts.includes(keep) ? keep : row.defaultEffort;
  fillContextSelect();
}

// The context row shows only for a model that has tiers; "Default" keeps the
// model's own window.
function fillContextSelect() {
  const modelSelect = document.getElementById('launchPresetModel');
  const contextSelect = document.getElementById('launchPresetContext');
  if (!modelSelect || !contextSelect) return;
  const row = presetModels().find(function (m) { return m.value === modelSelect.value; });
  const keep = contextSelect.value;
  const tiers = (row && row.contexts) || [];
  contextSelect.replaceChildren();
  const none = el('option', null, 'Default');
  none.value = '';
  contextSelect.appendChild(none);
  tiers.forEach(function (tier) {
    const option = el('option', null, tier.label);
    option.value = tier.value;
    contextSelect.appendChild(option);
  });
  contextSelect.value = tiers.some(function (t) { return t.value === keep; }) ? keep : '';
  const host = contextSelect.closest('.opt-row');
  if (host) host.hidden = !tiers.length;
}

// The model select is refilled only when the catalog behind it changes, so a
// config re-read while the owner is choosing never resets their pick.
function fillModelSelect() {
  const modelSelect = document.getElementById('launchPresetModel');
  if (!modelSelect) return;
  const models = presetModels();
  const signature = models.map(function (m) { return m.value; }).join('|');
  if (signature === modelsSignature) return;
  modelsSignature = signature;
  const keep = modelSelect.value;
  modelSelect.replaceChildren();
  models.forEach(function (m) {
    const option = el('option', null, m.label);
    option.value = m.value;
    modelSelect.appendChild(option);
  });
  if (models.some(function (m) { return m.value === keep; })) modelSelect.value = keep;
  fillEffortSelect();
}

function presetList() {
  return (config().launch_presets || []).map(function (p) { return Object.assign({}, p); });
}

function renderRow(preset, all) {
  const li = el('li', 'launch-preset-row');
  const text = el('div', 'launch-preset-text');
  const name = el('input', 'input-native launch-preset-name');
  name.type = 'text';
  name.value = preset.name;
  name.maxLength = 40;
  name.setAttribute('aria-label', 'Preset name');
  name.autocomplete = 'off';
  name.addEventListener('change', function () {
    const next = name.value.trim();
    if (!next || next === preset.name) {
      name.value = preset.name;
      return;
    }
    save(all.map(function (p) { return p.id === preset.id ? Object.assign({}, p, { name: next }) : p; }),
      'Preset renamed.');
  });
  const meta = [modelLabel(preset), effortLabel(preset.effort), contextLabel(preset)].filter(Boolean);
  text.append(name, el('span', 'launch-preset-meta', meta.join(' · ')));
  const kebab = el('button', 'icon-button action-row-kebab');
  kebab.dataset.track = 'launch-presets.menu';
  kebab.type = 'button';
  kebab.title = 'Preset actions';
  kebab.setAttribute('aria-label', 'Actions for ' + preset.name);
  kebab.innerHTML = icon('ellipsis-vertical');
  presetMenu.attach(preset.id, kebab, [{
    dataset: { item: 'launch-preset-remove-item' }, glyph: 'trash-2', danger: true,
    label: 'Remove', ariaLabel: 'Remove ' + preset.name,
    onTap: function () {
      save(all.filter(function (p) { return p.id !== preset.id; }), 'Preset removed.');
    },
  }]).dataset.surface = 'settings.preset-menu';
  li.append(text, kebab);
  return li;
}

// Redraw the list and the add form's selects from the loaded config.
export function renderLaunchPresetsEditor() {
  const list = document.getElementById('launchPresetList');
  if (!list) return;
  const all = presetList();
  list.replaceChildren();
  all.forEach(function (preset) { list.appendChild(renderRow(preset, all)); });
  presetMenu.endRender();
  fillModelSelect();
  const add = document.getElementById('launchPresetAdd');
  if (add) add.disabled = all.length >= MAX_PRESETS;
}

// `patchConfig(patch)` resolves true once the patch is saved and the config re-read.
export function wireLaunchPresets(patchConfig) {
  save = async function (presets, message) {
    if (await patchConfig({ launch_presets: presets })) toast(message, 'good');
  };
  const modelSelect = document.getElementById('launchPresetModel');
  const nameInput = document.getElementById('launchPresetName');
  const add = document.getElementById('launchPresetAdd');
  if (modelSelect) modelSelect.addEventListener('change', fillEffortSelect);
  if (!add || !nameInput) return;
  async function submit() {
    const name = nameInput.value.trim();
    const row = presetModels().find(function (m) { return m.value === modelSelect.value; });
    if (!name) {
      toast('Give the preset a name.', 'error');
      nameInput.focus();
      return;
    }
    if (!row) {
      toast('No model to build a preset on.', 'error');
      return;
    }
    const effort = document.getElementById('launchPresetEffort').value;
    const created = { name: name, agent: row.agent, model: row.model, effort: effort };
    const contextSelect = document.getElementById('launchPresetContext');
    if (contextSelect && contextSelect.value) created.context = contextSelect.value;
    const next = presetList().concat(created);
    if (await patchConfig({ launch_presets: next })) {
      nameInput.value = '';
      toast('Preset added.', 'good');
    }
  }
  add.addEventListener('click', submit);
  nameInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); submit(); }
  });
}
