/* Terminal image attach: the 🖼 button, paste, and drag-and-drop.
 *
 * An image lands on the session-host, which stores it and either pastes the
 * stored path straight into the PTY, or — when the compose bar is open —
 * returns the path (`inline=1`) so it can be appended to the textarea for
 * review before sending (issues #41/#366/#448/#450).
 *
 * Split out of terminal.js in issue #723, continuing the #315 split — this
 * is self-contained and orthogonal to the terminal's own lifecycle and
 * sizing.
 */

import { els, state } from './state.js';
import { apiFailToast, apiRaw, toast } from './api.js';
import { growComposeInput } from './terminal-compose.js';
import { readTerminalToken } from './webauthn.js';

// `opts.silent` (issue #448): suppress this call's own success toast so a
// multi-file selection can fire one summary toast instead of N flickering
// ones — toast() is a single-slot control, a rapid-fire second call just
// cancels the first's timer. Errors are never silenced. Returns true on
// success so the caller can count how many of a batch actually landed.
async function sendImage(file, opts) {
  const t = state.terminal;
  if (!t || !file) return false;
  const silent = !!(opts && opts.silent);
  // Compose bar open: ask the session-host to skip the paste-into-PTY
  // step (inline=1) and just return the stored path, so we can drop it
  // into the textarea for review-before-send — mirroring 📋 (issue #41).
  const inline = !!t.composeOpen;
  const fd = new FormData();
  fd.append('file', file, file.name || 'image.png');
  try {
    const tt = readTerminalToken();
    const res = await apiRaw(
      '/api/claude-code/sessions/' + encodeURIComponent(t.sid) + '/image' +
        (inline ? '?inline=1' : ''),
      { method: 'POST', terminalToken: tt, body: fd }
    );
    if (!res.ok) {
      const b = await res.json().catch(function () { return null; });
      throw new Error((b && b.detail) || ('HTTP ' + res.status));
    }
    if (inline) {
      const body = await res.json().catch(function () { return null; });
      const path = body && body.path;
      if (path) {
        const ta = els.terminalComposeInput;
        // Always append at the very end as its own paragraph (issue #366)
        // — never splice at the caret, which glued the path onto whatever
        // the cursor happened to sit on. A blank line separates it from
        // existing text, so sequential attachments stack cleanly:
        // <text>\n\n<path1>\n\n<path2>. Applies to every inline trigger
        // (compose attach, outer 🖼 button, paste/drop with the bar open).
        const cur = ta.value;
        const sep = cur ? (/\n\n$/.test(cur) ? '' : (/\n$/.test(cur) ? '\n' : '\n\n')) : '';
        ta.value = cur + sep + path;
        ta.selectionStart = ta.selectionEnd = ta.value.length;
        growComposeInput();
        ta.focus();
        // #450: mark that this compose buffer now carries an attached image
        // path, so the ➤ Send handler defers its submitting CR (see
        // sendSubmit). Claude Code runs a pasted-path→image-attachment
        // conversion on submit that swallows a CR arriving in the same burst
        // as the path — deferring the CR lets the conversion settle first, so
        // the prompt submits on the first tap instead of needing a second
        // Enter. Cleared once the buffer is sent or reset.
        t.composeHasImage = true;
      }
      if (!silent) toast('Uploaded — path added to the compose bar.', 'good', { icon: 'paperclip' });
    } else {
      if (!silent) toast('Sent — the file path was pasted into the prompt.', 'good', { icon: 'paperclip' });
      if (t.term) t.term.focus();
    }
    return true;
  } catch (exc) {
    apiFailToast('Image failed', exc);
    return false;
  }
}

// Every image entry point: the 🖼 button, the file input it opens, and
// paste/drop straight onto the terminal host.
export function wireTerminalImage() {
  els.terminalImage.addEventListener('click', function () {
    els.terminalImageInput.click();
  });
  els.terminalImageInput.addEventListener('change', async function () {
    const picked = els.terminalImageInput.files;
    const list = picked && picked.length
      ? Array.prototype.slice.call(picked) : [];
    els.terminalImageInput.value = '';
    if (!list.length) return;
    // Issue #448: upload sequentially (never Promise.all) — sendImage's
    // inline-append path reads then writes ta.value, so concurrent calls
    // would race and corrupt the append order. Each call is silent; one
    // summary toast fires at the end instead of N flickering ones.
    const inline = !!(state.terminal && state.terminal.composeOpen);
    // Issue #450: reopen the on-screen keyboard NOW, synchronously inside this
    // `change` tick — the native photo picker dismissed it, and the `change`
    // event is still a trusted continuation of the user's gesture. Same iOS
    // rule the read-aloud/voice paths lean on: WebKit only honours
    // .focus()→keyboard inside an active user-activation tick. sendImage's own
    // post-upload ta.focus() lands *after* the upload `await`, i.e. outside the
    // gesture — the caret shows but the keyboard stays down, so the whole
    // compose bar (Send included) drops to the true screen bottom, out of thumb
    // reach. Only meaningful when the compose bar is open (inline); the
    // non-inline path pastes into the PTY and refocuses the terminal instead.
    if (inline && els.terminalComposeInput) {
      try { els.terminalComposeInput.focus(); } catch (_) {}
    }
    let ok = 0;
    for (let i = 0; i < list.length; i++) {
      if (await sendImage(list[i], { silent: true })) ok++;
    }
    if (!ok) return;
    const plural = ok > 1;
    toast(
      inline
        ? 'Uploaded ' + ok + ' image' + (plural ? 's' : '') +
            ' — path' + (plural ? 's' : '') + ' added to the compose bar.'
        : 'Sent — ' + ok + ' file path' + (plural ? 's' : '') +
            ' pasted into the prompt.',
      'good',
      { icon: 'paperclip' }
    );
  });
  els.terminalHost.addEventListener('paste', function (ev) {
    const items = (ev.clipboardData && ev.clipboardData.items) || [];
    for (let i = 0; i < items.length; i++) {
      if (items[i].type && items[i].type.indexOf('image') === 0) {
        const file = items[i].getAsFile();
        if (file) { ev.preventDefault(); sendImage(file); return; }
      }
    }
  });
  els.terminalHost.addEventListener('dragover', function (ev) {
    ev.preventDefault();
  });
  els.terminalHost.addEventListener('drop', function (ev) {
    const file = ev.dataTransfer && ev.dataTransfer.files &&
      ev.dataTransfer.files[0];
    if (file && file.type && file.type.indexOf('image') === 0) {
      ev.preventDefault();
      sendImage(file);
    }
  });
}
