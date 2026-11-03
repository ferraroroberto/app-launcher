// Unit pin for the shared Mermaid renderer's pure parts: which code blocks it
// takes, the theme it hands the library and the width floor. Runs under plain
// Node against the real ES modules (the module touches no DOM at import time).
// The drawing itself needs a browser: tests/e2e/settings/test_documents.py.
// Invoked by tests/unit/static_ui/test_mermaid_render.py.

import assert from 'node:assert/strict';
import { renderMarkdown } from '../../app/webapp/static/markdown.js';
import {
  diagramMinWidth, findMermaidBlocks, themeVariables,
} from '../../app/webapp/static/shared/mermaid-render.js';

// The contract the finder depends on: a ```mermaid fence is a code block whose
// header names the language and whose copy button holds the exact source.
{
  const html = renderMarkdown(['```mermaid', 'graph TD', '  A["x < y"] --> B', '```'].join('\n'));
  assert.match(html, /<span class="md-block-lang">mermaid<\/span>/, html);
  assert.match(html, /class="icon-button md-block-copy"[^>]*data-copy="graph TD&#10;  A\[&quot;x &lt; y&quot;\] --&gt; B"/, html);
}

// A fake DOM node: just the three calls the finder makes.
function block(lang, copy, extra) {
  return Object.assign({
    dataset: {},
    querySelector(sel) {
      if (sel === '.md-block-lang') return lang === null ? null : { textContent: lang };
      if (sel === '.md-block-copy') return copy === null ? null : { getAttribute: () => copy };
      return null;
    },
  }, extra);
}
const rootOf = (blocks) => ({ querySelectorAll: () => blocks });

// Only Mermaid fences, whatever their case or padding, with their source.
{
  const mermaid = block('mermaid', 'graph TD\n  A --> B');
  const padded = block(' Mermaid ', 'pie\n  "a": 1');
  const json = block('json', '{}');
  const bare = block('', 'plain');
  const found = findMermaidBlocks(rootOf([json, mermaid, bare, padded]));
  assert.deepEqual(found.map((f) => f.block), [mermaid, padded]);
  assert.equal(found[0].source, 'graph TD\n  A --> B');
}

// Already handled or failed blocks are not picked up again; a block missing
// its header or copy button is skipped, not thrown on.
{
  const done = block('mermaid', 'x', { dataset: { diagram: 'failed' } });
  assert.deepEqual(findMermaidBlocks(rootOf([done, block(null, 'x'), block('mermaid', null)])), []);
}

// Theme: tokens map onto Mermaid's base-theme variables; an unset token falls
// back to a literal colour so the library never gets an empty value.
{
  const tokens = {
    '--card': '#161b22', '--card-2': '#21262d', '--card-off': '#010409', '--fg': '#e6edf3',
    '--muted': '#7d8590', '--line': '#30363d', '--accent': '#2f81f7', '--font': 'system-ui',
  };
  const v = themeVariables((name) => tokens[name]);
  assert.equal(v.background, '#161b22');
  assert.equal(v.primaryColor, '#21262d');
  assert.equal(v.primaryTextColor, '#e6edf3');
  assert.equal(v.primaryBorderColor, '#2f81f7');
  assert.equal(v.lineColor, '#7d8590');
  assert.equal(v.clusterBorder, '#30363d');
  assert.equal(v.fontFamily, 'system-ui');

  const blank = themeVariables(() => '');
  for (const [key, value] of Object.entries(blank)) {
    assert.ok(String(value).trim(), `${key} must never be empty`);
  }
  assert.notEqual(blank.primaryColor, v.primaryColor, 'light and dark differ');
  assert.equal(themeVariables(() => '  #123456  ').background, '#123456');
}

// Width floor: a small diagram is not blown up, a large one is capped so it
// scrolls rather than shrinking to nothing, and nonsense gives no floor.
{
  assert.equal(diagramMinWidth(200), 160);
  assert.equal(diagramMinWidth(600), 480);
  assert.equal(diagramMinWidth(2400), 720);
  for (const bad of [0, -5, NaN, undefined, 'x']) assert.equal(diagramMinWidth(bad), 0);
}

console.log('OK');
