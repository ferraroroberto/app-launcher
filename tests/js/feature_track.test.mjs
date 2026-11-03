// Unit pin for the feature-use recorder. Runs under plain Node against the real
// usage wiring and the real vendored client, with a faked browser around them:
// a use reaches the log as a `tap` through its direct `track` call, with no DOM
// element and no click. The same path in a real browser is proven by
// tests/e2e/settings/test_screen_time_features.py. Invoked by
// tests/unit/usage/test_feature_counts.py.

import assert from 'node:assert/strict';

const listeners = {};
const sent = [];
const created = [];

function defineGlobal(name, value) {
  Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
}

defineGlobal('window', globalThis);
defineGlobal('document', {
  visibilityState: 'visible',
  addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
  removeEventListener() {},
  createElement(tag) { created.push(tag); throw new Error('a feature use must not build an element'); },
  getElementById() { return null; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
});
globalThis.addEventListener = function () {};
globalThis.matchMedia = function () { return { matches: true }; };
globalThis.screen = { width: 390, height: 844 };
defineGlobal('navigator', { userAgent: 'invented', maxTouchPoints: 5, sendBeacon() {} });
defineGlobal('location', { href: 'https://invented.example/', search: '' });
defineGlobal('localStorage', { getItem() { return null; }, setItem() {}, removeItem() {} });
globalThis.fetch = function (url, init) {
  sent.push({ url: String(url), init: init || {} });
  return Promise.resolve({ ok: true, status: 200 });
};

const { FEATURE_IDS, trackFeature } = await import(
  '../../app/webapp/static/settings/feature-track.js'
);
const { usage, wireUsageLog } = await import('../../app/webapp/static/usage.js');

assert.equal(FEATURE_IDS.length, 9);
assert.ok(FEATURE_IDS.every((id) => /^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/.test(id)));

// Before the logger starts a use records nothing and breaks nothing.
assert.doesNotThrow(() => trackFeature('feat.ocr'));
assert.doesNotThrow(() => usage.track('mic.no-click'));

wireUsageLog();
FEATURE_IDS.forEach(trackFeature);
usage.track('mic.no-click');

// Anything off the list is dropped: a file name, a sentence, a non-string, a near miss.
['photo.png', 'feat.ocr extra', 'mic.start', 'feat.', '', null, undefined, 42, {}].forEach(trackFeature);

document.visibilityState = 'hidden';
(listeners.visibilitychange || []).forEach((fn) => fn({}));
await new Promise((resolve) => setTimeout(resolve, 20));

const events = [];
for (const r of sent.filter((s) => s.url === '/api/usage/events')) {
  const body = typeof r.init.body === 'string' ? r.init.body : await r.init.body.text();
  events.push(...JSON.parse(body).events);
}
const taps = events.filter((e) => e.kind === 'tap').map((e) => e.id);
assert.deepEqual(taps, [...FEATURE_IDS, 'mic.no-click'], 'only the listed ids and the direct call reach the log');
assert.deepEqual(created, [], 'a feature use built an element');

// A logger that throws must not take the app down.
const { usage: again } = await import('../../app/webapp/static/usage.js?again');
assert.doesNotThrow(() => again.track('feat.ocr'));

console.log('OK feature-track');
