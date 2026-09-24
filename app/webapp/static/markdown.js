/* The SPA's minimal markdown renderer — feature-agnostic, shared.

Lived in `life-os.js` until #1006, which made the module name a lie and
coupled two unrelated tabs through it: the Coding tab's Chat pane
(`session-transcript.js`) imported the renderer from the Life OS feature
module. It also left a verification blind spot — `.fleet.toml`'s `lifeos`
e2e surface narrows a `life-os.js`-only diff to the Life OS tests, which do
NOT include `test_session_transcript.py`, so a change to the shared renderer
was never exercised against its other consumer. As its own module it is
unclassified, so the router escalates to the full suite and both consumers
are covered.

NOT in `dom-utils.js`, which the finding proposed: that module declares
itself dependency-free and ten modules import it. A leaf of its own keeps
that property intact. `escapeHtml` moved into `dom-utils.js` (#1141) so this
module imports nothing DOM- or storage-bound and runs under plain Node, which
is how `tests/js/markdown.test.mjs` pins the parser without a browser.
*/

import { escapeHtml } from './dom-utils.js';

// Escape-first, then apply a small, safe subset (headings, bold, italic,
// inline code, fenced code, links, bulleted and numbered lists, GFM tables,
// paragraphs). Content comes from the user's own private files over a
// passkey-gated tailnet link, but we still escape every byte before
// formatting so a stray `<script>` in a note can never execute.
function inlineMd(s) {
  return s
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

// GFM tables (#1141). A row's cells split on `|`, except inside a backtick
// code span or when escaped as `\|`; one optional leading and trailing pipe
// is dropped. Runs on already-escaped text, so every cell stays escaped.
function splitRow(line) {
  let body = line.trim();
  if (body.startsWith('|')) body = body.slice(1);
  if (body.endsWith('|') && !body.endsWith('\\|')) body = body.slice(0, -1);
  const cells = [];
  let cell = '';
  let inSpan = false;
  for (let i = 0; i < body.length; i++) {
    const ch = body[i];
    if (ch === '\\' && body[i + 1] === '|') { cell += '|'; i++; continue; }
    if (ch === '`') inSpan = !inSpan;
    if (ch === '|' && !inSpan) { cells.push(cell.trim()); cell = ''; continue; }
    cell += ch;
  }
  cells.push(cell.trim());
  return cells;
}

// The delimiter row decides both "is this a table" and each column's
// alignment: `---` none, `:--` left, `--:` right, `:-:` center.
function parseDelimiter(line) {
  if (!line.includes('-')) return null;
  const cells = splitRow(line);
  const aligns = [];
  for (const c of cells) {
    const m = c.match(/^(:?)-+(:?)$/);
    if (!m) return null;
    aligns.push(m[1] && m[2] ? 'center' : m[2] ? 'right' : m[1] ? 'left' : '');
  }
  return aligns;
}

function renderCell(tag, content, align) {
  const cls = align ? ' class="md-al-' + align + '"' : '';
  return '<' + tag + cls + '>' + inlineMd(content) + '</' + tag + '>';
}

// Wrapped so a table wider than its container scrolls sideways inside it --
// the page never does (styles.css `.md-table-wrap`).
function renderTable(header, aligns, rows) {
  const width = header.length;
  const row = function (cells, tag) {
    let html = '<tr>';
    for (let i = 0; i < width; i++) html += renderCell(tag, cells[i] || '', aligns[i]);
    return html + '</tr>';
  };
  let html = '<div class="md-table-wrap"><table class="md-table"><thead>'
    + row(header, 'th') + '</thead>';
  if (rows.length) {
    html += '<tbody>' + rows.map(function (r) { return row(r, 'td'); }).join('') + '</tbody>';
  }
  return html + '</table></div>';
}

export function renderMarkdown(text) {
  const lines = escapeHtml(text).split('\n');
  const out = [];
  let inCode = false;
  // The open list's tag, 'ul' or 'ol'; null outside a list.
  let listTag = null;
  let para = [];

  function flushPara() {
    if (para.length) {
      out.push('<p>' + inlineMd(para.join(' ')) + '</p>');
      para = [];
    }
  }
  function flushList() {
    if (listTag) { out.push('</' + listTag + '>'); listTag = null; }
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.trim().startsWith('```')) {
      flushPara(); flushList();
      if (inCode) { out.push('</code></pre>'); inCode = false; }
      else { out.push('<pre class="md-code"><code>'); inCode = true; }
      continue;
    }
    if (inCode) { out.push(line); continue; }

    // A table needs a piped header row followed by a delimiter row with the
    // same cell count; anything short of that stays a paragraph. Body rows
    // run to the first blank or pipe-less line.
    if (line.includes('|') && i + 1 < lines.length) {
      const header = splitRow(line);
      const aligns = parseDelimiter(lines[i + 1]);
      if (aligns && aligns.length === header.length) {
        flushPara(); flushList();
        const rows = [];
        i += 2;
        while (i < lines.length && lines[i].trim() && lines[i].includes('|')) {
          rows.push(splitRow(lines[i]));
          i++;
        }
        i--;
        out.push(renderTable(header, aligns, rows));
        continue;
      }
    }

    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      flushPara(); flushList();
      const level = h[1].length;
      out.push('<h' + level + '>' + inlineMd(h[2]) + '</h' + level + '>');
      continue;
    }
    // Numbered items open an <ol> (#1202), keeping a first number other
    // than 1; a change of list kind closes the open list first.
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    const numbered = bullet ? null : line.match(/^\s*(\d+)[.)]\s+(.*)$/);
    if (bullet || numbered) {
      flushPara();
      const tag = bullet ? 'ul' : 'ol';
      if (listTag !== tag) {
        flushList();
        const start = numbered && numbered[1] !== '1' ? ' start="' + Number(numbered[1]) + '"' : '';
        out.push('<' + tag + start + '>');
        listTag = tag;
      }
      out.push('<li>' + inlineMd(bullet ? bullet[1] : numbered[2]) + '</li>');
      continue;
    }
    if (!line.trim()) { flushPara(); flushList(); continue; }
    para.push(line.trim());
  }
  if (inCode) out.push('</code></pre>');
  flushPara(); flushList();
  return out.join('\n');
}
