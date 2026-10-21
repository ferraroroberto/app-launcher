/* A changed file, drawn for its type (#1689): the Changed files panel's File
 * view, beside its Diff.
 *
 * Markdown goes through the shared sanitising renderer (markdown.js), JSON is
 * pretty-printed and tinted, code is a monospace listing with a line-number
 * gutter (CSS counters) and, for the main languages, token colours from
 * syntax-tint.js (#1749), and anything else is plain text. Every byte of the file is escaped before any
 * markup is put around it, so a file holding HTML or a `<script>` reads as
 * exactly that text and never runs.
 *
 * DOM-free on purpose, like markdown.js: it builds HTML strings and imports
 * nothing bound to the page, so `tests/js/file_view.test.mjs` pins it under
 * plain Node.
 */

import { escapeHtml } from './dom-utils.js';
import { renderMarkdown } from './markdown.js';
import { tintLanguage, tintLines, tintable } from './syntax-tint.js';

const MARKDOWN = new Set(['md', 'markdown', 'mdx']);
const JSON_EXT = new Set(['json', 'jsonc', 'json5', 'webmanifest', 'code-workspace']);
const CODE = new Set([
  'py', 'pyw', 'pyi', 'js', 'mjs', 'cjs', 'jsx', 'ts', 'tsx', 'css', 'scss', 'less',
  'html', 'htm', 'xml', 'svg', 'vue', 'svelte', 'ps1', 'psm1', 'psd1', 'bat', 'cmd',
  'sh', 'bash', 'zsh', 'yml', 'yaml', 'toml', 'ini', 'cfg', 'conf', 'sql', 'go', 'rs',
  'java', 'kt', 'swift', 'c', 'h', 'cpp', 'hpp', 'cc', 'cs', 'rb', 'php', 'lua', 'r',
  'mmd', 'dockerfile', 'makefile', 'gradle', 'tf', 'graphql', 'proto',
]);

// The file's extension, or its whole name for the extensionless kinds
// (Dockerfile, Makefile), lowercased.
export function extension(name) {
  const base = String(name || '').split(/[\\/]/).pop().toLowerCase();
  const dot = base.lastIndexOf('.');
  return dot > 0 ? base.slice(dot + 1) : base;
}

// 'markdown' | 'json' | 'code' | 'text', from the file's name alone.
export function fileKind(name) {
  const ext = extension(name);
  if (MARKDOWN.has(ext)) return 'markdown';
  if (JSON_EXT.has(ext)) return 'json';
  if (CODE.has(ext)) return 'code';
  return 'text';
}

// Drop the empty line a final newline leaves behind.
function trimFinal(all) {
  if (all.length > 1 && all[all.length - 1] === '') all.pop();
  return all;
}

function lines(text) {
  return trimFinal(String(text).split(/\r\n|\r|\n/));
}

// One <span> per line inside the listing; the gutter is a CSS counter on
// each, so a long line wraps right of its number and copying the listing
// takes the code alone. `html` is already-escaped markup per line.
function listing(cls, htmlLines) {
  return '<pre class="fv-listing ' + cls + '"><code>' +
    htmlLines.map(function (l) { return '<span class="fv-line">' + (l || ' ') + '</span>'; }).join('') +
    '</code></pre>';
}

// Escape-first JSON tint: the pretty text is split into tokens, each token's
// own text escaped, then wrapped by kind. A string followed by a colon is a
// key. Anything the pattern does not name passes through escaped, untinted.
const JSON_TOKEN = /("(?:\\.|[^"\\])*")(\s*:)?|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|\b(true|false|null)\b/g;

export function tintJson(text) {
  let out = '';
  let last = 0;
  let m;
  JSON_TOKEN.lastIndex = 0;
  while ((m = JSON_TOKEN.exec(text)) !== null) {
    out += escapeHtml(text.slice(last, m.index));
    if (m[1] !== undefined) {
      const cls = m[2] !== undefined ? 'fv-key' : 'fv-str';
      out += '<span class="' + cls + '">' + escapeHtml(m[1]) + '</span>' + escapeHtml(m[2] || '');
    } else if (m[3] !== undefined) {
      out += '<span class="fv-num">' + escapeHtml(m[3]) + '</span>';
    } else {
      out += '<span class="fv-lit">' + m[4] + '</span>';
    }
    last = JSON_TOKEN.lastIndex;
  }
  return out + escapeHtml(text.slice(last));
}

// JSON that parses is shown re-indented; anything else (JSONC comments, a
// half-written file) as the code listing, untouched.
function renderJson(text) {
  let pretty;
  try {
    pretty = JSON.stringify(JSON.parse(text), null, 2);
  } catch (exc) {
    return listing('fv-code', lines(text).map(escapeHtml));
  }
  return listing('fv-code fv-json', lines(pretty).map(tintJson));
}

// Code: coloured when its language is known and the file small enough, else
// the plain listing (a large file says why it has no colours).
function renderCode(name, text) {
  const lang = tintLanguage(extension(name));
  if (!lang) return listing('fv-code', lines(text).map(escapeHtml));
  if (!tintable(text)) {
    return '<div class="fv-note muted small">Colours are off for large files</div>' +
      listing('fv-code', lines(text).map(escapeHtml));
  }
  return listing('fv-code fv-tint', trimFinal(tintLines(text, lang)));
}

// The file's text as markup for `kind` (fileKind's answer, or forced).
export function renderFileView(name, text, kind) {
  const k = kind || fileKind(name);
  const body = String(text == null ? '' : text);
  if (k === 'markdown') return '<div class="fv-md tr-md">' + renderMarkdown(body) + '</div>';
  if (k === 'json') return renderJson(body);
  if (k === 'code') return renderCode(name, body);
  return '<pre class="fv-text">' + escapeHtml(body) + '</pre>';
}

// The absolute path the PC knows a changed file by, in Windows form, from a
// recorded path: as is when absolute, against `projectDir` when relative
// (a Codex patch names files from its working folder), else null.
export function windowsPath(path, projectDir) {
  const p = String(path || '').replace(/\//g, '\\');
  if (!p) return null;
  const absolute = /^[A-Za-z]:\\/.test(p) || p.startsWith('\\');
  const root = String(projectDir || '').replace(/\//g, '\\').replace(/\\+$/, '');
  if (!absolute && !root) return null;
  const full = absolute ? p : root + '\\' + p;
  const lead = full.match(/^\\*/)[0];   // a UNC share's leading pair stays
  const out = [];
  full.slice(lead.length).split('\\').forEach(function (seg) {
    if (seg === '' || seg === '.') return;
    if (seg === '..') {
      if (out.length > 1) out.pop();   // never above the drive or share
      return;
    }
    out.push(seg);
  });
  return lead + out.join('\\');
}
