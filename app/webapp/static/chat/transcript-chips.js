/* File chips in a transcript reply. */

import { icon } from '../_vendored/icons/icons.js';
import { fileKey } from './edited-card.js';
import { baseName } from './transcript-text.js';

// --- file chips --------------------------------------------------------
//
// A reply that names a file this session edited, in a code span or as a bare
// path in its prose, gets a file chip there: the code chip in the accent, a
// file glyph and the file's name, and a tap opens that file's diff in Changed
// files. Only an edited file links, and only when the
// name is unambiguous: the span must be the file's whole path or a trailing
// run of its folders and name (`styles.css`, `static/styles.css`, the full
// path) that no other edited file ends with. Everything else stays the plain
// code chip. A post-render DOM pass, like linkify(), so the markdown renderer
// stays string-only; the Life OS viewer has no session to open a panel
// against and never runs it. Which files count as edited, and what a tap
// opens, are the Chat pane's (session-transcript.js syncFileChips).

// A line reference after the name: ":12", ":12:3", ":12-20".
const LINE_REF_RE = /:\d+(?:[:-]\d+)?$/;
// A bare path's characters. Loose on purpose: only a run that resolves to an
// edited file becomes a chip.
const PATH_RUN_RE = /[\w.@~+\-/\\:]+/g;
const PATH_TRAIL_RE = /[.,;:!?]+$/;

// The edited file `text` names, with any line reference it carries, or null.
// `bare` (prose, not a code span) also asks it to look like a path, with a
// folder or an extension, so an extensionless file never links a plain word.
function resolveFile(text, files, bare) {
  let t = String(text || '').trim();
  if (!t || /\s/.test(t)) return null;
  const ref = t.match(LINE_REF_RE);
  if (ref) t = t.slice(0, -ref[0].length);
  if (bare && !/[\\/]/.test(t) && !/[^.]\.[a-z0-9]+$/i.test(t)) return null;
  const k = fileKey(t).replace(/^(\.\/)+/, '');
  if (!k) return null;
  const hits = files.filter(function (f) { return f.key === k || f.key.endsWith('/' + k); });
  return hits.length === 1 ? { file: hits[0], ref: ref ? ref[0] : '' } : null;
}

// `orig` is the node the chip stands in for, put back when the chips are redone.
// A tap calls `onOpen(chip, file)`.
function fileChip(hit, orig, onOpen) {
  const base = baseName(hit.file.path);
  const chip = document.createElement('button');
  chip.type = 'button';
  chip.className = 'tr-file-chip';
  chip.title = hit.file.path;
  chip.setAttribute('aria-label', 'Open the diff of ' + base);
  chip.dataset.path = hit.file.path;
  chip.innerHTML = icon('file-text');
  const name = document.createElement('span');
  name.className = 'tr-file-chip-name';
  name.textContent = base + hit.ref;
  chip.appendChild(name);
  chip._trOrig = orig;
  chip.addEventListener('click', function () { onOpen(chip, hit.file); });
  return chip;
}

// Text a bare path may sit in: prose, never a link, a code span or listing,
// a fenced block's header or a control.
function chipText(node) {
  const p = node.parentElement;
  return !!p && !p.closest('a, code, pre, button, .md-block');
}

// Chip every name in reply `body` that resolves to one of `files` (as
// edited-card.js's foldEdits names them).
export function chipFiles(body, files, onOpen) {
  body.querySelectorAll('code').forEach(function (code) {
    if (code.closest('pre, a, button')) return;
    const hit = resolveFile(code.textContent, files, false);
    if (hit) code.replaceWith(fileChip(hit, code, onOpen));
  });
  const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT, {
    acceptNode: function (node) { return chipText(node) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT; },
  });
  const nodes = [];
  let n;
  while ((n = walker.nextNode())) nodes.push(n);
  nodes.forEach(function (textNode) {
    const text = textNode.nodeValue;
    const frag = document.createDocumentFragment();
    let last = 0;
    let m;
    PATH_RUN_RE.lastIndex = 0;
    while ((m = PATH_RUN_RE.exec(text))) {
      const run = m[0].replace(PATH_TRAIL_RE, '');
      const hit = run && resolveFile(run, files, true);
      if (!hit) continue;
      if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
      frag.appendChild(fileChip(hit, document.createTextNode(run), onOpen));
      last = m.index + run.length;
    }
    if (!last) return;
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    textNode.parentNode.replaceChild(frag, textNode);
  });
}

export function unchipFiles(body) {
  body.querySelectorAll('.tr-file-chip').forEach(function (chip) { chip.replaceWith(chip._trOrig); });
  body.normalize();
}
