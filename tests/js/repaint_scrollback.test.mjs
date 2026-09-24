// Unit pin for the full-viewport repaint detector (#930). Runs under plain
// Node against the real ES module the terminal imports. Invoked by
// tests/test_repaint_scrollback.py.

import assert from 'node:assert/strict';
import { CLEAR_SCROLLBACK, createRepaintClearer } from '../../app/webapp/static/repaint-scrollback.js';

const ESC = '\x1b';
const pre = (n) => ESC + '[H' + (ESC + '[2K' + ESC + '[1B').repeat(n) + ESC + '[H';

// A full-viewport preamble gets the clear right after it, and nothing else
// changes: every byte the agent sent is still there, in order.
{
  const f = createRepaintClearer();
  const data = 'old text\r\n' + pre(5) + 'redrawn';
  const out = f(data, 5);
  assert.equal(out, 'old text\r\n' + pre(5) + CLEAR_SCROLLBACK + 'redrawn');
  assert.equal(out.replace(CLEAR_SCROLLBACK, ''), data);
}

// A partial erase is an ordinary redraw; a taller erase (rows shrank since)
// still covers the whole viewport.
{
  assert.equal(createRepaintClearer()(pre(3) + 'x', 5), pre(3) + 'x');
  assert.equal(createRepaintClearer()(pre(7) + 'x', 5), pre(7) + CLEAR_SCROLLBACK + 'x');
}

// Split across socket messages at every possible point, the clear lands
// exactly once, right after the preamble, and no byte is held back.
{
  const whole = 'before' + pre(4) + 'after';
  const want = 'before' + pre(4) + CLEAR_SCROLLBACK + 'after';
  for (let i = 1; i < whole.length; i++) {
    const f = createRepaintClearer();
    const a = f(whole.slice(0, i), 4);
    const b = f(whole.slice(i), 4);
    assert.equal(a + b, want, 'split at ' + i);
    // Each message is written whole the moment it arrives.
    assert.equal(a.replace(CLEAR_SCROLLBACK, ''), whole.slice(0, i), 'held back at ' + i);
  }
  // Byte at a time.
  const f = createRepaintClearer();
  let got = '';
  for (const ch of whole) got += f(ch, 4);
  assert.equal(got, want);
}

// Two repaints in one message, and back to back.
{
  const f = createRepaintClearer();
  const out = f(pre(2) + 'a' + pre(2) + 'b', 2);
  assert.equal(out, pre(2) + CLEAR_SCROLLBACK + 'a' + pre(2) + CLEAR_SCROLLBACK + 'b');
}

// Things that look close but are not the preamble pass through untouched:
// cursor home on its own, erase lines without the home, a home followed by
// text.
{
  const f = createRepaintClearer();
  for (const s of [ESC + '[H' + 'text', (ESC + '[2K' + ESC + '[1B').repeat(9), ESC + '[H' + ESC + '[2Kx']) {
    assert.equal(f(s, 3), s);
  }
  assert.equal(f('', 3), '');
}

console.log('repaint-scrollback: OK');
