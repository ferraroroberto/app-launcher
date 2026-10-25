// Privacy and origin pin for the usage log's browser half (#1524). Runs under
// plain Node against the real vendored client and the real wiring module
// (static/usage.js), with just enough of a browser faked around them.
// Invoked by tests/unit/usage/test_usage_log_adoption.py.
//
// Every value below is invented. The sentinels stand in for the things the
// contract forbids: message text, names, queries, form values, a URL with a
// query string, and the raw user-agent. None may appear in anything sent.

import assert from 'node:assert/strict';

const SENTINELS = [
  'SECRET-UA-TOKEN', 'SECRET-QUERY', 'SECRET-BUTTON-TEXT', 'SECRET-FORM-VALUE',
  'SECRET-USER-NAME', 'SECRET-SEARCH-TERM', 'invented.example',
];

// --------------------------------------------------------- a faked browser
const listeners = { document: {}, window: {} };
function on(bucket) {
  return function (type, fn) { (listeners[bucket][type] ||= []).push(fn); };
}
function fire(bucket, type, event) {
  (listeners[bucket][type] || []).forEach(function (fn) { fn(event || {}); });
}

const sent = [];            // every request the page made, whatever the API

function defineGlobal(name, value) {
  Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
}

defineGlobal('window', globalThis);
defineGlobal('document', {
  visibilityState: 'visible',
  addEventListener: on('document'),
  removeEventListener() {},
  // state.js looks its elements up at import; none exist in this fake page.
  getElementById() { return null; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
});
globalThis.addEventListener = on('window');
globalThis.matchMedia = function () { return { matches: true }; };   // coarse pointer
globalThis.screen = { width: 390, height: 844 };                      // short side 390 -> phone
defineGlobal('navigator', {
  userAgent: 'Mozilla/5.0 (iPhone; SECRET-UA-TOKEN) AppleWebKit',
  maxTouchPoints: 5,
  sendBeacon() { throw new Error('the page called the real sendBeacon'); },
});
defineGlobal('location', {
  href: 'https://invented.example/?q=SECRET-SEARCH-TERM&token=SECRET-QUERY',
  search: '?q=SECRET-SEARCH-TERM&token=SECRET-QUERY',
});
const store = new Map([['app-launcher.token', 'invented-bearer']]);
defineGlobal('localStorage', {
  getItem(k) { return store.has(k) ? store.get(k) : null; },
  setItem(k, v) { store.set(k, String(v)); },
  removeItem(k) { store.delete(k); },
});
globalThis.fetch = function (url, init) {
  sent.push({ url: String(url), init: init || {} });
  return Promise.resolve({ ok: true, status: 200 });
};

const { TOKEN_KEY } = await import('../../app/webapp/static/state.js');
store.set(TOKEN_KEY, 'invented-bearer');
const { wireUsageLog, USAGE_ENDPOINT } = await import('../../app/webapp/static/usage.js');
const { initUsageLog } = await import('../../app/webapp/static/_vendored/usage-log/usage-log.js');

function control(track, text) {
  const el = {
    textContent: text,
    value: 'SECRET-FORM-VALUE',
    getAttribute(name) { return name === 'data-track' ? track : null; },
    closest(sel) { return sel === '[data-track]' && track != null ? el : null; },
  };
  return el;
}

// ------------------------------------------------------------- drive a visit
const fetchBefore = globalThis.fetch;
const beaconBefore = navigator.sendBeacon;
wireUsageLog();
fire('document', 'launcher:tab', { detail: { tab: 'board' } });
fire('document', 'click', { target: control('board-refresh', 'SECRET-BUTTON-TEXT') });
// A control with no data-track, whose text and value are private: nothing.
fire('document', 'click', { target: control(null, 'SECRET-USER-NAME') });
// A data-track that is a sentence or a URL (a developer mistake) is dropped.
fire('document', 'click', { target: control('SECRET-USER-NAME wrote this', 'x') });
fire('document', 'click', { target: control('https://invented.example/a?q=SECRET-QUERY', 'x') });
fire('document', 'launcher:tab', { detail: { tab: 'settings' } });
fire('document', 'click', { target: control('settings.tokens', 'x') });

// The page goes hidden: with a `headers` option the client sends a keepalive
// fetch, since sendBeacon cannot carry the bearer header.
document.visibilityState = 'hidden';
fire('document', 'visibilitychange');
await new Promise(function (resolve) { setTimeout(resolve, 20); });

// ---------------------------------------------------------- the assertions
const usagePosts = sent.filter(function (r) { return r.url === USAGE_ENDPOINT; });
assert.ok(usagePosts.length >= 1, 'the visit produced no usage request');

// 1. Only the app's own origin: every request is a same-origin path.
sent.forEach(function (r) {
  assert.ok(r.url.startsWith('/') && !r.url.startsWith('//'), 'off-origin request: ' + r.url);
});
assert.equal(USAGE_ENDPOINT, '/api/usage/events');

// 2. The bearer token is on every usage request (a phone is not loopback),
//    including the page-hidden one (a keepalive fetch, never a beacon).
usagePosts.forEach(function (r) {
  assert.equal(r.init.headers.Authorization, 'Bearer invented-bearer');
  assert.equal(r.init.headers['Content-Type'], 'application/json');
  assert.equal(r.init.method, 'POST');
  assert.equal(r.init.credentials, 'same-origin');
});
assert.ok(usagePosts.some(function (r) { return r.init.keepalive === true; }),
  'the page-hidden flush was not a keepalive fetch');

// 3. Nothing global is wrapped: window.fetch and navigator.sendBeacon are the
//    very functions the page had before wireUsageLog(), so every other request
//    goes through untouched.
assert.equal(globalThis.fetch, fetchBefore, 'window.fetch was replaced');
assert.equal(navigator.sendBeacon, beaconBefore, 'navigator.sendBeacon was replaced');
await fetch('/api/other', { headers: { Accept: 'x' } });
const other = sent[sent.length - 1];
assert.equal(other.url, '/api/other');
assert.deepEqual(other.init.headers, { Accept: 'x' }, 'a non-usage request was modified');

// 4. What was sent: an exact envelope, an allowlist of kinds, bounded ids, and
//    none of the forbidden values anywhere in the bytes.
const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/;
const KINDS = new Set(['session_start', 'view', 'tap', 'session_end']);
const events = [];
for (const r of usagePosts) {
  const text = typeof r.init.body === 'string' ? r.init.body : await r.init.body.text();
  SENTINELS.forEach(function (s) { assert.ok(!text.includes(s), 'forbidden value sent: ' + s); });
  const body = JSON.parse(text);
  assert.deepEqual(Object.keys(body).sort(), ['device_class', 'events', 'os_class', 'session_id']);
  assert.equal(body.device_class, 'phone');
  assert.equal(body.os_class, 'ios');
  assert.match(body.session_id, /^[A-Za-z0-9-]{8,64}$/);
  body.events.forEach(function (e) {
    assert.ok(KINDS.has(e.kind), 'unknown kind ' + e.kind);
    assert.deepEqual(
      Object.keys(e).filter(function (k) { return !['kind', 'id', 'view'].includes(k); }), [],
      'event carries an unexpected field: ' + JSON.stringify(e),
    );
    if (e.id !== undefined) assert.match(e.id, ID);
    if (e.view !== undefined) assert.match(e.view, ID);
    events.push(e);
  });
}
const key = events.map(function (e) { return [e.kind, e.id, e.view].filter(Boolean).join('|'); });
assert.deepEqual(key, [
  'session_start',
  'view|board',
  'tap|board-refresh|board',
  'view|settings',
  'tap|settings.tokens|settings',
  'session_end',
]);

// 5. An off-origin endpoint disables the logger instead of sending anywhere.
const before = sent.length;
const warn = console.warn;
console.warn = function () {};
const off = initUsageLog({ endpoint: 'https://invented.example/collect' });
const proto = initUsageLog({ endpoint: '//invented.example/collect' });
console.warn = warn;
off.trackView('board'); off.flush(); proto.trackView('board'); proto.flush();
assert.equal(sent.length, before, 'a disabled logger still sent a request');

console.log('OK');
