/* The exact step of an owner check: the `run` lines in a copyable block and
 * `expect` under them, drawn in a row's detail and in the answer sheet.
 *
 * Lines that happen in the same place share one block (the design system's
 * code-listing frame: the place as its caption, an icon-button Copy), so a
 * copy never mixes a terminal command with a screen to look at. A field the
 * note does not carry says so in its own line, and an issue with no note says
 * why there is none; nothing is guessed.
 */

import { el } from '../dom-utils.js';
import { icon } from '../_vendored/icons/icons.js';
import { copyText } from '../apps/apps-listeners.js';

// fleet-config's `where` values (docs/owner-check.md "The exact step").
const WHERE_LABEL = {
  terminal: 'Terminal',
  elevated: 'Elevated shell',
  browser: 'Browser on the PC',
  phone: 'Phone',
  screen: 'Screen',
};
const COMMANDS = ['terminal', 'elevated'];

// Why an issue carries the label but no note, by the list's `note` state.
const NO_NOTE = {
  missing: 'No owner-check note on this issue, so no command is recorded.',
  unrepairable: 'No note could be written: nothing on the issue names a step, so no command is recorded.',
  unknown: 'Writing the missing note was not confirmed, so the command is unknown. The next refresh tries again.',
};

export function noNoteText(item) {
  return (NO_NOTE[item.note] || NO_NOTE.missing) + ' Open the issue to see what it needs.';
}

// The collapsed row's chip for an item with no note.
export function noNoteChip(item) {
  return item.note === 'unknown' ? 'note unconfirmed' : 'no note';
}

// Consecutive lines of one place, in order: [{ where, text }] with the texts
// joined one per line, which is what Copy puts on the clipboard.
export function runBlocks(run) {
  const blocks = [];
  (run || []).forEach(function (line) {
    const last = blocks[blocks.length - 1];
    if (last && last.where === line.where) last.text += '\n' + line.text;
    else blocks.push({ where: line.where, text: line.text });
  });
  return blocks;
}

function copy(button, block) {
  const dialog = button.closest('dialog');
  copyText(block.text, COMMANDS.indexOf(block.where) === -1 ? 'Step' : 'Command', {
    // A modal sheet covers the page's toast, so it carries its own.
    host: (dialog && dialog.querySelector('.sheet-toast')) || undefined,
    byHand: 'Select it and copy by hand.',
  });
}

function runBlock(block) {
  const label = WHERE_LABEL[block.where] || 'Run';
  const frame = el('div', 'md-block board-owner-run');
  const head = el('div', 'md-block-head');
  head.appendChild(el('span', 'md-block-lang', label));
  const button = el('button', 'icon-button board-owner-copy');
  button.type = 'button';
  button.dataset.track = 'board.owner-copy-step';
  button.setAttribute('aria-label', 'Copy ' + label.toLowerCase() + ' step');
  button.title = 'Copy';
  button.innerHTML = icon('copy');
  button.addEventListener('click', function () { copy(button, block); });
  head.appendChild(button);
  frame.appendChild(head);
  const pre = el('pre', 'md-code');
  pre.appendChild(el('code', null, block.text));
  frame.appendChild(pre);
  return frame;
}

// The nodes for an item that has a note; `cls` is the surrounding line class.
export function stepNodes(item, cls) {
  const nodes = runBlocks(item.run).map(runBlock);
  if (!nodes.length) nodes.push(el('p', cls + ' board-owner-no-run', 'No command recorded.'));
  if (item.expect) {
    const p = el('p', cls + ' board-owner-expect');
    p.appendChild(el('strong', null, 'Expect: '));
    p.appendChild(document.createTextNode(item.expect));
    nodes.push(p);
  } else {
    nodes.push(el('p', cls + ' board-owner-no-expect', 'No expected result recorded.'));
  }
  return nodes;
}
