/* Real-device timings: one row per page load and one per slow /api/ request,
 * so an "it was slow" report can be traced to the connection, the server or
 * the page. The server half is src/usage/perf_timings.py; how to read the
 * legs is docs/perf-timings.md.
 *
 * Collected with one PerformanceObserver on resource entries: api() and fetch
 * are never wrapped, and a fast request costs one comparison. Rows are sent
 * at most once per flush interval, only when there are any, and with a
 * keepalive fetch when the page hides (a beacon cannot carry the bearer
 * header). The raw path goes to this app's own server, which replaces it with
 * its route template before anything is stored; the query string never
 * leaves the page. Every failure is swallowed: timings never break the app.
 */

import { deviceClass, osClass } from './_vendored/usage-log/usage-log.js';

export const ENDPOINT = '/api/perf/timings';
// Mirrors src/usage/perf_timings.py SLOW_API_MS and MAX_ROWS_PER_BATCH.
export const SLOW_API_MS = 800;
const MAX_BATCH = 50;
const FLUSH_MS = 10000;
// Its own sends and the usage log's: timing them would only time the timer.
const SKIP_PATHS = new Set([ENDPOINT, '/api/usage/events']);
// EventSource and WebSocket entries are not fetch/xhr, so streams never count.
const INITIATORS = new Set(['fetch', 'xmlhttprequest']);

function noop() {}

// A non-negative whole-ms gap, or null when either end is missing.
export function span(from, to) {
  if (!(from > 0) || !(to >= from)) return null;
  return Math.round(to - from);
}

function roundMs(value) {
  return Number.isFinite(value) && value >= 0 ? Math.round(value) : null;
}

// The `app` entry of the response's Server-Timing header, or null when the
// browser does not expose it (the leg then reads unknown, never zero).
export function serverMs(entry) {
  const list = entry && entry.serverTiming;
  if (!list || !list.length) return null;
  for (let i = 0; i < list.length; i++) {
    if (list[i].name === 'app') return roundMs(list[i].duration);
  }
  return null;
}

// Same-origin /api/ pathname of a resource entry, query dropped; null for
// anything else.
export function apiPath(name, origin) {
  let url;
  try { url = new URL(name, origin); } catch (_) { return null; }
  if (url.origin !== origin || !url.pathname.startsWith('/api/')) return null;
  return SKIP_PATHS.has(url.pathname) ? null : url.pathname;
}

// Connection set-up split into connect and TLS; both 0 on a reused socket.
function connectLegs(e) {
  const tlsStart = e.secureConnectionStart > 0 ? e.secureConnectionStart : 0;
  return {
    connect: span(e.connectStart, tlsStart || e.connectEnd),
    tls: tlsStart ? span(tlsStart, e.connectEnd) : null,
  };
}

// The slow-API row for one resource entry.
export function apiRow(entry, path) {
  const c = connectLegs(entry);
  const dns = span(entry.domainLookupStart, entry.domainLookupEnd) || 0;
  const setup = dns + (c.connect || 0) + (c.tls || 0);
  return {
    kind: 'api',
    // Not every engine exposes the status; 0 means unknown.
    status: Number.isInteger(entry.responseStatus) ? entry.responseStatus : 0,
    path: path,
    // Time before the request went out that was not DNS or connect: a
    // socket or HTTP/2 backlog.
    queue: entry.requestStart > 0 ? Math.max(0, Math.round(entry.requestStart - entry.startTime - setup)) : null,
    connect: (c.connect === null && c.tls === null) ? null : (c.connect || 0) + (c.tls || 0),
    wait: span(entry.requestStart, entry.responseStart),
    server: serverMs(entry),
    download: span(entry.responseStart, entry.responseEnd),
    total: roundMs(entry.duration),
  };
}

