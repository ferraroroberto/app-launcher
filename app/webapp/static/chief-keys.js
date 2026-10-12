/* Which of the chief's questions are answered (#1487, one module since
 * #1502).
 *
 * A question's key is the chief's own id, else its position in the plan's
 * list. The answer sheet records answered keys with it (board-answers.js),
 * the Board's lane, header and tab badge count the questions still open with
 * it, and the webapp counts them for the badge on every other tab with its
 * Python twin (src/chief_answers.py item_key). One fixture pins both sides:
 * tests/fixtures/chief_question_keys.json.
 *
 * Imports nothing, so plain Node can load it for that test.
 */

export function itemKey(item, index) {
  return item.id || String(index);
}

// How many of `waiting` no device has answered: `answered` is the list of
// answered keys.
export function unansweredCount(waiting, answered) {
  return (waiting || []).filter(function (item, i) {
    return answered.indexOf(itemKey(item, i)) === -1;
  }).length;
}

// What this page sent, on which version of the plan: its own marks show at
// once and survive a record that failed (board-answers.js recordAnswered).
let sent = { updated_at: null, ids: [] };

function union(a, b) {
  return a.concat(b.filter(function (k) { return a.indexOf(k) === -1; }));
}

// The keys answered on this version of the plan: from any device, as the
// webapp recorded them, plus what this page sent.
export function answeredKeys(plan) {
  if (!plan) return [];
  const recorded = Array.isArray(plan.answered) ? plan.answered.map(String) : [];
  return sent.updated_at === (plan.updated_at || '') ? union(recorded, sent.ids) : recorded;
}

export function markSent(plan, keys) {
  const version = plan.updated_at || '';
  sent = { updated_at: version, ids: union(sent.updated_at === version ? sent.ids : [], keys) };
}

// The plan's open questions, fleet-wide (#1502): a number for a plan the
// Board read (0 when it has nothing queued), null when it could not read it
// (unreadable, or the webapp did not answer) — unknown, never zero.
export function openQuestionCount(plan) {
  if (!plan) return null;
  if (plan.state === 'empty') return 0;
  if (plan.state !== 'ok') return null;
  return unansweredCount(plan.waiting_on_roberto, answeredKeys(plan));
}
