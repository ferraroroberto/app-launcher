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
itself dependency-free and ten modules import it, while this renderer needs
`escapeHtml` from `api.js`. A leaf of its own keeps both properties intact.
*/

import { escapeHtml } from './api.js';

// Escape-first, then apply a small, safe subset (headings, bold, italic,
// inline code, fenced code, links, unordered lists, paragraphs). Content
// comes from the user's own private files over a passkey-gated tailnet
// link, but we still escape every byte before formatting so a stray
// `<script>` in a note can never execute.
function inlineMd(s) {
  return s
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

export function renderMarkdown(text) {
  const lines = escapeHtml(text).split('\n');
  const out = [];
  let inCode = false;
  let inList = false;
  let para = [];

  function flushPara() {
    if (para.length) {
      out.push('<p>' + inlineMd(para.join(' ')) + '</p>');
      para = [];
    }
  }
  function flushList() {
    if (inList) { out.push('</ul>'); inList = false; }
  }

  lines.forEach(function (line) {
    if (line.trim().startsWith('```')) {
      flushPara(); flushList();
      if (inCode) { out.push('</code></pre>'); inCode = false; }
      else { out.push('<pre class="md-code"><code>'); inCode = true; }
      return;
    }
    if (inCode) { out.push(line); return; }

    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      flushPara(); flushList();
      const level = h[1].length;
      out.push('<h' + level + '>' + inlineMd(h[2]) + '</h' + level + '>');
      return;
    }
    const li = line.match(/^\s*[-*]\s+(.*)$/);
    if (li) {
      flushPara();
      if (!inList) { out.push('<ul>'); inList = true; }
      out.push('<li>' + inlineMd(li[1]) + '</li>');
      return;
    }
    if (!line.trim()) { flushPara(); flushList(); return; }
    para.push(line.trim());
  });
  if (inCode) out.push('</code></pre>');
  flushPara(); flushList();
  return out.join('\n');
}
