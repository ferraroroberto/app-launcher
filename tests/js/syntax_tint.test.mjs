// Plain-Node pin for the File view's token colours (#1749): run by
// tests/test_syntax_tint.py. Imports the real ES module — no browser.
import assert from 'node:assert/strict';
import { performance } from 'node:perf_hooks';

import {
  TINT_MAX_CHARS, TINT_MAX_LINES, tintDiffLines, tintLanguage, tintLines, tintable,
} from '../../app/webapp/static/changes/syntax-tint.js';
import { renderFileView } from '../../app/webapp/static/changes/file-view.js';

const unescape = (s) => s
  .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"')
  .replace(/&#39;/g, "'").replace(/&amp;/g, '&');
const strip = (html) => unescape(html.replace(/<[^>]*>/g, ''));
const span = (cls, text) => '<span class="' + cls + '">' + text + '</span>';

// One fixture per language family: [file name, source, [class, token] pairs
// that must be tinted that way].
const FIXTURES = [
  ['m.py', '# note\n@cache\ndef f(x):\n    """doc\n    more"""\n    return "s" + str(1.5) if x is None else True\n',
    [['fv-cmt', '# note'], ['fv-kw', 'def'], ['fv-name', 'f'], ['fv-name', '@cache'],
      ['fv-str', '&quot;s&quot;'], ['fv-num', '1.5'], ['fv-num', 'None'], ['fv-kw', 'return']]],
  ['r.ps1', '# c\n<# block\n comment #>\nfunction Get-Thing {\n  param($Path)\n  $x = "a $Path"\n  if ($x -eq $true) { Write-Host \'ok\' 42 }\n}\n$h = @"\nhere\n"@\n',
    [['fv-cmt', '# c'], ['fv-kw', 'function'], ['fv-name', 'Get-Thing'], ['fv-str', '&quot;a $Path&quot;'],
      ['fv-num', '$true'], ['fv-name', 'Write-Host'], ['fv-num', '42'], ['fv-kw', 'if']]],
  ['a.js', '// hi\n/* a\n b */\nconst s = `t\nu`;\nfunction go(a) { return a ? "x" : null; }\n',
    [['fv-cmt', '// hi'], ['fv-kw', 'const'], ['fv-name', 'go'], ['fv-str', '&quot;x&quot;'], ['fv-num', 'null']]],
  ['a.ts', 'export interface A { n: number }\nlet v: A = { n: 0x1F };\n',
    [['fv-kw', 'export'], ['fv-kw', 'interface'], ['fv-num', '0x1F']]],
  ['p.html', '<!-- c -->\n<div class="a" id=\'b\'>text "q" 1</div>\n<input disabled\n  name="n">\n',
    [['fv-cmt', '&lt;!-- c --&gt;'], ['fv-kw', '&lt;div'], ['fv-name', 'class'], ['fv-str', '&quot;a&quot;'],
      ['fv-kw', '&lt;/div'], ['fv-name', 'name']]],
  ['s.css', '/* c */\n@media (max-width: 600px) {\n  .a {\n    color: #0550ae;\n    margin: 0 8px !important;\n  }\n}\n',
    [['fv-cmt', '/* c */'], ['fv-kw', '@media'], ['fv-name', '    color'], ['fv-num', '#0550ae'], ['fv-num', '8px'], ['fv-kw', '!important']]],
  ['c.toml', '# c\n[tool.x]\nname = "n"\nn = 3\nok = true\n"""\nx\n"""\n',
    [['fv-cmt', '# c'], ['fv-kw', '[tool.x]'], ['fv-name', 'name'], ['fv-str', '&quot;n&quot;'], ['fv-num', '3'], ['fv-num', 'true']]],
  ['c.yaml', '# c\nkey: value\nlist:\n  - name: "x"  # tail\n    on: true\n---\n',
    [['fv-cmt', '# c'], ['fv-name', 'key'], ['fv-str', '&quot;x&quot;'], ['fv-cmt', ' # tail'], ['fv-num', 'true'], ['fv-kw', '---']]],
  ['r.sh', '#!/bin/sh\n# c\nif [ -f "$1" ]; then\n  echo \'a\' --flag $HOME 3\nfi\n',
    [['fv-cmt', '#!/bin/sh'], ['fv-kw', 'if'], ['fv-str', '&quot;$1&quot;'], ['fv-str', '&#39;a&#39;'], ['fv-name', ' --flag'], ['fv-kw', 'fi']]],
  ['q.sql', '-- c\nSELECT id, \'a\' FROM t WHERE n = 1 AND ok IS NULL;\nselect 2;\n',
    [['fv-cmt', '-- c'], ['fv-kw', 'SELECT'], ['fv-str', '&#39;a&#39;'], ['fv-kw', 'FROM'], ['fv-num', 'NULL'], ['fv-kw', 'select']]],
  ['m.go', '// c\npackage main\nfunc main() {\n\tx := "s"\n\treturn nil\n}\n',
    [['fv-cmt', '// c'], ['fv-kw', 'package'], ['fv-kw', 'func'], ['fv-name', 'main'], ['fv-str', '&quot;s&quot;'], ['fv-num', 'nil']]],
  ['m.c', '#include <stdio.h>\nint main(void) { return 0; /* done */ }\n',
    [['fv-kw', '#include'], ['fv-kw', 'int'], ['fv-name', 'main'], ['fv-cmt', '/* done */']]],
];

for (const [name, src, expected] of FIXTURES) {
  const lang = tintLanguage(name.split('.').pop());
  assert.ok(lang, name + ' has a language');
  const out = tintLines(src, lang);
  const joined = out.join('\n');
  for (const [cls, tok] of expected) {
    assert.ok(joined.includes(span(cls, tok)), name + ': ' + cls + ' ' + JSON.stringify(tok) + ' in\n' + joined);
  }
  // Round trip: strip the tags and unescape each line and the source comes
  // back exactly; the line count is the source's.
  const srcLines = src.split(/\r\n|\r|\n/);
  assert.equal(out.length, srcLines.length, name + ': line count');
  out.forEach((l, i) => assert.equal(strip(l), srcLines[i], name + ' line ' + (i + 1)));
  // Balanced per line: every opened span closes inside its own line.
  out.forEach((l, i) => assert.equal((l.match(/<span /g) || []).length, (l.match(/<\/span>/g) || []).length,
    name + ' line ' + (i + 1) + ' unbalanced: ' + l));
}

// A token that spans lines is closed and reopened at each break.
{
  const out = tintLines('x = 1\n"""a\nb\nc"""\ny = 2\n', 'python');
  assert.equal(out[1], span('fv-str', '&quot;&quot;&quot;a'));
  assert.equal(out[2], span('fv-str', 'b'));
  assert.equal(out[3], span('fv-str', 'c&quot;&quot;&quot;'));
  const cmt = tintLines('/* a\nb */ x', 'c');
  assert.equal(cmt[0], span('fv-cmt', '/* a'));
  assert.ok(cmt[1].startsWith(span('fv-cmt', 'b */')), cmt[1]);
}

// CRLF and CR split like LF, and a final newline leaves the empty last line
// for file-view.js to trim.
{
  const crlf = tintLines('a = 1\r\n# c\r\nb = 2\r\n', 'python');
  assert.equal(crlf.length, 4);
  assert.equal(crlf[1], span('fv-cmt', '# c'));
  assert.equal(crlf[3], '');
  assert.equal(tintLines('a\rb', 'python').length, 2);
}

// Nothing in a file is ever markup: a <script> in a string, a comment, a tag
// body or an attribute stays text on every line, in every family.
{
  const bad = '<script>alert(1)</script><img src=x onerror=alert(1)>';
  const cases = [
    ['python', 's = "' + bad + '"\n# ' + bad + '\n'],
    ['js', 's = "' + bad + '"; // ' + bad + '\n'],
    ['html', bad + '\n<a href="' + bad + '">' + bad + '</a>\n<!-- ' + bad + ' -->\n'],
    ['css', 'a { content: "' + bad + '" } /* ' + bad + ' */\n'],
    ['shell', 'echo "' + bad + '" # ' + bad + '\n'],
    ['powershell', '"' + bad + '" # ' + bad + '\n'],
    ['toml', 'k = "' + bad + '" # ' + bad + '\n'],
    ['yaml', 'k: "' + bad + '" # ' + bad + '\n'],
    ['sql', "SELECT '" + bad + "' -- " + bad + '\n'],
    ['c', 'x = "' + bad + '"; // ' + bad + '\n'],
  ];
  for (const [lang, src] of cases) {
    const html = tintLines(src, lang).join('\n');
    assert.ok(!/<script|<img|<a /i.test(html.replace(/<span class="fv-[a-z]+">|<\/span>/g, '')), lang + ' let markup through: ' + html);
    assert.equal(strip(html), src.replace(/\n$/, '\n'), lang + ' round trip');
  }
  // Through the File view too.
  const page = renderFileView('x.html', '<script>window.__ran = true</script>\n');
  assert.ok(!page.includes('<script'), page);
  assert.ok(page.includes('&lt;script'), page);
}

// Languages and the size cut-off.
assert.equal(tintLanguage('py'), 'python');
assert.equal(tintLanguage('ps1'), 'powershell');
assert.equal(tintLanguage('yml'), 'yaml');
assert.equal(tintLanguage('zip'), null);
assert.equal(tintLanguage('toString'), null, 'object prototype keys are not languages');
assert.ok(tintable('a\n'.repeat(TINT_MAX_LINES)));
assert.ok(!tintable('a\n'.repeat(TINT_MAX_LINES + 1)));
assert.ok(tintable('x'.repeat(TINT_MAX_CHARS)));
assert.ok(!tintable('x'.repeat(TINT_MAX_CHARS + 1)));

// File view: a known language is tinted with its gutter lines kept; an unknown
// code extension stays the plain listing; a large file says why it has no colours.
{
  const html = renderFileView('m.py', 'def f():\n    return 1\n');
  assert.ok(html.startsWith('<pre class="fv-listing fv-code fv-tint"><code>'), html);
  assert.equal((html.match(/class="fv-line"/g) || []).length, 2, html);
  assert.ok(html.includes(span('fv-kw', 'def')), html);
  assert.ok(!html.includes('fv-note'), html);
  const plain = renderFileView('Makefile', 'all:\n\techo hi\n');
  assert.ok(plain.startsWith('<pre class="fv-listing fv-code"><code>'), plain);
  assert.ok(!/fv-(kw|str|num|cmt|name)/.test(plain), plain);
  const big = renderFileView('m.py', 'x = 1\n'.repeat(TINT_MAX_LINES + 5));
  assert.ok(big.startsWith('<div class="fv-note muted small">Colours are off for large files</div><pre class="fv-listing fv-code">'), big.slice(0, 200));
  assert.ok(!/fv-(kw|str|num|cmt|name)"/.test(big));
  assert.equal((big.match(/class="fv-line"/g) || []).length, TINT_MAX_LINES + 5);
  // An empty line keeps its row and the final newline adds none.
  assert.equal((renderFileView('m.py', 'a = 1\n\nb = 2\n').match(/class="fv-line"/g) || []).length, 3);
}

// A diff hunk is tokenised as two sides: each changed line reads its own side,
// and a token opened and closed inside the hunk colours across its lines.
{
  const hunk = [' def f():', '-    """old', '-    doc"""', '+    """new', '+    more"""',
    '+    return "s<b>"  # c', '\\ No newline at end of file'];
  const out = tintDiffLines(hunk, 'python');
  assert.equal(out[0], span('fv-kw', 'def') + ' ' + span('fv-name', 'f') + '():');
  assert.equal(out[2], span('fv-str', '    doc&quot;&quot;&quot;'));
  assert.equal(out[4], span('fv-str', '    more&quot;&quot;&quot;'));
  assert.ok(out[5].includes(span('fv-str', '&quot;s&lt;b&gt;&quot;')) && out[5].includes(span('fv-cmt', '# c')), out[5]);
  assert.equal(out[6], null, 'the no-newline marker is left plain');
  hunk.slice(0, 6).forEach((l, i) => assert.equal(strip(out[i]), l.slice(1), 'line ' + i));
  // A removed block comment does not swallow the added line beside it.
  assert.equal(tintDiffLines(['-/* a', '-b */', '+x = 1;'], 'c')[2], 'x = ' + span('fv-num', '1') + ';');
}

// Plain (null): no language, an empty line, a CR inside a line, a hunk over the
// cut-off. A trailing CR (a CRLF file) is kept, and a one-sided hunk colours.
{
  assert.deepEqual(tintDiffLines(['+a = 1', ' b'], null), [null, null]);
  assert.deepEqual(tintDiffLines(['+a = 1', ''], 'python').map((l) => l === null), [false, true]);
  assert.deepEqual(tintDiffLines(['+a = 1', '+b\rc = 2'], 'python'), [null, null]);
  assert.equal(tintDiffLines(['+a = 1\r'], 'python')[0], 'a = ' + span('fv-num', '1') + '\r');
  assert.equal(tintDiffLines(['-a = 1', '-b = 2'], 'python')[1], 'b = ' + span('fv-num', '2'));
  const lines = (n) => Array.from({ length: n }, (_, i) => (i % 2 ? '-' : '+') + 'x = "q"  # 42');
  assert.ok(tintDiffLines(lines(TINT_MAX_LINES + 2), 'python').every((l) => l === null), 'line cap');
  assert.ok(tintDiffLines(['+' + 'x'.repeat(TINT_MAX_CHARS)], 'python').every((l) => l === null), 'size cap');
  const t0 = performance.now();
  assert.ok(tintDiffLines(lines(TINT_MAX_LINES), 'python').every((l) => l !== null));
  assert.ok(performance.now() - t0 < 250, 'a 2,000-line hunk took ' + (performance.now() - t0) + 'ms');
}

// A pathological input stays linear: an unterminated triple quote, a long
// line of quotes and backslashes, a huge unclosed comment.
{
  const nasty = ['"""' + 'a\n'.repeat(5000), '"\\'.repeat(20000), '/*' + 'x'.repeat(90000)];
  nasty.forEach((src, i) => {
    const t0 = performance.now();
    tintLines(src, i === 2 ? 'js' : 'python');
    assert.ok(performance.now() - t0 < 500, 'nasty input ' + i + ' took ' + (performance.now() - t0) + 'ms');
  });
}

// Timing at the cut-off, recorded for the PR (a 100 KB / 2,000-line file).
{
  const line = 'def handler(request, *args, **kwargs):  # a comment, "quoted", 42\n';
  const src = line.repeat(Math.floor(TINT_MAX_CHARS / line.length)).slice(0, TINT_MAX_CHARS);
  tintLines(src, 'python');   // warm
  const t0 = performance.now();
  tintLines(src, 'python');
  const ms = performance.now() - t0;
  console.log('tint 100 KB python: ' + ms.toFixed(1) + ' ms');
  assert.ok(ms < 250, 'tinting a 100 KB file took ' + ms + 'ms');
}

console.log('OK');
