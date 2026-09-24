// Unit pin for the shared markdown renderer's GFM table block (#1141). Runs
// under plain Node — no browser — against the real ES module both the Chat
// pane and Life OS import. Invoked by tests/test_markdown_tables.py.

import assert from 'node:assert/strict';
import { renderMarkdown } from '../../app/webapp/static/markdown.js';

const md = (lines) => renderMarkdown(lines.join('\n'));

// A standard table: wrapper, header, body.
{
  const html = md(['| Name | Count |', '|---|---|', '| alpha | 3 |', '| beta | 12 |']);
  assert.ok(html.startsWith('<div class="md-table-wrap"><table class="md-table">'), html);
  assert.match(html, /<thead><tr><th>Name<\/th><th>Count<\/th><\/tr><\/thead>/);
  assert.match(html, /<tbody><tr><td>alpha<\/td><td>3<\/td><\/tr><tr><td>beta<\/td><td>12<\/td><\/tr><\/tbody>/);
  assert.ok(!html.includes('<p>'), 'a table must not also leave a paragraph behind');
}

// Per-column alignment from the delimiter colons, on header and body cells.
{
  const html = md(['| l | c | r | n |', '|:--|:-:|--:|---|', '| 1 | 2 | 3 | 4 |']);
  assert.match(html, /<th class="md-al-left">l<\/th><th class="md-al-center">c<\/th><th class="md-al-right">r<\/th><th>n<\/th>/);
  assert.match(html, /<td class="md-al-left">1<\/td><td class="md-al-center">2<\/td><td class="md-al-right">3<\/td><td>4<\/td>/);
}

// Inline markdown still renders inside cells; HTML in a cell stays escaped.
{
  const html = md([
    '| a | b |', '|---|---|',
    '| `x()` **bold** | [docs](https://example.com/d) |',
    '| <script>alert(1)</script> | <b>raw</b> |',
  ]);
  assert.match(html, /<td><code>x\(\)<\/code> <strong>bold<\/strong><\/td>/);
  assert.match(html, /<td><a href="https:\/\/example.com\/d" target="_blank" rel="noopener">docs<\/a><\/td>/);
  assert.ok(html.includes('&lt;script&gt;alert(1)&lt;/script&gt;'), html);
  assert.ok(html.includes('&lt;b&gt;raw&lt;/b&gt;'), html);
  assert.ok(!html.includes('<script>') && !html.includes('<b>'), html);
}

// A pipe inside a code span, or escaped as \|, does not split the cell.
{
  const html = md(['| expr | note |', '|---|---|', '| `a|b` | x \\| y |']);
  assert.match(html, /<td><code>a\|b<\/code><\/td><td>x \| y<\/td>/);
}

// Leading/trailing pipes are optional.
{
  const html = md(['a | b', '--- | ---', '1 | 2']);
  assert.match(html, /<th>a<\/th><th>b<\/th>/);
  assert.match(html, /<td>1<\/td><td>2<\/td>/);
}

// Short rows pad with empty cells; extra cells are dropped.
{
  const html = md(['| a | b | c |', '|---|---|---|', '| 1 |', '| 1 | 2 | 3 | 4 |']);
  assert.match(html, /<tr><td>1<\/td><td><\/td><td><\/td><\/tr>/);
  assert.match(html, /<tr><td>1<\/td><td>2<\/td><td>3<\/td><\/tr>/);
  assert.ok(!html.includes('<td>4</td>'), html);
}

// A header-only table is still a table, with no empty <tbody>.
{
  const html = md(['| a | b |', '|---|---|']);
  assert.ok(html.includes('<thead>') && !html.includes('<tbody>'), html);
}

// The table ends at a blank line; a lead-in line before it is its own
// paragraph, and text after it is a paragraph again.
{
  const html = md(['Summary:', '| a | b |', '|---|---|', '| 1 | 2 |', '', 'After the table.']);
  assert.ok(html.startsWith('<p>Summary:</p>\n<div class="md-table-wrap">'), html);
  assert.ok(html.endsWith('</table></div>\n<p>After the table.</p>'), html);
}

// ...and at the first pipe-less line.
{
  const html = md(['| a | b |', '|---|---|', '| 1 | 2 |', 'plain text']);
  assert.ok(html.endsWith('</table></div>\n<p>plain text</p>'), html);
}

// Non-table pipe text is unchanged.
{
  assert.equal(md(['a | b']), '<p>a | b</p>');
  assert.equal(md(['use `a | b` here']), '<p>use <code>a | b</code> here</p>');
  // A header whose "delimiter" row has the wrong cell count is not a table.
  assert.equal(md(['| a | b |', '|---|']), '<p>| a | b | |---|</p>');
  // A second line of dashes-and-words is not a delimiter.
  assert.equal(md(['a | b', 'c | d']), '<p>a | b c | d</p>');
  // Pipes inside a fence stay literal code.
  const fenced = md(['```', '| a | b |', '|---|---|', '| 1 | 2 |', '```']);
  assert.ok(!fenced.includes('<table'), fenced);
  assert.ok(fenced.includes('| a | b |\n|---|---|\n| 1 | 2 |'), fenced);
}

// Numbered lists (#1202): an <ol>, not one run-on paragraph -- a numbered plan
// in the Chat plan card (#1151) rendered as `<p>1. a 2. b</p>`.
{
  assert.equal(md(['1. Add the route', '2. Wire the button']),
    '<ol>\n<li>Add the route</li>\n<li>Wire the button</li>\n</ol>');
  // A list that doesn't start at 1 keeps its number; `1)` works like `1.`.
  assert.ok(md(['3. c', '4. d']).startsWith('<ol start="3">'), md(['3. c', '4. d']));
  assert.ok(md(['1) a', '2) b']).startsWith('<ol>\n<li>a</li>'), md(['1) a', '2) b']));
  // Inline markdown renders and HTML stays escaped inside an item.
  assert.equal(md(['1. `x()` <b>raw</b>']),
    '<ol>\n<li><code>x()</code> &lt;b&gt;raw&lt;/b&gt;</li>\n</ol>');
  // Switching kinds closes one list and opens the other.
  assert.equal(md(['- a', '1. b']), '<ul>\n<li>a</li>\n</ul>\n<ol>\n<li>b</li>\n</ol>');
  // A lead-in line is its own paragraph.
  assert.equal(md(['Plan:', '1. a']), '<p>Plan:</p>\n<ol>\n<li>a</li>\n</ol>');
}

console.log('markdown tables + lists: OK');
