/* Token colours for the Changed files panel's File view (#1749).
 *
 * A compact regex tokenizer, not a grammar: one alternation per language
 * family, five classes (keyword, string, number/literal, comment, name) that
 * map onto the fleet's existing text tokens in styles.css. It is less exact
 * than a library (a JS regex literal, a nested f-string or a bash heredoc can
 * mis-tint); that costs a colour, never a character.
 *
 * Escape-first: every token's own text goes through escapeHtml before a span
 * is put around it, and nothing is ever markup. A token that spans lines (a
 * block comment, a triple-quoted string) is closed and reopened at each line
 * break, so the output is one balanced HTML string per source line and the
 * File view's line numbers stay true. Stripping the tags and unescaping a
 * line gives the source line back exactly.
 *
 * DOM-free on purpose, like file-view.js: `tests/js/syntax_tint.test.mjs`
 * pins it under plain Node.
 */

import { escapeHtml } from './dom-utils.js';

const LINE_BREAK = /\r\n|\r|\n/;
// Anything up to the end of the input: a block that never closes runs to the
// end instead of failing to match (the rest of the file is its body).
const TO_END = '(?![\\s\\S])';

const words = function (list) { return '\\b(?:' + list.split(' ').join('|') + ')\\b'; };
const DQ = '"(?:\\\\.|[^"\\\\\\n\\r])*"?';          // one-line "…", closing quote optional
const SQ = "'(?:\\\\.|[^'\\\\\\n\\r])*'?";
const SLASH_COMMENTS = ['fv-cmt', '//[^\\n\\r]*'];
const BLOCK_COMMENT = ['fv-cmt', '/\\*[\\s\\S]*?(?:\\*/|' + TO_END + ')'];
const NUMBER = ['fv-num', '\\b(?:0[xX][\\da-fA-F_]+|\\d[\\d_]*(?:\\.\\d+)?(?:[eE][+-]?\\d+)?)\\b'];
const CALL = ['fv-name', '\\b[A-Za-z_$][\\w$]*(?=\\()'];

