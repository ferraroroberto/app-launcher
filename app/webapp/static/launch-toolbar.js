/* The launch toolbar (#1434, step 2/7 of #1432): the controls that shape the
 * next agent launch, as one component every launch surface mounts. Code ›
 * Projects and Life › Skills (#1439) both mount it.
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
 *
 * `switchesInMenu` (#1503 decision 6, Life only) folds the switch row into
 * the combo's menu: the model list on top (a pick commits and closes, as
 * ever), the two switches under it (they toggle in place and leave the menu
 * open), then an optional help line. The combo's label then names any
 * switch that is on ("Claude · Sonnet · Detached"), so a mode left on is
 * never out of sight. A toolbar mounted without it is byte-identical to one
 * mounted before it existed.
 */

import { setSwitch, switchEl } from './_vendored/switch/switch.js';
import { el } from './dom-utils.js';

// One labelled switch: the visible word and the vendored track in one
// <label>, so the word is part of the tap target.
function labelledSwitch(spec) {
  const wrap = el('label', 'launch-switch');
  wrap.appendChild(el('span', 'launch-switch-label', spec.text));
  const sw = switchEl(false, {
    label: spec.label,
    onToggle: function (next, btn) {
      setSwitch(btn, next);
      if (spec.onChange) spec.onChange();
    },
  });
  sw.id = spec.id;
  sw.title = spec.title;
  // A tap on the switch is handled by the switch alone. Its click handler
  // rebuilds its insides, so a tap that landed on the knob (where the knob
  // sits once it is on) leaves a detached target, and WebKit's <label> then
  // reads the click as outside its control and clicks the switch again:
  // two toggles, no change (#1503). Cancelling the click stops the label's
  // re-dispatch; a tap on the word still reaches the switch through it.
  sw.addEventListener('click', function (ev) { ev.preventDefault(); });
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
//   switchesInMenu  the switches live in the combo's menu, under the model
//              list, and the label names any that is on (#1503)
//   menuHint   with switchesInMenu: a short help line under the switches
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
  const inMenu = !!opts.switchesInMenu;
  const trigger = el('button', 'model-combo-trigger', model.label);
  trigger.type = 'button';
  trigger.id = ids.trigger;
  trigger.setAttribute('aria-haspopup', inMenu ? 'dialog' : 'listbox');
  trigger.setAttribute('aria-expanded', 'false');
  trigger.setAttribute('aria-controls', ids.menu);
  trigger.title = model.title;
  const menu = el('span', 'model-combo-menu');
  menu.id = ids.menu;
  // With the switches inside, the menu is a small dialog holding the model
  // listbox and the switch group (a listbox may hold only options).
  menu.setAttribute('role', inMenu ? 'dialog' : 'listbox');
  menu.setAttribute('aria-label', model.menuLabel);
  menu.hidden = true;
  let list = menu;
  if (inMenu) {
    menu.classList.add('launch-menu');
    list = el('span', 'model-combo-list');
    list.setAttribute('role', 'listbox');
    list.setAttribute('aria-label', model.menuLabel);
    menu.appendChild(list);
  }
  const first = el('button', null, model.label);
  first.type = 'button';
  first.setAttribute('role', 'option');
  first.dataset.value = model.value;
  first.setAttribute('aria-selected', 'true');
  list.appendChild(first);
  combo.appendChild(trigger);
  combo.appendChild(menu);
  head.appendChild(combo);
  if (opts.trailing) {
    opts.trailing.classList.add('launch-toolbar-trailing');
    head.appendChild(opts.trailing);
  }

  // In the menu, the label names the switches that are on, so the closed
  // combo still says how the next launch will run.
  const onChange = inMenu ? function () { nameSwitches(); } : null;
  const detached = labelledSwitch({
    id: ids.detached, text: 'Detached', label: 'Launch detached',
    title: opts.detachedTitle, onChange: onChange,
  });
  const resume = labelledSwitch({
    id: ids.resume, text: 'Resume', label: 'Resume session',
    title: opts.resumeTitle, onChange: onChange,
  });
  if (inMenu) {
    const group = el('span', 'launch-menu-options');
    group.setAttribute('role', 'group');
    group.setAttribute('aria-label', 'Launch options');
    group.appendChild(detached.wrap);
    group.appendChild(resume.wrap);
    if (opts.menuHint) group.appendChild(el('span', 'launch-menu-hint', opts.menuHint));
    menu.appendChild(group);
    root.appendChild(head);
  } else {
    switches.appendChild(detached.wrap);
    switches.appendChild(resume.wrap);
    root.appendChild(head);
    root.appendChild(switches);
  }

  host.replaceChildren(root);

  function on(sw) { return sw.getAttribute('aria-checked') === 'true'; }
  // The label's tail (CSS draws `data-note` after the model's name, so the
  // combo's own relabel on a pick never drops it).
  function nameSwitches() {
    const names = [];
    if (on(detached.sw)) names.push('Detached');
    if (on(resume.sw)) names.push('Resume');
    if (names.length) trigger.dataset.note = names.join(' · ');
    else delete trigger.dataset.note;
  }
  return {
    root: root,
    combo: combo,
    isDetached: function () { return on(detached.sw); },
    isResume: function () { return on(resume.sw); },
  };
}
