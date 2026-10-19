// Unit pin for the Changed files panel's File view (#1689). Runs under plain
// Node against the real ES module (it imports only dom-utils.js and
// markdown.js, neither DOM-bound). Invoked by tests/test_file_view.py.

import assert from 'node:assert/strict';
import { fileKind, renderFileView, tintJson, windowsPath } from '../../app/webapp/static/file-view.js';

const SCRIPT = '<script>alert(1)</script>';
// Escaped text may still read "onerror=": only a live tag is a fault.
const noRawTags = (html) => !/<script|<img|<b>/i.test(html);

// Kinds from the name alone.
{
  assert.equal(fileKind('README.md'), 'markdown');
  assert.equal(fileKind('docs/Notes.MARKDOWN'), 'markdown');
  assert.equal(fileKind('config/apps.json'), 'json');
  assert.equal(fileKind('E:\\work\\proj\\src\\main.py'), 'code');
  assert.equal(fileKind('static/app.js'), 'code');
  assert.equal(fileKind('scripts/run.ps1'), 'code');
  assert.equal(fileKind('Dockerfile'), 'code');
  assert.equal(fileKind('notes.txt'), 'text');
  assert.equal(fileKind('LICENSE'), 'text');
  assert.equal(fileKind('.gitignore'), 'text');
}

// Markdown renders through the sanitising renderer.
{
  const html = renderFileView('a.md', '# Title\n\nSome **bold** text.\n');
  assert.ok(html.startsWith('<div class="fv-md tr-md">'), html);
  assert.match(html, /<h1>Title<\/h1>/);
  assert.match(html, /<strong>bold<\/strong>/);
}

// Code: one line span per line (the CSS counter's gutter), the final newline
// adds no empty line, CRLF splits the same as LF, nothing tinted.
{
  const html = renderFileView('m.py', 'def f():\r\n    return 1\r\n');
  assert.ok(html.startsWith('<pre class="fv-listing fv-code"><code>'), html);
  assert.equal((html.match(/class="fv-line"/g) || []).length, 2, html);
  assert.ok(html.includes('<span class="fv-line">    return 1</span>'), html);
  assert.ok(!/fv-(key|str|num|lit)/.test(html), 'code must not be tinted: ' + html);
  // An empty line keeps its row, so the numbers stay true.
  assert.equal((renderFileView('m.py', 'a\n\nb').match(/class="fv-line"/g) || []).length, 3);
}

// JSON: pretty-printed, keys / strings / numbers / literals tinted.
{
  const html = renderFileView('c.json', '{"name":"x","n":3,"ok":true,"nil":null,"list":[1.5,-2e3]}');
  assert.ok(html.startsWith('<pre class="fv-listing fv-code fv-json"><code>'), html);
  assert.ok(html.includes('<span class="fv-key">&quot;name&quot;</span>:'), html);
  assert.ok(html.includes('<span class="fv-str">&quot;x&quot;</span>'), html);
  assert.ok(html.includes('<span class="fv-num">3</span>'), html);
  assert.ok(html.includes('<span class="fv-lit">true</span>'), html);
  assert.ok(html.includes('<span class="fv-lit">null</span>'), html);
  assert.ok(html.includes('<span class="fv-num">-2000</span>'), html);
  assert.ok((html.match(/class="fv-line"/g) || []).length > 5, 'pretty-printed over lines: ' + html);
  // A string holding an escaped quote is one token.
  assert.equal(tintJson('"a\\"b"'), '<span class="fv-str">&quot;a\\&quot;b&quot;</span>');
}

// JSON that doesn't parse is the plain code listing, untouched.
{
  const html = renderFileView('c.json', '{ // comment\n  "a": 1,\n}');
  assert.ok(html.startsWith('<pre class="fv-listing fv-code"><code>'), html);
  assert.ok(!html.includes('fv-json') && html.includes('// comment'), html);
}

// Plain text is one escaped block.
{
  assert.equal(renderFileView('n.txt', 'a < b & c'), '<pre class="fv-text">a &lt; b &amp; c</pre>');
}

// An inert <script> (and other markup) in every kind: escaped, never live.
{
  const evil = SCRIPT + '\n<img src=x onerror=alert(1)>\n<b>raw</b>\n';
  for (const name of ['x.md', 'x.py', 'x.txt', 'x.json']) {
    const html = renderFileView(name, evil);
    assert.ok(noRawTags(html), name + ': ' + html);
    assert.ok(html.includes('&lt;script&gt;'), name + ': ' + html);
  }
  const json = renderFileView('x.json', JSON.stringify({ k: SCRIPT, [SCRIPT]: 1 }));
  assert.ok(noRawTags(json), json);
  assert.ok(json.includes('<span class="fv-key">&quot;&lt;script&gt;'), json);
}

// The Windows path Copy path hands out.
{
  assert.equal(windowsPath('E:/work/proj/src/a.py'), 'E:\\work\\proj\\src\\a.py');
  assert.equal(windowsPath('E:\\work\\proj\\a.py', 'C:/other'), 'E:\\work\\proj\\a.py');
  assert.equal(windowsPath('src/a.py', 'E:/work/proj/'), 'E:\\work\\proj\\src\\a.py');
  assert.equal(windowsPath('./src/../b.md', 'E:\\work\\proj'), 'E:\\work\\proj\\b.md');
  assert.equal(windowsPath('..\\..\\..\\x', 'E:\\a'), 'E:\\x');
  assert.equal(windowsPath('//server/share/a.txt'), '\\\\server\\share\\a.txt');
  assert.equal(windowsPath('src/a.py', ''), null);
  assert.equal(windowsPath('', 'E:\\a'), null);
}

console.log('OK');