// A language: its flags and its ordered rules, [class | null, source, sub?].
// The first rule to match at the leftmost position wins; a null class is
// consumed and left plain (it stops a keyword from matching inside it). A
// `sub` names a language the matched text is tokenised again with.
const LANGS = {
  python: {
    flags: 'gm',
    rules: [
      ['fv-cmt', '#[^\\n\\r]*'],
      ['fv-str', '[rRbBuUfF]{0,2}(?:"""[\\s\\S]*?(?:"""|' + TO_END + ")|'''[\\s\\S]*?(?:'''|" + TO_END + '))'],
      ['fv-str', '[rRbBuUfF]{0,2}' + DQ],
      ['fv-str', '[rRbBuUfF]{0,2}' + SQ],
      ['fv-name', '^[ \\t]*@[\\w.]+'],
      ['fv-kw', words('and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield match case')],
      ['fv-num', words('True False None')],
      NUMBER,
      CALL,
    ],
  },
  powershell: {
    flags: 'gim',
    rules: [
      ['fv-cmt', '<#[\\s\\S]*?(?:#>|' + TO_END + ')'],
      ['fv-cmt', '#[^\\n\\r]*'],
      ['fv-str', '@"[\\s\\S]*?(?:\\n"@|' + TO_END + ")|@'[\\s\\S]*?(?:\\n'@|" + TO_END + ')'],
      ['fv-str', '"(?:`.|""|[^"`\\n\\r])*"?'],
      ['fv-str', "'(?:''|[^'\\n\\r])*'?"],
      ['fv-num', '\\$(?:true|false|null)\\b'],
      [null, '\\$(?:\\{[^}\\n\\r]*\\}|[\\w:]+)'],
      ['fv-kw', words('begin break catch class continue data do dynamicparam else elseif end enum exit filter finally for foreach from function if in param process return switch throw trap try until using while workflow')],
      ['fv-name', '\\b[A-Za-z]+-[A-Za-z]+\\b'],
      NUMBER,
    ],
  },
  js: {
    flags: 'gm',
    rules: [
      SLASH_COMMENTS,
      BLOCK_COMMENT,
      ['fv-str', '`(?:\\\\[\\s\\S]|[^`\\\\])*`?'],
      ['fv-str', DQ],
      ['fv-str', SQ],
      ['fv-kw', words('as async await break case catch class const continue debugger default delete do else enum export extends finally for from function get if implements import in instanceof interface let new of package private protected public readonly return set static super switch this throw try type typeof var void while with yield declare namespace abstract')],
      ['fv-num', words('true false null undefined NaN Infinity')],
      NUMBER,
      CALL,
    ],
  },
  c: {
    flags: 'gm',
    rules: [
      SLASH_COMMENTS,
      BLOCK_COMMENT,
      ['fv-str', DQ],
      ['fv-str', SQ],
      ['fv-str', '`[^`]*`?'],
      ['fv-kw', '^[ \\t]*#[ \\t]*[a-z]+'],
      ['fv-kw', words('abstract as break case catch chan char class const constexpr continue crate default defer delete do double dyn else enum extends extern fallthrough fn final finally float for foreach fun func function go goto if impl implements import in instanceof int interface internal let long loop match mod mut namespace new object open operator override package private protected pub public range readonly ref return select self short signed sizeof static string struct super switch template this throw throws trait try type typedef typeof union unsafe unsigned use using val var virtual void volatile when where while yield')],
      ['fv-num', words('true false null NULL nil None')],
      NUMBER,
      CALL,
    ],
  },
  css: {
    flags: 'gm',
    rules: [
      BLOCK_COMMENT,
      ['fv-str', DQ],
      ['fv-str', SQ],
      ['fv-kw', '@[\\w-]+|!important'],
      ['fv-name', '^[ \\t]+[\\w-]+(?=[ \\t]*:(?!:))'],
      ['fv-num', '#[\\da-fA-F]{3,8}\\b|[+-]?(?:\\d+\\.?\\d*|\\.\\d+)(?:%|[a-zA-Z]{1,4}\\b)?'],
    ],
  },
  html: {
    flags: 'gm',
    rules: [
      ['fv-cmt', '<!--[\\s\\S]*?(?:-->|' + TO_END + ')'],
      ['fv-kw', '<![^>]*>?'],
      [null, '</?[A-Za-z][^>]*>?', 'tag'],
    ],
  },
  // The inside of one HTML tag, tokenised again on its own.
  tag: {
    flags: 'g',
    rules: [
      ['fv-str', '"[^"]*"?|\'[^\']*\'?'],
      ['fv-kw', '^</?[\\w:.-]+|/?>$'],
      ['fv-name', '[\\w:@.-]+(?=\\s*=)'],
    ],
  },
  toml: {
    flags: 'gm',
    rules: [
      ['fv-cmt', '#[^\\n\\r]*'],
      ['fv-str', '"""[\\s\\S]*?(?:"""|' + TO_END + ")|'''[\\s\\S]*?(?:'''|" + TO_END + ')'],
      ['fv-str', DQ],
      ['fv-str', "'[^'\\n\\r]*'?"],
      ['fv-kw', '^[ \\t]*\\[\\[?[^\\]\\n\\r]*\\]?\\]?'],
      ['fv-name', '^[ \\t]*[\\w.-]+(?=[ \\t]*=)'],
      ['fv-num', words('true false inf nan')],
      ['fv-num', '[+-]?\\d[\\d_]*(?:\\.\\d+)?(?:[eE][+-]?\\d+)?\\b'],
    ],
  },
  yaml: {
    flags: 'gm',
    rules: [
      ['fv-cmt', '(?:^|[ \\t])#[^\\n\\r]*'],
      ['fv-str', DQ],
      ['fv-str', "'(?:''|[^'\\n\\r])*'?"],
      ['fv-kw', '^(?:---|\\.\\.\\.)[ \\t]*$'],
      ['fv-name', '^[ \\t]*(?:-[ \\t]+)?[\\w./-]+(?=[ \\t]*:(?:[ \\t]|$))'],
      ['fv-num', '\\b(?:true|false|null|yes|no|on|off)\\b|~'],
      ['fv-num', '[+-]?\\d[\\d_]*(?:\\.\\d+)?(?:[eE][+-]?\\d+)?\\b'],
    ],
  },
  shell: {
    flags: 'gm',
    rules: [
      ['fv-cmt', '(?:^|[ \\t])#[^\\n\\r]*'],
      ['fv-str', DQ],
      ["fv-str", "'[^'\\n\\r]*'?"],
      [null, '\\$(?:\\{[^}\\n\\r]*\\}|[A-Za-z_]\\w*|[0-9@*#?$!-])'],
      ['fv-kw', words('alias break case continue declare do done elif else esac eval exec exit export fi for function if in local readonly return select set shift source then trap unset until while')],
      ['fv-name', '(?:^|[ \\t])--?[A-Za-z][\\w-]*'],
      NUMBER,
    ],
  },
  sql: {
    flags: 'gim',
    rules: [
      ['fv-cmt', '--[^\\n\\r]*'],
      BLOCK_COMMENT,
      ['fv-str', "'(?:''|[^'\\n\\r])*'?"],
      ['fv-kw', words('add all alter and as asc begin between by case check column commit constraint create cross database default delete desc distinct drop else end exists foreign from full group having if in index inner insert into is join key left like limit not offset on or order outer primary references right rollback select set table then transaction union unique update values view when where with')],
      ['fv-num', words('true false null')],
      NUMBER,
      CALL,
    ],
  },
};

// File extension → language. An extension not listed has no colours.
const BY_EXT = {};
function assign(lang, exts) { exts.split(' ').forEach(function (e) { BY_EXT[e] = lang; }); }
assign('python', 'py pyw pyi');
assign('powershell', 'ps1 psm1 psd1');
assign('js', 'js mjs cjs jsx ts tsx');
assign('c', 'go rs java kt swift c h cpp hpp cc cs php gradle proto');
assign('css', 'css scss less');
assign('html', 'html htm xml svg vue svelte');
assign('toml', 'toml ini cfg conf');
assign('yaml', 'yml yaml');
assign('shell', 'sh bash zsh');
assign('sql', 'sql');

