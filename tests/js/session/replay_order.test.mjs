// Screen-first terminal replay (terminal/replay-order.js), under plain Node
// against the real connection module. A fake xterm models the parts that
// matter: async writes parse in order on later ticks, and the core's
// writeSync must find the queue empty (inside a write it would replay the
// chunk in flight). Invoked by tests/unit/static_ui/test_node_module_helpers.py.

import '../_browser_stubs.mjs';
import assert from 'node:assert/strict';

globalThis.location = { protocol: 'http:', host: 'test.invalid' };

const { connectTerminalWs } =
  await import('../../../app/webapp/static/terminal/terminal-connection.js');
const { isHistoryMarker } =
  await import('../../../app/webapp/static/terminal/replay-order.js');

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
const settle = async () => { for (let i = 0; i < 10; i++) await tick(); };

class FakeWs {
  constructor(url) {
    this.url = url;
    this.readyState = 1;
    this.onopen = this.onmessage = this.onerror = this.onclose = null;
  }
  send() {}
  close() { this.readyState = 3; }
}
globalThis.WebSocket = FakeWs;

function fakeTerm() {
  const parsed = [];
  const queue = [];
  let busy = false;
  const term = {
    rows: 24,
    element: { style: {} },
    buffer: { active: { viewportY: 0, baseY: 0 } },
    parsed,
    scrollToBottom() {},
    write(data, cb) {
      queue.push([data, cb]);
      if (busy) return;
      busy = true;
      setTimeout(function drain() {
        const [chunk, done] = queue.shift();
        if (chunk) parsed.push(['async', chunk]);
        if (done) done();
        if (queue.length) setTimeout(drain, 0); else busy = false;
      }, 0);
    },
    _core: {
      writeSync(data) {
        assert.equal(queue.length, 0, 'writeSync ran with writes still queued');
        parsed.push(['sync', data]);
      },
    },
  };
  return term;
}

function open(isFullscreen) {
  const terminal = { sid: 'fake-sid', tt: '', ws: null, isFullscreen, term: fakeTerm() };
  connectTerminalWs(terminal);
  return terminal;
}

const frame = (terminal, data) => terminal.ws.onmessage({ data });
const MARKER = '{"type":"history"}';
const CLEAR = '\x1b[H\x1b[2J\x1b[3J';

// The marker is the only JSON frame recognised as history.
{
  assert.equal(isHistoryMarker(MARKER), true);
  assert.equal(isHistoryMarker('{"type":"shutdown"}'), false);
  assert.equal(isHistoryMarker('{ not json'), false);
  assert.equal(isHistoryMarker('plain text'), false);
  assert.equal(isHistoryMarker(null), false);
}

// Only an inline agent asks for the screen first.
{
  assert.match(open(false).ws.url, /[?&]order=screen(&|$)/);
  assert.doesNotMatch(open(true).ws.url, /order=/);
}

// New host: the screen paints first, the full replay is parsed in one
// synchronous pass after it, and output that arrived meanwhile follows in
// order. The marker itself never reaches xterm.
{
  const t = open(false);
  frame(t, CLEAR + 'screen rows\r\n');
  frame(t, MARKER);
  frame(t, CLEAR + 'old line\r\nscreen rows\r\n');
  frame(t, 'live 1');
  frame(t, 'live 2');
  await settle();
  assert.deepEqual(t.term.parsed, [
    ['async', CLEAR + 'screen rows\r\n'],
    ['sync', CLEAR + 'old line\r\nscreen rows\r\n'],
    ['async', 'live 1'],
    ['async', 'live 2'],
  ]);
  assert.equal(t.historyHold, null);
  assert.equal(t.awaitingHistory, false);
}

// Old host (ignores order=screen): the single replay frame and live output
// are written as they always were.
{
  const t = open(false);
  frame(t, CLEAR + 'whole ring\r\n');
  frame(t, 'live');
  await settle();
  assert.deepEqual(t.term.parsed, [
    ['async', CLEAR + 'whole ring\r\n'],
    ['async', 'live'],
  ]);
}

// A reconnect before the history pass runs drops it: nothing from the old
// socket lands after the new one's frames.
{
  const t = open(false);
  frame(t, CLEAR + 'screen\r\n');
  frame(t, MARKER);
  frame(t, CLEAR + 'stale history\r\n');
  connectTerminalWs(t);
  frame(t, CLEAR + 'fresh ring\r\n');
  await settle();
  assert.deepEqual(t.term.parsed, [
    ['async', CLEAR + 'screen\r\n'],
    ['async', CLEAR + 'fresh ring\r\n'],
  ]);
}

// Without the core's writeSync the history still lands, queued in order.
{
  const t = open(false);
  delete t.term._core;
  frame(t, CLEAR + 'screen\r\n');
  frame(t, MARKER);
  frame(t, CLEAR + 'history\r\n');
  frame(t, 'live');
  await settle();
  assert.deepEqual(t.term.parsed, [
    ['async', CLEAR + 'screen\r\n'],
    ['async', CLEAR + 'history\r\n'],
    ['async', 'live'],
  ]);
}

console.log('OK');
