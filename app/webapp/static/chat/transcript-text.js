/* The transcript renderer's building blocks: links in plain text, times,
 * the meta line, capped text blocks, a path as "folder / name", an edit's
 * line counts and the turn chevron. Every part of the renderer
 * (transcript-render.js and the modules beside it) draws with these.
 */

// Bare URLs become links everywhere transcript text is shown — reply
// prose (after markdown, so a markdown link is left alone), prompts, tool
// bodies. DOM-walking, never string-replacing HTML: text nodes outside an
// existing <a> are split around each match into real anchor elements, so
// nothing a transcript contains can ever be interpreted as markup.
const URL_RE = /https?:\/\/[^\s<>"'`]+/g;
const TRAILING_PUNCT_RE = /[.,;:!?'")\]]+$/;

export function linkify(root) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: function (node) {
      return node.parentElement && node.parentElement.closest('a')
        ? NodeFilter.FILTER_REJECT
        : NodeFilter.FILTER_ACCEPT;
    },
  });
  const nodes = [];
  let n;
  while ((n = walker.nextNode())) {
    if (URL_RE.test(n.nodeValue)) nodes.push(n);
    URL_RE.lastIndex = 0;
  }
  nodes.forEach(function (textNode) {
    const text = textNode.nodeValue;
    const frag = document.createDocumentFragment();
    let last = 0;
    let m;
    URL_RE.lastIndex = 0;
    while ((m = URL_RE.exec(text))) {
      let url = m[0];
      const trail = url.match(TRAILING_PUNCT_RE);
      if (trail) url = url.slice(0, -trail[0].length);
      if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
      const a = document.createElement('a');
      a.href = url;
      a.textContent = url;
      a.target = '_blank';
      a.rel = 'noopener';
      frag.appendChild(a);
      last = m.index + url.length;
    }
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    textNode.parentNode.replaceChild(frag, textNode);
  });
}

export function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

export function meta(label, ts) {
  const el = document.createElement('div');
  el.className = 'tr-meta';
  const when = fmtTime(ts);
  el.textContent = when ? label + ' · ' + when : label;
  return el;
}

export function pre(text, truncated, className) {
  const el = document.createElement('pre');
  el.className = className || 'tr-pre';
  el.textContent = text || '';
  linkify(el);
  if (truncated) {
    const mark = document.createElement('span');
    mark.className = 'tr-trunc';
    mark.textContent = '\n(truncated)';
    el.appendChild(mark);
  }
  return el;
}

export function firstLine(text, max) {
  const line = String(text || '').split('\n')[0].trim();
  return line.length > max ? line.slice(0, max) + '…' : line;
}

// The last segment of a POSIX or Windows path.
export function baseName(path) {
  const parts = String(path || '').split(/[\\/]/);
  return parts[parts.length - 1] || String(path || '');
}

// A file as "folder / name" pieces: the folder relative to the open
// session's project when the path sits under it (or was recorded relative),
// else just its parent folder. Never the whole absolute path, which wrapped
// to three lines at 390px; callers keep that on `title`. `session` is the
// open session (null: no project to be relative to).
export function fileParts(path, session) {
  const full = String(path || '').replace(/\\/g, '/');
  const base = baseName(full);
  const parent = full.slice(0, full.length - base.length).replace(/\/+$/, '');
  if (!/^([a-zA-Z]:)?\//.test(full)) return { dir: parent, base: base };
  const root = String((session && session.project_dir) || '').replace(/\\/g, '/').replace(/\/+$/, '');
  if (root && parent.toLowerCase().startsWith(root.toLowerCase())) {
    const rel = parent.slice(root.length);
    if (!rel || rel.charAt(0) === '/') return { dir: rel.replace(/^\/+/, ''), base: base };
  }
  return { dir: baseName(parent), base: base };
}

// An edit's line counts, "+4 −0" (a true minus sign, as the Claude Code app),
// coloured like the Changed files panel's. Its own element, so a step line's
// ellipsis can never cut it.
export function deltaEl(added, removed, className) {
  const el = document.createElement('span');
  el.className = className;
  const add = document.createElement('span');
  add.className = 'chg-add';
  add.textContent = '+' + (added || 0);
  const del = document.createElement('span');
  del.className = 'chg-del';
  del.textContent = '−' + (removed || 0);
  el.append(add, ' ', del);
  return el;
}
export function chevron() {
  const chev = document.createElement('span');
  chev.className = 'tr-turn-chevron';
  chev.setAttribute('aria-hidden', 'true');
  chev.textContent = '›';
  return chev;
}
