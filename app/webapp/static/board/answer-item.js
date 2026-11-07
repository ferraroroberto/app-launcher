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

// The longest answer text either sheet takes. It is the owner-check route's own
// bound too (src/board/owner_checks.py ANSWER_TEXT_MAX, pinned equal), so no hop
// after the field cuts text the field let through. A dictated answer stays far under.
export const ANSWER_TEXT_MAX = 20000;
// The remaining count shows only this close to the limit.
const COUNT_NEAR = 2000;

// The line under an Other field naming how much room is left, '' while far from it.
export function answerCountText(length) {
  const left = ANSWER_TEXT_MAX - length;
  if (left < 0) return (-left) + ' characters over the ' + ANSWER_TEXT_MAX + ' limit: shorten the answer';
  if (left <= COUNT_NEAR) return left + ' characters left';
  return '';
}

// The Other row: the text field, its mic when `mic(input)` returns a button, and
// the paperclip (`files`, from answer-files.js). `onInput(input)` runs on each
// keystroke or transcript, so it reads the field's text. `count` is the hint to
// append under the row: it fills in near ANSWER_TEXT_MAX, so a limit is seen
// before Done.
export function otherRow({ value, placeholder, ariaLabel, files, mic, onInput }) {
  const input = el('input', 'tr-ask-input');
  input.type = 'text';
  input.maxLength = ANSWER_TEXT_MAX;
  input.placeholder = placeholder;
  input.setAttribute('aria-label', ariaLabel);
  input.value = value;
  const count = el('p', 'tr-ask-hint');
  count.setAttribute('role', 'status');
  const recount = function () {
    count.textContent = answerCountText(input.value.length);
    count.hidden = !count.textContent;
  };
  const changed = function () { recount(); onInput(input); };
  recount();
  input.addEventListener('input', changed);
  const row = el('div', 'tr-ask-other');
  row.appendChild(input);
  const button = mic ? mic(input, changed) : null;
  if (button) row.appendChild(button);
  files.wirePaste(input);
  row.appendChild(files.button);
  return { input: input, row: row, count: count, files: files };
}

// Which option rows read as pressed: `isOn(index)` for each.
export function paintOptions(block, isOn) {
  block.querySelectorAll('.tr-ask-opt').forEach(function (b) {
    b.setAttribute('aria-pressed', isOn(Number(b.dataset.i)) ? 'true' : 'false');
  });
}
