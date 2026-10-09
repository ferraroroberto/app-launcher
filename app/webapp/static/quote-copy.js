/* The copy button on a rendered Markdown blockquote (#1326) or fenced code
block (#1474).

`markdown.js` emits the button with the quote's (or block's) plain text in
`data-copy`,
but stays DOM- and toast-free so it runs under plain Node. The click lives
here instead: one delegated listener on the document, so every consumer of
the renderer (Chat pane, plan cards, Life OS viewers) gets a working button
without each wiring its own.

The clipboard write happens synchronously inside the tap, the same iOS rule
`session-transcript.js`'s card copy follows: an `await` ahead of it loses the
gesture and the copy fails silently on the phone.
*/

import { toast } from './api.js';

export function wireQuoteCopy() {
  document.addEventListener('click', function (ev) {
    const btn = ev.target.closest && ev.target.closest('.md-quote-copy, .md-block-copy');
    if (!btn) return;
    const what = btn.classList.contains('md-block-copy') ? 'Code' : 'Quote';
    ev.preventDefault();
    const fail = function () { toast('Clipboard unavailable — copy manually', 'error'); };
    let write;
    try {
      write = navigator.clipboard.writeText(btn.getAttribute('data-copy') || '');
    } catch (exc) {
      fail();
      return;
    }
    write.then(function () { toast(what + ' copied', 'good', { icon: 'copy' }); }, fail);
  });
}
