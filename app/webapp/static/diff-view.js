/* One edit's diff (#1349), drawn from the server's hunks — Chat's per-step
 * diffs, and the session's Changed files panel after them.
 *
 * Same look as the Show changes viewer (#977, `.chg-diff` + the `--diff-*`
 * tokens), plus a line-number gutter where the harness recorded real
 * numbers (`diff.numbered`). A diff worked out from an edit's own old/new
 * text has none, and gets no gutter rather than made-up numbers; its hunks
 * are separated by a plain ⋯ instead of an @@ header. Every line keeps its
 * +/− prefix, so added and removed read without the colour. Built with
 * textContent throughout: a diff is file content, never markup.
 *
 * A file whose language syntax-tint.js knows gets token colours: a
 * line's code part is set from the tokenizer's escaped markup, after its
 * prefix in a `.d-pfx` span, so `.d-txt`'s text is still the whole patch line.
 */

import { extension } from './file-view.js';
import { tintDiffLines, tintLanguage, tintable } from './syntax-tint.js';

function hunkHeader(h) {
  let oldCount = 0;
  let newCount = 0;
  h.lines.forEach(function (line) {
    const c = line.charAt(0);
    if (c === '-' || c === ' ') oldCount += 1;
    if (c === '+' || c === ' ') newCount += 1;
  });
  return '@@ -' + h.old_start + ',' + oldCount + ' +' + h.new_start + ',' + newCount + ' @@';
}

// The tokenizer language for a diff's file, and whether colour was skipped for
// size: {lang, off}. `lines` is every diff line of the file; the cut-off is the
// File view's, over the whole diff.
export function diffTint(lines, path) {
  const lang = tintLanguage(extension(path));
  if (!lang) return { lang: null, off: false };
  if (!tintable(lines.join('\n'))) return { lang: null, off: true };
  return { lang: lang, off: false };
}

// One block-level line; `ln` null means no gutter (an unnumbered diff).
// `codeHtml`, when not null, is the line's escaped, tinted code part.
function row(box, cls, ln, text, codeHtml) {
  const line = document.createElement('span');
  line.className = cls;
  if (ln !== null) {
    const num = document.createElement('span');
    num.className = 'd-ln';
    num.setAttribute('aria-hidden', 'true');
    num.textContent = ln;
    line.appendChild(num);
  }
  const body = document.createElement('span');
  body.className = 'd-txt';
  if (codeHtml === null) {
    body.textContent = text === '' ? ' ' : text;
  } else {
    line.classList.add('d-tint');
    const pfx = document.createElement('span');
    pfx.className = 'd-pfx';
    pfx.textContent = text.charAt(0);
    const code = document.createElement('span');
    code.innerHTML = codeHtml;
    body.append(pfx, code);
  }
  line.appendChild(body);
  box.appendChild(line);
}

// `diff` is {hunks: [{old_start, new_start, lines}], numbered}; `path` names
// the file, which picks its token colours (none when absent or unknown).
export function renderHunks(diff, path) {
  const numbered = !!diff.numbered;
  const box = document.createElement('pre');
  box.className = 'chg-diff tr-diff' + (numbered ? ' tr-diff-numbered' : '');
  const hunks = diff.hunks || [];
  const tint = diffTint([].concat.apply([], hunks.map(function (h) { return h.lines; })), path);
  if (tint.off) box.dataset.tint = 'off';
  hunks.forEach(function (h, i) {
    if (numbered) row(box, 'd-hunk', '', hunkHeader(h), null);
    else if (i > 0) row(box, 'd-hunk', null, '⋯', null);
    const code = tintDiffLines(h.lines, tint.lang);
    let oldLn = h.old_start;
    let newLn = h.new_start;
    h.lines.forEach(function (line, j) {
      const c = line.charAt(0);
      let cls = 'd-ctx';
      let ln = '';
      if (c === '+') {
        cls = 'd-add';
        ln = newLn++;
      } else if (c === '-') {
        cls = 'd-del';
        ln = oldLn++;
      } else if (c === '\\') {
        cls = 'd-meta';   // "\ No newline at end of file" numbers nothing
      } else {
        ln = newLn++;
        oldLn++;
      }
      row(box, cls, numbered ? String(ln) : null, line, code[j]);
    });
  });
  return box;
}
