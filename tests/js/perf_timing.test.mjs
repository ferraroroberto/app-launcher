// Unit pin for perf-timing.js's leg maths and path filter (#1924): the rows a
// phone posts are pure functions of Performance entries, pinned here under
// plain Node with invented entries. Invoked by tests/unit/usage/test_perf_timings.py.

import './_browser_stubs.mjs';
import assert from 'node:assert/strict';

const { apiPath, apiRow, lastBootRequest, loadRow, serverMs, span, SLOW_API_MS } =
  await import('../../app/webapp/static/perf-timing.js');

const ORIGIN = 'https://launcher.example';

assert.equal(SLOW_API_MS, 800);

// span: a gap only when both ends are known and ordered.
assert.equal(span(10, 25.4), 15);
assert.equal(span(0, 25), null);
assert.equal(span(30, 25), null);

// Only same-origin /api/ paths, query dropped, its own sends skipped.
assert.equal(apiPath(ORIGIN + '/api/board?since=1', ORIGIN), '/api/board');
assert.equal(apiPath(ORIGIN + '/static/main.js', ORIGIN), null);
assert.equal(apiPath('https://other.example/api/board', ORIGIN), null);
assert.equal(apiPath(ORIGIN + '/api/perf/timings', ORIGIN), null);
assert.equal(apiPath(ORIGIN + '/api/usage/events', ORIGIN), null);

// Server-Timing: the `app` entry, or unknown (null), never zero.
assert.equal(serverMs({ serverTiming: [{ name: 'db', duration: 5 }, { name: 'app', duration: 41.6 }] }), 42);
assert.equal(serverMs({ serverTiming: [] }), null);
assert.equal(serverMs({}), null);

// A slow request on a fresh TLS connection.
const slow = {
  name: ORIGIN + '/api/jobs', initiatorType: 'fetch', startTime: 100, duration: 1200,
  domainLookupStart: 100, domainLookupEnd: 110,
  connectStart: 110, secureConnectionStart: 150, connectEnd: 200,
  requestStart: 260, responseStart: 1000, responseEnd: 1300,
  serverTiming: [{ name: 'app', duration: 300 }], responseStatus: 200,
};
assert.deepEqual(apiRow(slow, '/api/jobs'), {
  kind: 'api', status: 200, path: '/api/jobs',
  queue: 60,        // 160 ms before the request, minus 10 DNS and 90 connect+TLS
  connect: 90, wait: 740, server: 300, download: 300, total: 1200,
});
// A reused socket: no set-up, all the pre-request time is queue.
const reused = Object.assign({}, slow, {
  domainLookupStart: 100, domainLookupEnd: 100, connectStart: 100, secureConnectionStart: 0, connectEnd: 100,
  requestStart: 400, serverTiming: undefined, responseStatus: undefined,
});
const r = apiRow(reused, '/api/jobs');
assert.equal(r.queue, 300);
assert.equal(r.server, null);
assert.equal(r.status, 0);

// The boot request that landed last before boot ended; streams and later
// requests do not count.
const entries = [
  { name: ORIGIN + '/api/config', initiatorType: 'fetch', requestStart: 50, responseStart: 80, responseEnd: 90 },
  { name: ORIGIN + '/api/git-status?quiet=1', initiatorType: 'fetch', requestStart: 60, responseStart: 700, responseEnd: 720,
    serverTiming: [{ name: 'app', duration: 600 }] },
  { name: ORIGIN + '/api/voice/events', initiatorType: 'other', requestStart: 60, responseStart: 70, responseEnd: 750 },
  { name: ORIGIN + '/api/board', initiatorType: 'fetch', requestStart: 800, responseStart: 900, responseEnd: 950 },
];
assert.deepEqual(lastBootRequest(entries, 760, ORIGIN), {
  last_path: '/api/git-status', last_wait: 640, last_server: 600, last_download: 20,
});
assert.equal(lastBootRequest([], 760, ORIGIN).last_path, null);

// The load row: document legs plus boot.
const nav = {
  type: 'reload', transferSize: 0, decodedBodySize: 5000,
  domainLookupStart: 5, domainLookupEnd: 5, connectStart: 5, secureConnectionStart: 0, connectEnd: 5,
  requestStart: 10, responseStart: 40, responseEnd: 55, domContentLoadedEventEnd: 120.4,
};
const load = loadRow(nav, 760.6, entries, ORIGIN);
assert.equal(load.kind, 'load');
assert.equal(load.nav_type, 'reload');
assert.equal(load.cached, true);
assert.equal(load.ttfb, 30);
assert.equal(load.download, 15);
assert.equal(load.dom_ready, 120);
assert.equal(load.boot, 761);
assert.equal(load.tls, null);
assert.equal(load.last_path, '/api/git-status');

console.log('OK');
