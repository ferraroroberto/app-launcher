// Unit pin for the one model-list builder both model pickers use (the chat
// "Switch model" sheet and the Life OS resume Model row): same models, order
// and labels for the same agent. Runs under plain Node against the real ES
// module; invoked by tests/unit/core/test_model_choices.py.

import '../_browser_stubs.mjs';
import assert from 'node:assert/strict';

const { modelChoices, modelValueForLabel } =
  await import('../../../app/webapp/static/shared/model-choices.js');

const config = {
  model_catalog: {
    claude: [
      { value: 'sonnet', label: 'Sonnet', available: true },
      { value: 'opus', label: 'Opus', available: true },
      { value: 'fable', label: 'Fable', available: true },
    ],
    codex: [
      { value: 'gpt-6-luna', label: 'Luna', available: true },
      { value: 'gpt-5.6-terra', label: 'Terra', available: true },
      { value: 'gpt-6-astra', label: 'Astra', available: false },
    ],
  },
  hidden_models: [],
};
const labels = (list) => list.map((c) => c.label);

// Claude: the legacy Haiku leads, then the catalog in its own order.
assert.deepEqual(labels(modelChoices('claude', config, '')), ['Haiku', 'Sonnet', 'Opus', 'Fable']);
// Values are provider-qualified; `id` is the bare id a session switch posts.
assert.deepEqual(modelChoices('claude', config, '')[0], { value: 'claude:haiku', label: 'Haiku', id: 'haiku' });
// Codex has no Haiku, and an unavailable model is never offered.
assert.deepEqual(labels(modelChoices('codex', config, '')), ['Luna', 'Terra']);

// A hidden model is left out, except the one the conversation or session runs.
const hidden = { ...config, hidden_models: ['claude:opus', 'claude:haiku'] };
assert.deepEqual(labels(modelChoices('claude', hidden, '')), ['Sonnet', 'Fable']);
assert.deepEqual(labels(modelChoices('claude', hidden, 'claude:opus')), ['Sonnet', 'Opus', 'Fable']);
assert.deepEqual(labels(modelChoices('claude', hidden, 'claude:haiku')), ['Haiku', 'Sonnet', 'Fable']);

// An unavailable model stays out even when it is the own one.
assert.deepEqual(labels(modelChoices('codex', config, 'codex:gpt-6-astra')), ['Luna', 'Terra']);

// No config, no catalog: only what the builder itself knows.
assert.deepEqual(labels(modelChoices('claude', null, '')), ['Haiku']);
assert.deepEqual(modelChoices('codex', undefined, ''), []);

// A session reports its model by label; the value is looked up, hidden or not.
assert.equal(modelValueForLabel('claude', hidden, 'Opus'), 'claude:opus');
assert.equal(modelValueForLabel('claude', hidden, 'Haiku'), 'claude:haiku');
assert.equal(modelValueForLabel('codex', config, 'Terra'), 'codex:gpt-5.6-terra');
assert.equal(modelValueForLabel('claude', config, 'Nope'), '');
assert.equal(modelValueForLabel('claude', config, ''), '');

console.log('OK');
