/* The one model list behind both model pickers: the chat's "Switch model"
 * sheet and the Life OS conversation's resume Model row. Both show the same
 * models, in the same order, with the same labels and the same marks, so they
 * cannot drift apart.
 */

import { icon } from '../_vendored/icons/icons.js';

// The legacy Haiku has no catalog row (Settings never offers it), but a resume
// and an in-session switch both run on it.
const HAIKU = { value: 'haiku', label: 'Haiku', available: true };

function catalogOf(agent, config) {
  const catalog = ((config || {}).model_catalog || {})[agent] || [];
  return (agent === 'claude' ? [HAIKU] : []).concat(catalog);
}

// The models a picker offers for `agent`, in the order they read: available
// ones the owner has not hidden in Settings, plus `own` (the model the
// conversation or session runs, as 'agent:id') even if hidden. `value` is
// provider-qualified; `id` is the bare id a session switch posts.
export function modelChoices(agent, config, own) {
  const hidden = new Set(((config || {}).hidden_models || []).map(String));
  return catalogOf(agent, config).filter(function (m) {
    const value = agent + ':' + m.value;
    return m.available !== false && (value === own || !hidden.has(value));
  }).map(function (m) {
    return { value: agent + ':' + m.value, label: m.label, id: m.value };
  });
}

// The provider-qualified value of the model a session names by its label, or
// '' when the catalog has none by that name.
export function modelValueForLabel(agent, config, label) {
  const hit = catalogOf(agent, config).find(function (m) { return m.label === label; });
  return hit ? agent + ':' + hit.value : '';
}

// One picker row: the label, "Its own" on the model the conversation or
// session runs, and a check on the current pick. `track` is the caller's
// constant usage id, `onPick(choice)` runs on a tap.
export function modelPickRow(choice, opts) {
  const btn = document.createElement('button');
  btn.dataset.track = opts.track;
  btn.type = 'button';
  btn.className = 'settings-row model-switch-pick';
  btn.setAttribute('role', 'listitem');
  btn.dataset.value = choice.value;
  const label = document.createElement('span');
  label.className = 'settings-row-label';
  label.textContent = choice.label;
  btn.appendChild(label);
  if (choice.value === opts.own) {
    const note = document.createElement('span');
    note.className = 'settings-row-value';
    note.textContent = 'Its own';
    btn.appendChild(note);
  }
  if (choice.value === opts.current) {
    btn.setAttribute('aria-current', 'true');
    btn.setAttribute('aria-label', choice.label + ', current model');
    const mark = document.createElement('span');
    mark.className = 'model-switch-current';
    mark.innerHTML = icon('circle-check');
    btn.appendChild(mark);
  }
  btn.addEventListener('click', function () { opts.onPick(choice); });
  return btn;
}
