// Unit pin for the machine-health row's reading: the verdict word, the worse of
// the ports and commit percents, and the tone. Runs under plain Node against
// the real ES module. Invoked by tests/unit/usage/test_machine_health.py.

import assert from 'node:assert/strict';
import { machineReading, worstMetric } from '../../../app/webapp/static/machine-health.js';

const machine = (ports, commit) => ({ ports_percent: ports, commit_percent: commit });

// Before the first answer, and when the launcher could not be asked.
assert.deepEqual(machineReading(undefined), { tone: 'none', value: 'Reading…' });
assert.deepEqual(machineReading(null), { tone: 'none', value: 'Not confirmed' });

// An unreadable verdict is the word, never a percent, whatever else came with it.
let r = machineReading({ verdict: 'unknown', why: 'unreachable', machine: machine(3, 40) });
assert.equal(r.value, 'Not confirmed');
assert.equal(r.tone, 'none');
assert.equal(machineReading({ verdict: 'fine' }).value, 'Not confirmed');

// ok, watch and act: the tone follows the verdict, the value names the worse metric.
r = machineReading({ verdict: 'ok', machine: machine(3.2, 68.5) });
assert.deepEqual(r, { tone: 'ok', value: 'OK · Commit 69%' });
r = machineReading({ verdict: 'watch', machine: machine(55, 40) });
assert.deepEqual(r, { tone: 'attention', value: 'Watch · Ports 55%' });
r = machineReading({ verdict: 'act', machine: machine(82, 91) });
assert.deepEqual(r, { tone: 'danger', value: 'Needs action · Commit 91%' });

// A tie reads Ports; one unmeasured metric is skipped, not read as 0%.
assert.equal(worstMetric(machine(50, 50)).name, 'Ports');
assert.deepEqual(worstMetric(machine(null, 12)), { name: 'Commit', pct: 12 });
assert.equal(worstMetric(machine(null, null)), null);
assert.equal(worstMetric(undefined), null);

// Nothing measured: the verdict word alone, not a made-up percent.
r = machineReading({ verdict: 'act', machine: machine(null, null) });
assert.equal(r.value, 'Needs action');
r = machineReading({ verdict: 'ok' });
assert.equal(r.value, 'OK');

console.log('machine_health ok');
