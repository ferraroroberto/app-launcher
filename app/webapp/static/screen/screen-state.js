/* Screen view's shared state: the one `view` record every Screen module
 * reads and writes, and the element lookup they share. screen-view.js owns
 * the socket and the lifecycle; screen-control.js, screen-keys.js and
 * screen-quality-sheet.js own their parts of the dialog.
 */

export const view = {
  open: false,
  standalone: false,   // this window is the pop-out: closing closes it
  monitors: [],
  monitorId: null,
  ws: null,
  connectGen: 0,       // bumped by every socket teardown: a connect() older than it is stale
  attempt: 0,          // automatic reconnects tried since the last drawn frame
  retryTimer: null,    // the pending automatic reconnect
  frameUrl: '',
  lastFrameAt: 0,
  zoom: { scale: 1, x: 0, y: 0 },
  mode: 'view',        // 'view' | 'control'
  control: 'touch',    // Control's style: 'touch' | 'trackpad'
  live: false,         // a frame is drawn and the desktop is readable
  mods: new Set(),     // sticky modifiers for the next key
  cursor: null,        // the last cursor message: {x, y, shape, visible}
  pad: null,           // the trackpad's cursor, {x, y} 0-1 in the frame
  padSentAt: 0,        // when input that moves the PC's cursor last went out
  preset: 'balanced',  // the quality preset on the connect query
  readoutOn: false,    // the readout overlay is up (and the server sends stats)
  readout: null,       // the client's Readout while it is up
  serverStats: null,   // the last `stats` message from the server
};

export function $(id) { return document.getElementById(id); }
