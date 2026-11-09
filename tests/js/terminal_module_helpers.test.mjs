// Pure-helper pins for the terminal's client modules, under plain Node: the
// keyboard-overlay height, the full-screen pan, the WS frame router, the
// repaint-batch reset on reconnect and the reconnect closing its old socket.
// None needs layout or a real event loop, so none pays a browser page load.
// Invoked by tests/unit/static_ui/test_node_module_helpers.py.

import './_browser_stubs.mjs';
import assert from 'node:assert/strict';

globalThis.location = { protocol: 'http:', host: 'test.invalid' };

const { keyboardOverlayHeight, terminalPanY, routeFrame } =
  await import('../../app/webapp/static/terminal.js');
const { beginRepaintBatch, connectTerminalWs } =
  await import('../../app/webapp/static/terminal/terminal-connection.js');

// keyboardOverlayHeight: pin the overlay to the visual viewport only for a
// keyboard-sized shrink.
{
  const full = 900;
  assert.equal(keyboardOverlayHeight(full, full), null, 'equal heights: no override');
  assert.equal(keyboardOverlayHeight(full, full - 80), null, 'URL-bar sized shrink: no override');
  assert.equal(keyboardOverlayHeight(full, full - 340), 560, 'keyboard-sized shrink: visual height');
  assert.equal(keyboardOverlayHeight(0, 500), null, 'bad layout height: no override');
  assert.equal(keyboardOverlayHeight(full, 560.6), 561, 'fractional height rounds');
}

// terminalPanY: how far a taller canvas moves up to keep the prompt visible.
{
  assert.equal(terminalPanY(900, 560), 340, 'canvas taller than the box pans the difference');
  assert.equal(terminalPanY(560, 560), 0, 'an exact fit does not pan');
  assert.equal(terminalPanY(400, 560), 0, 'a shorter canvas never pans down');
  assert.equal(terminalPanY(560.6, 200), 361, 'fractional content height rounds');
  assert.equal(terminalPanY(0, 560), 0, 'bad content height: 0');
  assert.equal(terminalPanY(900, 0), 0, 'bad box height: 0');
  assert.equal(terminalPanY(500, 300), 200);
}

// routeFrame: only a shutdown control frame is ever withheld from xterm.
{
  assert.equal(routeFrame('{"type":"shutdown"}', true), 'close-mirror');
  assert.equal(routeFrame('{"type":"shutdown"}', false), 'swallow');
  assert.equal(routeFrame('hello world\r\n', true), 'write');
  assert.equal(routeFrame('hello world\r\n', false), 'write');
  assert.equal(routeFrame('{ not valid json', true), 'write');
  assert.equal(routeFrame('{"type":"resize","rows":40}', true), 'write');
  assert.equal(routeFrame(null, true), 'write');
}

// beginRepaintBatch: a reconnect's batch starts clean, with no buffer or armed
// quiet timer left by the connection that died.
{
  const terminal = { isFullscreen: true, term: { element: { style: {} } } };
  beginRepaintBatch(terminal);
  terminal.batchBuf.push('STALE-FROM-DEAD-CONNECTION-A');
  terminal.batchQuietTimer = setTimeout(() => {}, 60000);
  beginRepaintBatch(terminal);
  assert.deepEqual(terminal.batchBuf, []);
  assert.equal(terminal.batchQuietTimer, null);
  clearTimeout(terminal.batchTimer);
}

// connectTerminalWs: reconnecting while the old socket is still OPEN closes it.
{
  class FakeWs {
    constructor(url) {
      this.url = url;
      this.readyState = 1;
      this.onopen = this.onmessage = this.onerror = this.onclose = null;
      this.closeCalled = false;
    }
    send() {}
    close() { this.closeCalled = true; this.readyState = 3; }
  }
  globalThis.WebSocket = FakeWs;
  const terminal = { sid: 'fake-sid', tt: 'fake-tt', ws: null };
  connectTerminalWs(terminal);
  const oldWs = terminal.ws;
  assert.equal(oldWs.readyState, 1, 'precondition: the old socket is OPEN');
  connectTerminalWs(terminal);
  assert.equal(oldWs.closeCalled, true, 'the previous socket is closed, not just detached');
  assert.notEqual(terminal.ws, oldWs);
}

console.log('OK');
