// Pin for the Life OS title-sync module (static/life-os/title-sync.js): only
// a `failed` verdict reads as failed, so the phone never toasts a stale name
// for a synced, missing or unknown verdict. Prints the declared values for the
// Python side to compare with TitleSync.
// Invoked by tests/unit/life_os/test_title_sync.py.

import assert from 'node:assert/strict';

const { TitleSync, titleSync, syncFailed } = await import('../../app/webapp/static/life-os/title-sync.js');

assert.ok(Object.isFrozen(TitleSync));

assert.equal(syncFailed({ status: TitleSync.FAILED, error: 'kept its old name' }), true);
for (const notFailed of [{ status: TitleSync.SYNCED }, { status: 'none' }, { status: 'Failed' },
  {}, null, undefined]) {
  assert.equal(syncFailed(notFailed), false, JSON.stringify(notFailed));
}
assert.equal(titleSync({ status: 'synced' }), TitleSync.SYNCED);
assert.equal(titleSync({ status: 'exploded' }), null);

console.log('VALUES ' + JSON.stringify(Object.values(TitleSync).sort()));
console.log('OK');
