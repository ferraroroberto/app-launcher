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
 */

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

// One block-level line; `ln` null means no gutter (an unnumbered diff).
function row(box, cls, ln, text) {
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
  body.textContent = text === '' ? ' ' : text;
  line.appendChild(body);
  box.appendChild(line);
}

// `diff` is {hunks: [{old_start, new_start, lines}], numbered}.
export function renderHunks(diff) {
  const numbered = !!diff.numbered;
  const box = document.createElement('pre');
  box.className = 'chg-diff tr-diff' + (numbered ? ' tr-diff-numbered' : '');
  (diff.hunks || []).forEach(function (h, i) {
    if (numbered) row(box, 'd-hunk', '', hunkHeader(h));
    else if (i > 0) row(box, 'd-hunk', null, '⋯');
    let oldLn = h.old_start;
    let newLn = h.new_start;
    h.lines.forEach(function (line) {
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
      row(box, cls, numbered ? String(ln) : null, line);
    });
  });
  return box;
}