// Highlighting is skipped above either bound, whichever hits first (the
// content route already refuses anything over 256 KB): the tokenizer is
// linear, but a phone should never wait on colour for a big file.
export const TINT_MAX_LINES = 2000;
export const TINT_MAX_CHARS = 100 * 1024;

const compiled = {};

// The language's single alternation: rule i is capture group i + 1, so no rule
// source may hold a capturing group of its own.
function compile(name) {
  if (!compiled[name]) {
    const lang = LANGS[name];
    compiled[name] = {
      re: new RegExp(lang.rules.map(function (r) { return '(' + r[1] + ')'; }).join('|'), lang.flags),
      rules: lang.rules,
    };
  }
  return compiled[name];
}

// Escaped `text` in its class, as {cls, text} runs appended to `out`.
function tokenize(text, name, out) {
  const c = compile(name);
  const re = c.re;
  re.lastIndex = 0;
  let last = 0;
  let m;
  while ((m = re.exec(text)) !== null) {
    if (m[0] === '') {
      re.lastIndex += 1;
      continue;
    }
    if (m.index > last) out.push({ cls: null, text: text.slice(last, m.index) });
    let i = 1;
    while (m[i] === undefined) i += 1;
    const rule = c.rules[i - 1];
    if (rule[2]) tokenize(m[0], rule[2], out);
    else out.push({ cls: rule[0], text: m[0] });
    last = re.lastIndex;
  }
  if (last < text.length) out.push({ cls: null, text: text.slice(last) });
}

// The language for a file extension (lowercase, no dot), or null.
export function tintLanguage(ext) {
  return Object.prototype.hasOwnProperty.call(BY_EXT, ext) ? BY_EXT[ext] : null;
}

// Whether `text` is small enough to colour.
export function tintable(text) {
  if (text.length > TINT_MAX_CHARS) return false;
  let breaks = 0;
  for (let i = text.indexOf('\n'); i >= 0; i = text.indexOf('\n', i + 1)) {
    breaks += 1;
    if (breaks > TINT_MAX_LINES) return false;
  }
  return true;
}

// `text` as one escaped, tinted HTML string per source line (split on CRLF,
// CR or LF, so it lines up with file-view.js's own line split before its
// final-newline trim), for the language `lang` (tintLanguage's answer).
export function tintLines(text, lang) {
  const runs = [];
  tokenize(String(text), lang, runs);
  const out = [];
  let cur = '';
  runs.forEach(function (run) {
    run.text.split(LINE_BREAK).forEach(function (part, i) {
      if (i > 0) {
        out.push(cur);
        cur = '';
      }
      if (part === '') return;
      const esc = escapeHtml(part);
      cur += run.cls ? '<span class="' + run.cls + '">' + esc + '</span>' : esc;
    });
  });
  out.push(cur);
  return out;
}

// One diff hunk's lines (each with its ' ' / '+' / '-' prefix) as the escaped,
// tinted HTML of its code part, one entry per line; null for a line left
// plain. The new side (context + added) and the old side (context + removed)
// are tokenised as two texts, so a docstring or block comment that opens and
// closes inside the hunk colours across its lines, and each changed line reads
// the side it belongs to. A line the tokenizer cannot keep one-to-one (an
// embedded CR), an unknown language or a hunk over the size cut-off is all
// null: the caller draws it as it always did.
export function tintDiffLines(lines, lang) {
  const out = lines.map(function () { return null; });
  if (!lang || !tintable(lines.join('\n'))) return out;
  const sides = { 'new': [], old: [] };
  const trailingCr = [];
  const row = [];   // each line's position in the side it is read from
  for (let i = 0; i < lines.length; i += 1) {
    const c = lines[i].charAt(0);
    if (c === '\\') continue;   // "\ No newline at end of file"
    let code = lines[i].slice(1);
    trailingCr[i] = code.endsWith('\r');   // the tokenizer's line split would eat it
    if (trailingCr[i]) code = code.slice(0, -1);
    if (/\r/.test(code)) return out;
    row[i] = (c === '-' ? sides.old : sides['new']).length;
    if (c !== '-') sides['new'].push(code);
    if (c !== '+') sides.old.push(code);
  }
  const tinted = {};
  for (const key of ['new', 'old']) {
    // An empty side (an all-added or all-removed hunk) has nothing to read:
    // tintLines('') would still answer one empty line.
    tinted[key] = sides[key].length ? tintLines(sides[key].join('\n'), lang) : [];
    if (tinted[key].length !== sides[key].length) return out;
  }
  for (let i = 0; i < lines.length; i += 1) {
    const c = lines[i].charAt(0);
    if (lines[i] === '' || c === '\\') continue;   // drawn plain, as before
    out[i] = tinted[c === '-' ? 'old' : 'new'][row[i]] + (trailingCr[i] ? '\r' : '');
  }
  return out;
}
