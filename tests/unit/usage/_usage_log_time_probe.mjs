// Drives the vendored usage-log client through one scripted day on a virtual
// clock and prints every send with the minute it left the page, as JSON;
// tests/usage_log/test_usage_log_time_spent.py replays them into the real server
// half and asserts the foreground minutes.
//
// Two visits: a desktop one (tabs, a sheet, taps, a hide and show, a 20-minute
// idle gap) and a phone one (nested sheets, a sheet removed while open).

import { pathToFileURL } from 'node:url';

const sends = [];
let minute = 0;
let coarse = false;
let current = null;  // the visit whose listeners and observer are live

function define(name, value) {
  Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
}

function listeners() {
  const map = {};
  return {
    map,
    addEventListener(type, handler) { map[type] = handler; },
    removeEventListener(type) { delete map[type]; },
  };
}

const win = listeners();
const doc = listeners();
define('window', Object.assign(win, {
  matchMedia: () => ({ matches: coarse }),
  screen: { width: 1280, height: 800 },
}));
define('document', Object.assign(doc, { visibilityState: 'visible' }));
define('navigator', { userAgent: 'probe', maxTouchPoints: 0 });
define('fetch', (url, init) => {
  sends.push({ minute, body: JSON.parse(init.body) });
  return Promise.resolve({ ok: true });
});
define('MutationObserver', class {
  constructor(callback) { this.callback = callback; current.observer = this; }
  observe() {}
  disconnect() {}
});
console.warn = () => {};

const { initUsageLog } = await import(pathToFileURL(process.argv[2]).href);

function visit(device) {
  coarse = device === 'phone';
  window.screen = coarse ? { width: 390, height: 844 } : { width: 1280, height: 800 };
  const root = listeners();
  current = { root };
  current.usage = initUsageLog({ root });
  return current;
}

function sheet(id) {
  const attrs = { 'data-track-view': id };
  return {
    attrs,
    isConnected: true,
    hasAttribute: (name) => name in attrs,
    getAttribute: (name) => (name in attrs ? attrs[name] : null),
  };
}

function toggle(el, open) {
  if (open) el.attrs.open = ''; else delete el.attrs.open;
  if (current.observer) current.observer.callback([{ type: 'attributes', target: el, attributeName: 'open' }]);
}

function remove(el) {
  el.isConnected = false;
  if (current.observer) current.observer.callback([{ type: 'childList', target: {} }]);
}

function tap(id) {
  current.root.map.click({ target: { closest: () => ({ getAttribute: () => id }) } });
}

function visibility(state) {
  document.visibilityState = state;
  doc.map.visibilitychange();
}

/** Run `step` at minute `m`; whatever it queued is sent then, as a real page sends within seconds. */
function at(m, step) {
  minute = m;
  step();
  current.usage.flush();
}

let desk;
at(0, () => { desk = visit('desktop'); desk.usage.trackView('board'); });
const confirm = sheet('confirm-sheet');
at(2, () => tap('refresh-all'));
at(4, () => desk.usage.trackView('settings'));
at(5, () => toggle(confirm, true));
at(7, () => tap('confirm-yes'));
at(8, () => toggle(confirm, false));
at(10, () => visibility('hidden'));
at(12, () => visibility('visible'));
at(13, () => tap('refresh-all'));
at(33, () => tap('refresh-all'));  // after 20 idle minutes on the desktop
at(35, () => visibility('hidden'));

let phone;
document.visibilityState = 'visible';
at(60, () => { phone = visit('phone'); phone.usage.trackView('home'); });
const links = sheet('links-sheet');
const edit = sheet('link-edit');
const share = sheet('share-sheet');
at(62, () => toggle(links, true));
at(63, () => toggle(edit, true));
at(64, () => toggle(edit, false));
at(65, () => tap('link-save'));
at(66, () => toggle(links, false));
at(70, () => toggle(share, true));
at(72, () => remove(share));
at(90, () => visibility('hidden'));

console.log(JSON.stringify(sends));
