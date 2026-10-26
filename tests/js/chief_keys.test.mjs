// The JS half of the shared answered-key pin (#1502): chief-keys.js must
// key and count every case of the shared fixture exactly as
// src/chief/answers.py does (tests/unit/chief/test_chief_question_keys.py runs both).
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';
import { itemKey, unansweredCount } from '../../app/webapp/static/board/chief-keys.js';

const fixture = JSON.parse(
  readFileSync(new URL('../fixtures/chief_question_keys.json', import.meta.url), 'utf8')
);

for (const c of fixture.cases) {
  assert.deepEqual(c.waiting.map(itemKey), c.keys, c.name + ': keys');
  assert.equal(unansweredCount(c.waiting, c.answered), c.unanswered, c.name + ': unanswered');
}
console.log('OK ' + fixture.cases.length + ' cases');
