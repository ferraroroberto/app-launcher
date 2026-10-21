// Installs the browser globals the app's ES modules touch at import time, so a
// plain-Node test can import them. Import this first: a static import runs
// before the importing file's own `await import(...)` of the module under test.

const store = new Map();
globalThis.localStorage = {
  getItem: (k) => store.get(k) ?? null,
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};
const stub = () => new Proxy(function () {}, {
  get: (t, p) => (p === Symbol.toPrimitive ? () => '' : stub()),
  apply: () => stub(),
});
globalThis.document = stub();
globalThis.window = stub();