// The boot request that landed last, before `bootEnd`.
export function lastBootRequest(entries, bootEnd, origin) {
  let last = null;
  for (const e of entries) {
    if (!INITIATORS.has(e.initiatorType) || !(e.responseEnd <= bootEnd)) continue;
    const path = apiPath(e.name, origin);
    if (path && (!last || e.responseEnd > last.entry.responseEnd)) last = { entry: e, path: path };
  }
  if (!last) return { last_path: null, last_wait: null, last_server: null, last_download: null };
  return {
    last_path: last.path,
    last_wait: span(last.entry.requestStart, last.entry.responseStart),
    last_server: serverMs(last.entry),
    last_download: span(last.entry.responseStart, last.entry.responseEnd),
  };
}

// The load row: the document's Navigation Timing legs plus boot.
export function loadRow(nav, bootMs, entries, origin) {
  const c = connectLegs(nav);
  let cached = null;
  if (Number.isFinite(nav.transferSize)) cached = nav.transferSize === 0 && nav.decodedBodySize > 0;
  return Object.assign({
    kind: 'load',
    nav_type: nav.type,
    cached: cached,
    dns: span(nav.domainLookupStart, nav.domainLookupEnd),
    connect: c.connect,
    tls: c.tls,
    ttfb: span(nav.requestStart, nav.responseStart),
    download: span(nav.responseStart, nav.responseEnd),
    dom_ready: nav.domContentLoadedEventEnd > 0 ? Math.round(nav.domContentLoadedEventEnd) : null,
    boot: roundMs(bootMs),
  }, lastBootRequest(entries, bootMs, origin));
}

function displayMode() {
  try {
    if (window.matchMedia('(display-mode: standalone)').matches || navigator.standalone) return 'pwa';
  } catch (_) { /* fall through */ }
  return 'tab';
}

// The ?v= stamp the server put on this module's URL: a stale cached copy
// carries an old one.
function assetHash() {
  try { return new URL(import.meta.url).searchParams.get('v'); } catch (_) { return null; }
}

/**
 * Start recording. `headers` is a function returning the auth headers, read
 * on every send. Returns `{ bootDone }`: call it once boot's fetches have
 * settled; only the first call records a load row.
 */
export function initPerfTiming({ headers } = {}) {
  const origin = window.location.origin;
  const envelope = { device_class: deviceClass(), os_class: osClass(), display: displayMode(), asset: assetHash() };
  let queue = [];
  let timer = null;
  let booted = false;
  // Boot's API entries, kept by the observer: the page's own resource buffer
  // (250 entries by default) can fill with static modules before boot ends.
  const bootEntries = [];

  function send(rows) {
    try {
      const conn = navigator.connection && navigator.connection.effectiveType;
      const body = JSON.stringify(Object.assign({}, envelope, { conn_type: conn || null, rows: rows }));
      fetch(ENDPOINT, {
        method: 'POST',
        headers: Object.assign({}, typeof headers === 'function' ? headers() : {}, { 'Content-Type': 'application/json' }),
        body: body,
        credentials: 'same-origin',
        keepalive: true,
      }).catch(noop);
    } catch (_) { /* never break the app */ }
  }

  function flush() {
    if (timer) { clearTimeout(timer); timer = null; }
    while (queue.length) send(queue.splice(0, MAX_BATCH));
  }

  function push(row) {
    queue.push(row);
    if (!timer) timer = setTimeout(flush, FLUSH_MS);
  }

  try {
    new PerformanceObserver(function (list) {
      for (const e of list.getEntries()) {
        if (!INITIATORS.has(e.initiatorType)) continue;
        if (!booted) bootEntries.push(e);
        if (e.duration < SLOW_API_MS) continue;
        const path = apiPath(e.name, origin);
        if (path) push(apiRow(e, path));
      }
    }).observe({ type: 'resource', buffered: true });
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'hidden') flush();
    });
  } catch (_) { /* no observer: no API rows */ }

  return {
    bootDone: function () {
      if (booted) return;
      booted = true;
      try {
        const nav = performance.getEntriesByType('navigation')[0];
        if (nav) push(loadRow(nav, performance.now(), bootEntries, origin));
        bootEntries.length = 0;
      } catch (_) { /* never break the app */ }
    },
  };
}
