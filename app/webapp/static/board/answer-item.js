/* The two parts of an answer-sheet item that a chief question and an owner
 * check share: the option rows and the Other row (a text field with a mic when
 * dictation is available, and the item's paperclip).
 *
 * Each sheet decides what a pick or a typed word means; this only draws the
 * controls and reports taps. board-answers.js builds a chief question from them
 * and owner-checks.js an owner check, so the owner answers both kinds of
 * question through one form.
 */

import { askOption } from '../chat/ask-option.js';
import { el } from '../dom-utils.js';

// The option rows. `options`: [{ label, description, recommended?, key? }]; a tap
// calls `onPick(index)`. `key` is the answer an owner-check row stands for, kept
// on the row as `data-answer`. `track` is the constant tracking id of every row.
export function optionList({ groupLabel, options, track, onPick }) {
  const list = el('div', 'tr-ask-options');
  list.setAttribute('role', 'group');
  list.setAttribute('aria-label', groupLabel);
  options.forEach(function (opt, index) {
    const b = askOption({ n: index + 1, label: opt.label, description: opt.description, mark: true, track: track });
    b.dataset.i = String(index);
    if (opt.recommended) b.classList.add('is-recommended');
    if (opt.key) b.dataset.answer = opt.key;
    b.addEventListener('click', function () { onPick(index); });
    list.appendChild(b);
  });
  return list;
}

// The Other row: the text field, its mic when `mic(input)` returns a button, and
// the paperclip (`files`, from answer-files.js). `onInput(input)` runs on each
// keystroke or transcript, so it reads the field's text.
export function otherRow({ value, maxLength, placeholder, ariaLabel, files, mic, onInput }) {
  const input = el('input', 'tr-ask-input');
  input.type = 'text';
  input.maxLength = maxLength;
  input.placeholder = placeholder;
  input.setAttribute('aria-label', ariaLabel);
  input.value = value;
  const changed = function () { onInput(input); };
  input.addEventListener('input', changed);
  const row = el('div', 'tr-ask-other');
  row.appendChild(input);
  const button = mic ? mic(input, changed) : null;
  if (button) row.appendChild(button);
  files.wirePaste(input);
  row.appendChild(files.button);
  return { input: input, row: row, files: files };
}

// Which option rows read as pressed: `isOn(index)` for each.
export function paintOptions(block, isOn) {
  block.querySelectorAll('.tr-ask-opt').forEach(function (b) {
    b.setAttribute('aria-pressed', isOn(Number(b.dataset.i)) ? 'true' : 'false');
  });
}
