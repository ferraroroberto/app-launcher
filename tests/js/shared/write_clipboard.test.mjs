// Unit pin for the shared clipboard write: `writeText` when the page has it,
// the selected-textarea `execCommand` fallback when it is missing (plain http)
// or refuses, and a rejection when neither lands. Runs under plain Node;
// invoked by tests/unit/static_ui/test_write_clipboard.py.

import '../_browser_stubs.mjs';
import assert from 'node:assert/strict';

const { writeClipboard } = await import('../../../app/webapp/static/api.js');

let copied;
let appended;
let focusedBack;

// A fake DOM just deep enough for the fallback: one textarea, one root.
function installDom({ execResult, execThrows = false, dialog = null }) {
  copied = [];
  appended = [];
  focusedBack = false;
  const previous = { focus: () => { focusedBack = true; }, closest: () => dialog };
  const root = { appendChild: (node) => appended.push(node) };
  globalThis.document = {
    body: root,
    activeElement: previous,
    createElement: () => ({
      setAttribute() {},
      style: {},
      focus() {},
      select() {},
      setSelectionRange() {},
      remove() { this.removed = true; },
    }),
    execCommand: (cmd) => {
      assert.equal(cmd, 'copy');
      if (execThrows) throw new Error('denied');
      copied.push(appended[appended.length - 1].value);
      return execResult;
    },
  };
  return root;
}

function setClipboard(clipboard) {
  Object.defineProperty(globalThis, 'navigator', { value: { clipboard }, configurable: true });
}

// 1. writeText present and working: used, no fallback touched.
installDom({ execResult: true });
const written = [];
setClipboard({ writeText: async (t) => { written.push(t); } });
await writeClipboard('https');
assert.deepEqual(written, ['https']);
assert.equal(appended.length, 0);

// 2. No navigator.clipboard (plain http): the fallback copies the text.
installDom({ execResult: true });
setClipboard(undefined);
await writeClipboard('over http');
assert.deepEqual(copied, ['over http']);
assert.equal(appended[0].removed, true, 'the hidden textarea is removed');
assert.equal(focusedBack, true, 'focus returns to the element that had it');

// 3. writeText rejects (unfocused document): the fallback still copies.
installDom({ execResult: true });
setClipboard({ writeText: async () => { throw new Error('not focused'); } });
await writeClipboard('refused');
assert.deepEqual(copied, ['refused']);

// 4. Inside an open modal dialog the textarea goes in the dialog, not <body>.
const inModal = [];
const modal = { appendChild: (node) => { inModal.push(node); appended.push(node); } };
installDom({ execResult: true, dialog: modal });
setClipboard(undefined);
await writeClipboard('in a sheet');
assert.deepEqual(copied, ['in a sheet']);
assert.equal(inModal.length, 1, 'the textarea sits inside the open dialog');

// 5. Neither path lands: rejects with the clipboard's own failure when it had
// one, and with a plain message when the page has no clipboard API at all.
installDom({ execResult: false });
setClipboard({ writeText: async () => { throw new Error('not focused'); } });
await assert.rejects(writeClipboard('x'), /not focused/);

installDom({ execResult: false });
setClipboard(undefined);
await assert.rejects(writeClipboard('x'), /no clipboard access/);

installDom({ execThrows: true });
setClipboard(undefined);
await assert.rejects(writeClipboard('x'), /no clipboard access/);

console.log('OK');
