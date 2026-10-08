/* The launch toolbar (#1434, step 2/7 of #1432): the controls that shape the
 * next agent launch, as one component every launch surface mounts. Code ›
 * Projects mounts it today; Life › Skills mounts the same one in step 7.
 *
 *   [ Claude · Opus ▾ ]              [trailing]
 *   Detached (switch)   Resume (switch)
 *
 *   model     the provider-qualified model combo (#540/#845). The toolbar
 *             only builds its markup; the caller wires it with
 *             wireModelCombo (dom-utils.js), as each surface owns its own
 *             option list and saves its choice its own way.
 *   Detached  launch in a console window on the PC, listed and killable here
 *             but with no phone terminal ('remote' mode).
 *   Resume    reopen the agent's own session picker (#151), orthogonal to
 *             Detached (#157): both on renders the picker in the console.
 *
 * Two rows, so a phone never wraps it raggedly (the three controls and a
 * labelled switch pair do not fit 390px on one line): the model with an
 * optional trailing control of the caller's pinned right, then the switch
 * pair. The two toggles are the vendored switch (accent when on,
 * design.md), each with its word beside it. The word is the switch's
 * <label>, so a tap on either toggles it. They are client-side state read at launch time, never
 * saved: a launch mode that survives a reload would surprise the next tap.
 */

import { setSwitch, switchEl } from './_vendored/switch/switch.js';

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

// One labelled switch: the visible word and the vendored track in one
// <label>, so the word is part of the tap target.
function labelledSwitch(spec) {
  const wrap = el('label', 'launch-switch');
  wrap.appendChild(el('span', 'launch-switch-label', spec.text));
  const sw = switchEl(false, {
    label: spec.label,
    onToggle: function (next, btn) { setSwitch(btn, next); },
  });
  sw.id = spec.id;
  sw.title = spec.title;
  wrap.appendChild(sw);
  return { wrap: wrap, sw: sw };
}

// opts:
//   ids        { combo, trigger, menu, detached, resume } — element ids, so
//              each surface keeps its own stable hooks
//   model      { value, label, title, menuLabel } — the combo's first option
//              and names, until the caller fills it from the catalog
//   detachedTitle / resumeTitle — each switch's hover hint
//   trailing   optional element pinned right on the model's row (Code puts
//              its git refresh there)
// Returns { root, combo, isDetached(), isResume() }.
export function mountLaunchToolbar(host, opts) {
  if (!host) return null;
  const ids = opts.ids;
  const model = opts.model;

  const root = el('div', 'launch-toolbar');
  const head = el('div', 'launch-toolbar-head');
  const switches = el('div', 'launch-toolbar-switches');

  const combo = el('span', 'model-combo');
  combo.id = ids.combo;
  combo.dataset.value = model.value;
  const trigger = el('button', 'model-combo-trigger', model.label);
  trigger.type = 'button';
  trigger.id = ids.trigger;
  trigger.setAttribute('aria-haspopup', 'listbox');
  trigger.setAttribute('aria-expanded', 'false');
  trigger.setAttribute('aria-controls', ids.menu);
  trigger.title = model.title;
  const menu = el('span', 'model-combo-menu');
  menu.id = ids.menu;
  menu.setAttribute('role', 'listbox');
  menu.setAttribute('aria-label', model.menuLabel);
  menu.hidden = true;
  const first = el('button', null, model.label);
  first.type = 'button';
  first.setAttribute('role', 'option');
  first.dataset.value = model.value;
  first.setAttribute('aria-selected', 'true');
  menu.appendChild(first);
  combo.appendChild(trigger);
  combo.appendChild(menu);
  head.appendChild(combo);
  if (opts.trailing) {
    opts.trailing.classList.add('launch-toolbar-trailing');
    head.appendChild(opts.trailing);
  }

  const detached = labelledSwitch({
    id: ids.detached, text: 'Detached', label: 'Launch detached',
    title: opts.detachedTitle,
  });
  const resume = labelledSwitch({
    id: ids.resume, text: 'Resume', label: 'Resume session',
    title: opts.resumeTitle,
  });
  switches.appendChild(detached.wrap);
  switches.appendChild(resume.wrap);
  root.appendChild(head);
  root.appendChild(switches);

  host.replaceChildren(root);

  function on(sw) { return sw.getAttribute('aria-checked') === 'true'; }
  return {
    root: root,
    combo: combo,
    isDetached: function () { return on(detached.sw); },
    isResume: function () { return on(resume.sw); },
  };
}
