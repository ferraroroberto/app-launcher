/* Launch presets in the launch toolbars (Coding and Life OS).
 *
 * A preset is a named model + effort set in Settings › Launch defaults. Each
 * surface gets a second dropdown beside its model one: "Default" (today's
 * behaviour: the model dropdown's pick, the saved effort) and one row per
 * preset. Picking a preset sets the surface's model through the surface's own
 * path, and remembers the pick on this device. A launch then sends the
 * preset's effort for as long as the model dropdown still shows the preset's
 * model; a model picked by hand afterwards turns the preset off by itself, so
 * the effort never outlives the model it was chosen for.
 *
 * Nothing is drawn while no preset exists, so a toolbar without presets is
 * byte-identical to one built before this existed. Model ids and labels come
 * from the config's catalog (modelChoices), never from a list here.
 */

import { el } from './dom-utils.js';
import { wireModelCombo } from './shared/model-combo.js';
import { modelChoices } from './shared/model-choices.js';

const DEFAULT_VALUE = 'default';

// surface key -> { picker, combo, presets, activeId, restored }
const surfaces = new Map();

function storageKey(key) {
  return 'app-launcher.launch-preset.' + key;
}

function readActive(key) {
  try { return window.localStorage.getItem(storageKey(key)) || ''; } catch (_e) { return ''; }
}

function writeActive(key, id) {
  try {
    if (id) window.localStorage.setItem(storageKey(key), id);
    else window.localStorage.removeItem(storageKey(key));
  } catch (_e) { /* storage blocked: the pick lasts for this page only */ }
}

function presetModel(preset) {
  return preset.agent + ':' + preset.model;
}

// The saved presets whose model the dropdowns still offer: a model hidden in
// Settings (or dropped from the catalog) takes its presets out of the list,
// they come back with the model.
function offeredPresets(config) {
  return ((config && config.launch_presets) || []).filter(function (preset) {
    return modelChoices(preset.agent, config).some(function (choice) {
      return choice.value === presetModel(preset);
    });
  });
}

function pickerMarkup(key) {
  const root = el('span', 'model-combo launch-preset-combo');
  root.id = key + 'PresetCombo';
  root.dataset.value = DEFAULT_VALUE;
  const trigger = el('button', 'model-combo-trigger', 'Default');
  trigger.type = 'button';
  trigger.id = key + 'PresetBtn';
  trigger.dataset.track = 'launch-preset.combo';
  trigger.setAttribute('aria-haspopup', 'listbox');
  trigger.setAttribute('aria-expanded', 'false');
  trigger.setAttribute('aria-controls', key + 'PresetMenu');
  trigger.title = 'Launch preset: a model with the effort it runs at';
  const menu = el('span', 'model-combo-menu');
  menu.id = key + 'PresetMenu';
  menu.setAttribute('role', 'listbox');
  menu.setAttribute('aria-label', 'Launch preset');
  menu.hidden = true;
  root.append(trigger, menu);
  return root;
}

function comboModel(spec) {
  const combo = document.getElementById(spec.comboId);
  return combo ? combo.dataset.value || '' : '';
}

// The preset in force: the remembered one, while the model dropdown shows its model.
function activePreset(state, spec) {
  const preset = state.presets.find(function (p) { return p.id === state.activeId; });
  return preset && presetModel(preset) === comboModel(spec) ? preset : null;
}

function syncLabel(state, spec) {
  if (!state.picker) return;
  const preset = activePreset(state, spec);
  state.controller.setValue(preset ? preset.id : DEFAULT_VALUE);
}

function ensurePicker(state, spec) {
  if (state.picker) return;
  const anchor = document.getElementById(spec.comboId);
  if (!anchor) return;
  state.picker = pickerMarkup(spec.key);
  anchor.after(state.picker);
  state.controller = wireModelCombo(state.picker, function (value) {
    const preset = state.presets.find(function (p) { return p.id === value; });
    state.activeId = preset ? preset.id : '';
    writeActive(spec.key, state.activeId);
    if (preset) spec.select(presetModel(preset));
    syncLabel(state, spec);
  });
  // A model picked by hand, or restored from the server, changes the combo's
  // data-value: the label follows it back to Default.
  new MutationObserver(function () { syncLabel(state, spec); })
    .observe(anchor, { attributes: true, attributeFilter: ['data-value'] });
}

function removePicker(state) {
  if (!state.picker) return;
  state.controller.destroy();
  state.picker.remove();
  state.picker = null;
  state.controller = null;
}

// Redraw each surface's dropdown from `config`. Called whenever the config
// lands, after the model dropdowns' own options are set.
export function syncLaunchPresets(config, specs) {
  specs.forEach(function (spec) {
    let state = surfaces.get(spec.key);
    if (!state) {
      state = { picker: null, controller: null, presets: [], activeId: readActive(spec.key), restored: false };
      surfaces.set(spec.key, state);
    }
    state.presets = offeredPresets(config);
    if (!state.presets.length) {
      removePicker(state);
      return;
    }
    ensurePicker(state, spec);
    if (!state.picker) return;
    const options = [{ value: DEFAULT_VALUE, label: 'Default' }].concat(
      state.presets.map(function (p) { return { value: p.id, label: p.name, title: p.name + ' · ' + p.effort }; })
    );
    state.controller.setOptions(options);
    // A surface whose model is not saved on the server (Life OS) forgets it on
    // a reload: put the remembered preset's model back once.
    const wanted = state.presets.find(function (p) { return p.id === state.activeId; });
    if (spec.restore && !state.restored && wanted && presetModel(wanted) !== comboModel(spec)) {
      spec.select(presetModel(wanted));
    }
    state.restored = true;
    syncLabel(state, spec);
  });
}

// The effort a launch of `modelValue` ('agent:id') from surface `key` sends,
// '' when no preset is in force for that model.
export function presetEffortFor(key, modelValue) {
  const state = surfaces.get(key);
  if (!state || !state.picker) return '';
  const preset = state.presets.find(function (p) { return p.id === state.activeId; });
  return preset && presetModel(preset) === modelValue ? preset.effort : '';
}

// The preset's name for a launch of `modelValue` from surface `key`, for a
// toast; '' when none is in force.
export function presetNameFor(key, modelValue) {
  const state = surfaces.get(key);
  if (!presetEffortFor(key, modelValue)) return '';
  const preset = state.presets.find(function (p) { return p.id === state.activeId; });
  return preset ? preset.name : '';
}
