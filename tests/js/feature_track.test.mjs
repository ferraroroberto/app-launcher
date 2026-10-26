// Unit pin for the feature-use recorder. Runs under plain Node with a stub DOM:
// the real click-to-event path is proven in a browser by
// tests/e2e/settings/test_screen_time_features.py. Invoked by
// tests/unit/usage/test_feature_counts.py.

import assert from 'node:assert/strict';

const seen = [];
const probes = [];
globalThis.document = {
  createElement() {
    const attrs = new Map();
    const el = {
      hidden: false,
      setAttribute: (k, v) => attrs.set(k, v),
      removeAttribute: (k) => attrs.delete(k),
      // What the usage log's delegated listener reads when the click lands.
      click: () => seen.push(attrs.get('data-track') ?? null),
      attrs,
    };
    probes.push(el);
    return el;
  },
  body: { appendChild() {} },
};

const { FEATURE_IDS, trackFeature } = await import(
  '../../app/webapp/static/settings/feature-track.js'
);

assert.equal(FEATURE_IDS.length, 8);
assert.ok(FEATURE_IDS.every((id) => /^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/.test(id)));

// A listed id is on the probe at click time, then gone again.
trackFeature('feat.ocr');
assert.deepEqual(seen, ['feat.ocr']);
assert.equal(probes.length, 1);
assert.equal(probes[0].attrs.has('data-track'), false, 'the id does not outlive the click');
assert.equal(probes[0].hidden, true);

// One probe serves every call.
FEATURE_IDS.forEach(trackFeature);
assert.equal(probes.length, 1);
assert.deepEqual(seen.slice(1), FEATURE_IDS);

// Anything off the list is dropped without a click: a file name, a sentence, a
// non-string, a near miss.
const before = seen.length;
['photo.png', 'feat.ocr extra', 'mic.start', 'feat.', '', null, undefined, 42, {}].forEach(trackFeature);
assert.equal(seen.length, before, 'an id off the list reached the log');

// A page whose DOM throws must not take the app down.
globalThis.document = { createElement() { throw new Error('no DOM'); }, body: null };
const { trackFeature: fresh } = await import(
  '../../app/webapp/static/settings/feature-track.js?again'
);
assert.doesNotThrow(() => fresh('feat.ocr'));

console.log('OK feature-track');
