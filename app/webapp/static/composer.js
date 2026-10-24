/* The shared composer: the one input surface every
 * session view mounts — the terminal overlay, Chat mode and the Board
 * drawer — plus the chief answer sheet's Anything else as a field
 * Owns its own markup so a second mount is one
 * call, not a second copy of the HTML:
 *
 *   ┌ OCR staging tray (hidden until a screenshot is staged) ┐
 *   │ tall predictive <textarea>            │ 🎤 mic    │ ⌨ keys │
 *   │                                       │ 📎 attach │ ➤ send │
 *
 * The 2×2 grid keeps today's shape on every surface (an owner decision): mic · keys on row 1,
  * attach · send on row 2. The 📎 button
 * (it was a photo glyph) opens a menu — the same row-menu component
 * as the session ⚙️ gear — of everything that puts something into the
 * message: Photo, Extract text (OCR), Attach file (to context) and Attach
 * large file (not read). The OCR row hides with photo-ocr unconfigured; the
 * rest always show, so the button always opens the menu. Keys took the slot
 * OCR freed; the D-pad anchors above the composer where the thumb reaches it
 * (terminal-keys.js).
 *
 * Surface-specific behaviour comes in through `opts`, so the module never
 * touches a WebSocket, a session id or the PTY:
 *
 *   mountComposer(host, {
 *     placeholder:       textarea placeholder,
 *     send(text, meta):  deliver a composed prompt. `meta.hasImage` says the
 *                        buffer carries an attached-file path. Return
 *                        a falsy value — or a promise that resolves falsy /
 *                        rejects — to KEEP the draft (a failed send must not
 *                        eat the text); anything else clears the buffer.
 *                        Send is disabled while a promise is pending.
 *     upload(file, signal, kind): store one attachment and resolve the text
 *                        to append — its path, or for `kind === 'large'`
 *                        a line naming the path and telling the agent
 *                        not to read the file. A throw carries the failure's
 *                        reason. `signal` aborts when the operator cancels
 *                        pass it to the request.
 *     keys:              { send(bytes), onOpen() } to drive a PTY from the
 *                        ⌨ D-pad, or null when the session has none — the
 *                        button then renders disabled with the reason.
 *     keysOffReason:     optional — the disabled ⌨ button's title when the
 *                        default ("this session has no PTY") is not the
 *                        reason (the Board drawer has no socket).
 *     onDictationStart:  optional — fires when a recording starts (the
 *                        terminal silences an in-flight read-aloud).
 *     sendOnModEnter:    optional — bind Ctrl+Enter / Cmd+Enter in the
 *                        textarea to Send. Off by default; only
 *                        Chat mode asks for it.
 *     field:             optional — mount as a form field (the chief
 *                        answer sheet's Anything else). No ➤ Send and no ⌨
 *                        keys: the host's own primary action reads the
 *                        text, so `send` and `keys` are not used and the
 *                        tools column is mic over image.
 *     onChange:          optional — fires whenever the text changes, typed
 *                        or not (a dictated transcript, an appended path,
 *                        extracted OCR text), so a field host can re-gate.
 *   }) → handle
 *
 * The handle: `root`, `textarea`, `attachFiles(files)` (the entry point the
 * attach menu, the composer's own paste and drop and the terminal
 * host's paste and drop all use), `restoreDraft(text)` (hand back a cleared
 * draft whose send went unanswered), `reset()` (leave-surface teardown), `closePopovers()`,
 * `setAvailability({ dictate, ocr })`, `setKeys(keysOpts | null, reason?)`,
 * `setPlaceholder(text)`, `setSendable(enabled, reason)`, `isBusy()` (a
 * stopped dictation is still finalizing — a field host's own send waits it
 * out, as ➤ does).
 *
 * `setSendable(false, reason)` gates ➤ Send alone (a detached session
 * whose agent the console-input probe never proved) — the textarea, mic,
 * image and OCR stay usable. The gate is `aria-disabled`, never `disabled`:
 * a genuinely disabled button fires no tap event on iOS, so the reason could
 * only ever surface through a `title` hover a phone does not have, leaving a
 * dead button and no explanation. A tap still lands and toasts the
 * reason — the same convention as the Board drawer's actions and the
 * overlay's mode segments.
 *
 * Unavailable mic / keys render DISABLED rather than hidden: a grid that
 * collapses to three buttons is a different shape (design.md button-disabled
 * — flat muted text, never opacity). The OCR *option* hides, as the issue
 * asks, since it lives inside a menu.
 *
 * Voice dictation: the mic drops the transcript
 * into the textarea for review — never straight into a PTY. Send refuses
 * while a stopped recording is still finalizing; reset() disposes the
 * instance so the mic and the app-wide dictation mutex release even
 * mid-permission-prompt.
 *
 * Screenshot OCR: the option *stages* screenshots into the tray;
 * Extract sends ALL of them to photo-ocr in one /api/ocr call so it collates
 * them into one deduplicated text (overlapping shots merged), which lands in
 * the textarea for review. photo-ocr may hold that call for ~15 min on a slow
 * hub, so a tap on the busy Extract button cancels it (as uploads do); the staged shots stay for a retry.
 *
 * Large file: a file for the agent to move, archive or process by
 * path, never to read. It goes through the same queue, uncapped by the
 * ordinary 12 MB (its own limit is a Setting the upload enforces), streams
 * to the machine, and appends a line with its path and size that tells the
 * agent to leave its contents out of the context.
 *
 * Attach: uploads run sequentially (never
 * Promise.all — the append reads then writes the textarea), every path is
 * appended at the very end as its own paragraph regardless of the caret,
 * and the textarea is refocused synchronously inside the picker's `change`
 * tick so iOS keeps the keyboard up (WebKit honours .focus()→keyboard only
 * inside a user-activation tick). The queue itself is attach-batch.js: a
 * pick made mid-batch queues behind it in pick order, an oversize file is
 * refused before any byte is sent, and one summary toast ends the run,
 * naming each failure. While it runs the image button shows an hourglass
 * with "N/M" and a status line above the textarea says "Uploading N of M"
 * (neither takes focus, so the keyboard stays up). A tap on the busy image
 * button cancels the run: the upload in flight is aborted, the rest of
 * the queue is dropped, and the button is ready again with "Upload
 * cancelled". A paste or drop mid-run still queues behind it.
 */

import { apiFailToast, apiRawJson, toast } from './api.js';
import { readTerminalToken } from './webauthn.js';
import { createDictation, startWorkTimer, voiceDictationAvailable } from './voice.js';
import { createRowMenu } from './_vendored/row-menu/row-menu.js';
import { bindLongPressHint } from './long-press-hint.js';
import { mountKeysPopover } from './terminal-keys.js';
import { icon } from './_vendored/icons/icons.js';
import { cancelledSummary, createAttachQueue, summarize } from './attach-batch.js';

// Max visible rows before the textarea scrolls internally. Roomy enough
// for a long dictated voice note without the bar eating the whole
// screen when the keyboard is up. The CSS min-height floors it at 2 rows.
const _COMPOSE_MAX_ROWS = 8;

const _TITLE_MIC = 'Dictate (voice to text)';
const _TITLE_MIC_OFF = 'Dictation unavailable — voice-transcriber not configured';
const _LABEL_MIC = 'Dictate';
const _TITLE_KEYS = 'Keyboard keys';
const _TITLE_KEYS_OFF = 'No terminal keys — this session has no PTY';
const _TITLE_ATTACH = 'Attach';
// The button's own title. The plain one is the honest default: on a phone,
// and on the surfaces that don't opt into `sendOnModEnter`, ➤ is the only way
// to deliver. The shortcut variant names the binding that actually exists
// — it replaced 'Send (with Enter)', which promised a shortcut no
// code implemented and described the opposite of what the return key does.
const _TITLE_SEND = 'Send';
const _TITLE_SEND_MOD_ENTER = 'Send (Ctrl/Cmd+Enter)';
const _TITLE_CANCEL_UPLOAD = 'Cancel upload';
const _TITLE_CANCEL_EXTRACT = 'Cancel extraction';
const _LABEL_PHOTO = 'Photo';
const _LABEL_OCR = 'Extract text (OCR)';
const _LABEL_ATTACH = 'Attach file (to context)';
const _LABEL_LARGE = 'Attach large file (not read)';

// Auto-grow a composer textarea up to _COMPOSE_MAX_ROWS; the return key adds
// newlines on every surface, and ➤ Send delivers the text — plus Ctrl/Cmd+Enter
// on a surface that opted into `sendOnModEnter`.
export function growTextarea(ta) {
  ta.style.height = 'auto';
  const lineHeight = parseFloat(getComputedStyle(ta).lineHeight) || 20;
  ta.style.height =
    Math.min(ta.scrollHeight, _COMPOSE_MAX_ROWS * lineHeight + 16) + 'px';
  // Keep the caret (end of a freshly inserted transcript) in view.
  ta.scrollTop = ta.scrollHeight;
}

function render(host, placeholder) {
  host.classList.add('composer');
  host.innerHTML =
    '<div class="ocr-tray" hidden>' +
      '<div class="ocr-thumbs"></div>' +
      '<button type="button" class="ocr-extract">' + icon('scan-text') + ' Extract text</button>' +
    '</div>' +
    '<div class="composer-upload-status" role="status" aria-live="polite" hidden></div>' +
    '<div class="compose-bar">' +
      '<textarea class="compose-input composer-input" rows="2" enterkeyhint="enter"></textarea>' +
      '<div class="compose-tools">' +
        '<button type="button" class="icon-button compose-record composer-mic" aria-pressed="false">' + icon('mic') + '</button>' +
        '<button type="button" class="icon-button compose-record composer-keys">' + icon('keyboard') + '</button>' +
        '<button type="button" class="icon-button compose-record composer-image has-options">' + icon('paperclip') + '</button>' +
        '<button type="button" class="icon-button compose-send composer-send">' + icon('send-horizontal') + '</button>' +
      '</div>' +
    '</div>' +
    // No accept filter on the attach input: iOS then offers Photo
    // Library / Take Photo / Choose Files, so arbitrary files can be
    // attached. `multiple`: one gallery tap picks several photos.
    // Photo is the same upload narrowed to images, and the large-file
    // input feeds the not-read path.
    '<input type="file" class="composer-attach-input" multiple hidden>' +
    '<input type="file" class="composer-photo-input" accept="image/*" multiple hidden>' +
    '<input type="file" class="composer-large-input" multiple hidden>' +
    '<input type="file" class="composer-ocr-input" accept="image/*" multiple hidden>';
  const ta = host.querySelector('.composer-input');
  ta.placeholder = placeholder;
  return {
    tray: host.querySelector('.ocr-tray'),
    thumbs: host.querySelector('.ocr-thumbs'),
    extract: host.querySelector('.ocr-extract'),
    uploadStatus: host.querySelector('.composer-upload-status'),
    textarea: ta,
    mic: host.querySelector('.composer-mic'),
    keys: host.querySelector('.composer-keys'),
    image: host.querySelector('.composer-image'),
    send: host.querySelector('.composer-send'),
    attachInput: host.querySelector('.composer-attach-input'),
    photoInput: host.querySelector('.composer-photo-input'),
    largeInput: host.querySelector('.composer-large-input'),
    ocrInput: host.querySelector('.composer-ocr-input'),
  };
}

// A paste's files, or none when it also carries plain text (see the paste
// listener in mountComposer).
function pastedFiles(dt) {
  if (!dt || Array.prototype.indexOf.call(dt.types || [], 'text/plain') !== -1) return [];
  const files = [];
  const items = dt.items || [];
  for (let i = 0; i < items.length; i++) {
    const file = items[i].kind === 'file' ? items[i].getAsFile() : null;
    if (file) files.push(file);
  }
  return files;
}

function carriesFiles(dt) {
  return !!dt && Array.prototype.indexOf.call(dt.types || [], 'Files') !== -1;
}

// `label` pins the accessible name when the tooltip carries state the name
// must not (the mic stays "Dictate" while unavailable).
function setButtonState(btn, enabled, titleOn, titleOff, label) {
  btn.disabled = !enabled;
  btn.title = enabled ? titleOn : titleOff;
  btn.setAttribute('aria-label', label || btn.title);
}

// One mount's mutable state, threaded through the module-level helpers below.
// The helpers read and write these fields instead of closing over locals, so
// each stays a small function of its own.
function newComposerState(host, opts, el) {
  return {
    host: host,
    opts: opts,
    el: el,
    keysOpts: opts.keys || null,
    // Fixed for the life of the mount: the surface opts into the shortcut at
    // mount time, so the title can't drift from the binding.
    titleSend: opts.sendOnModEnter ? _TITLE_SEND_MOD_ENTER : _TITLE_SEND,
    // the buffer carries an attached-file path, so the surface's send
    // may hold its submitting CR back for the path→attachment conversion.
    hasImage: false,
    // Send's own gate (setSendable) — kept apart from the in-flight
    // disable an async send holds, so settling a send never re-enables a
    // button the surface gated off.
    sendBlocked: false,
    sendReason: '',
    sending: false,
    // Created by mountComposer once the state exists: the helpers below reach
    // them through here.
    dictation: null,
    keysPopover: null,
    attachQueue: null,
    imageMenu: null,
    ocrStaged: [],      // File objects awaiting a collated extraction
    ocrThumbUrls: [],   // object URLs to revoke when the tray re-renders
    // The in-flight extraction's AbortController, or null at rest. photo-ocr may
    // hold one call for ~15 min on a slow hub, so the busy Extract button
    // stays tappable and a second tap cancels it, like the upload.
    ocrCtrl: null,
    // OCR is offered only while photo-ocr is configured (setAvailability).
    ocrOffered: true,
  };
}

function composerGrow(c) {
  growTextarea(c.el.textarea);
  if (c.opts.onChange) c.opts.onChange();
}

// ---- OCR staging tray ----------------------------------------------------
function renderOcrTray(c) {
  const el = c.el;
  el.thumbs.innerHTML = '';
  c.ocrThumbUrls.forEach(function (u) { URL.revokeObjectURL(u); });
  c.ocrThumbUrls = [];
  if (!c.ocrStaged.length) {
    el.tray.hidden = true;
    return;
  }
  el.tray.hidden = false;
  c.ocrStaged.forEach(function (file, idx) {
    const cell = document.createElement('div');
    cell.className = 'ocr-thumb';
    const img = document.createElement('img');
    const url = URL.createObjectURL(file);
    c.ocrThumbUrls.push(url);
    img.src = url;
    img.alt = 'staged screenshot ' + (idx + 1);
    const rm = document.createElement('button');
    rm.type = 'button';
    rm.className = 'ocr-thumb-x';
    rm.innerHTML = icon('x');
    rm.title = 'Remove';
    rm.addEventListener('click', function () {
      c.ocrStaged.splice(idx, 1);
      renderOcrTray(c);
    });
    cell.appendChild(img);
    cell.appendChild(rm);
    el.thumbs.appendChild(cell);
  });
  el.extract.innerHTML =
    icon('camera') + ' Extract text (' + c.ocrStaged.length + ')';
}

function clearOcrStaging(c) {
  c.ocrStaged = [];
  renderOcrTray(c);
}

// Run OCR over EVERY staged image in one call so photo-ocr deduplicates
// the overlap. Same bearer + passkey terminal token as an upload. A tap while
// one is running cancels it instead: staged images are kept for a retry.
async function runOcrExtraction(c) {
  const el = c.el;
  if (c.ocrCtrl) {
    c.ocrCtrl.abort();
    return;
  }
  const list = c.ocrStaged.slice();
  if (!list.length) return;
  const fd = new FormData();
  list.forEach(function (f, i) {
    fd.append('files', f, f.name || ('screenshot-' + (i + 1) + '.png'));
  });
  const ctrl = new AbortController();
  c.ocrCtrl = ctrl;
  // `aria-busy`, never `disabled`: iOS fires no tap on a disabled button.
  el.extract.setAttribute('aria-busy', 'true');
  el.extract.title = _TITLE_CANCEL_EXTRACT;
  el.extract.setAttribute('aria-label', _TITLE_CANCEL_EXTRACT);
  el.image.disabled = true;
  const stopTimer = startWorkTimer(
    el.extract, icon('camera') + ' Extract text', icon('hourglass') + ' Reading '
  );
  try {
    const body = await apiRawJson('/api/ocr', {
      method: 'POST', terminalToken: readTerminalToken(), body: fd,
      signal: ctrl.signal,
    });
    const text = body && body.text;
    const plural = list.length > 1;
    if (!text) {
      toast('No text found in the image' + (plural ? 's' : ''), undefined, { icon: 'camera' });
      return;
    }
    // Insert at the caret with a leading space when the textarea already
    // has trailing content, so the OCR appends cleanly to typed text.
    const ta = el.textarea;
    const before = ta.value.slice(0, ta.selectionStart);
    const sep = (before && !/\s$/.test(before)) ? ' ' : '';
    ta.setRangeText(sep + text, ta.selectionStart, ta.selectionEnd, 'end');
    composerGrow(c);
    ta.focus();
    clearOcrStaging(c);
    // A partly read take is a 200 whose text already carries a
    // `[missing: photo N …]` line per unread photo; say so
    // up front rather than a plain success the owner might not look past.
    const missed = (body.missing_photos || []).length;
    if (missed) {
      toast(
        missed + ' of ' + list.length + ' screenshot' + (plural ? 's' : '') +
          ' could not be read — see the [missing] lines.',
        'error',
        { icon: 'camera' }
      );
    } else {
      toast(
        'Text extracted from ' + list.length + ' image' +
          (plural ? 's' : '') + ' — review, then tap Send.',
        'good',
        { icon: 'camera' }
      );
    }
  } catch (exc) {
    if (ctrl.signal.aborted) {
      toast('Extraction cancelled', undefined, { icon: 'camera' });
    } else {
      apiFailToast('OCR failed', exc);
    }
  } finally {
    c.ocrCtrl = null;
    stopTimer();
    // The timer restores the bare label; a kept (cancelled/failed) staging
    // needs its "(N)" back.
    if (c.ocrStaged.length) renderOcrTray(c);
    el.extract.removeAttribute('aria-busy');
    el.extract.removeAttribute('aria-label');
    el.extract.removeAttribute('title');
    el.image.disabled = false;
  }
}

function wireOcr(c) {
  const el = c.el;
  el.ocrInput.addEventListener('change', function () {
    const picked = el.ocrInput.files;
    const list = picked && picked.length ? Array.prototype.slice.call(picked) : [];
    el.ocrInput.value = '';
    // Stage, don't send — accumulate across taps; Extract collates them all.
    if (list.length) {
      c.ocrStaged = c.ocrStaged.concat(list);
      renderOcrTray(c);
    }
  });
  el.extract.addEventListener('click', function () { runOcrExtraction(c); });
}

// ---- attach ------------------------------------------------------------
// Always append at the very end as its own paragraph — never
// splice at the caret, which glued the path onto whatever the cursor
// happened to sit on. A blank line separates it from existing text, so
// sequential attachments stack cleanly: <text>\n\n<path1>\n\n<path2>.
function appendParagraph(c, text) {
  const ta = c.el.textarea;
  const cur = ta.value;
  const sep = cur ? (/\n\n$/.test(cur) ? '' : (/\n$/.test(cur) ? '\n' : '\n\n')) : '';
  ta.value = cur + sep + text;
  ta.selectionStart = ta.selectionEnd = ta.value.length;
  composerGrow(c);
  ta.focus();
}

// `kind` is 'large' for a not-read large file: its line names a path
// the agent must not open as an attachment, so it does not set hasImage
// (which holds Send's CR back for an image conversion).
function appendPath(c, path, kind) {
  appendParagraph(c, path);
  if (kind !== 'large') c.hasImage = true;
}

function labelImage(c, text) {
  c.el.image.title = text;
  c.el.image.setAttribute('aria-label', text);
}

// "N of M" on the image button and in the status line while the
// queue runs; both go back to rest when it drains. The button stays
// tappable — a tap now cancels the run — so it is `aria-busy`,
// never `disabled`: iOS fires no tap on a disabled button.
function showUploadProgress(c, p) {
  const el = c.el;
  if (!p) {
    el.uploadStatus.hidden = true;
    el.uploadStatus.textContent = '';
    el.image.classList.remove('is-uploading');
    el.image.removeAttribute('aria-busy');
    el.image.innerHTML = icon('paperclip');
    labelImage(c, _TITLE_ATTACH);
    return;
  }
  el.uploadStatus.hidden = false;
  el.uploadStatus.textContent = 'Uploading ' + p.index + ' of ' + p.total + ' · ' + p.name;
  el.image.classList.add('is-uploading');
  el.image.setAttribute('aria-busy', 'true');
  labelImage(c, _TITLE_CANCEL_UPLOAD);
  el.image.innerHTML = icon('hourglass') +
    '<span class="composer-upload-count">' + p.index + '/' + p.total + '</span>';
}

function createComposerAttachQueue(c) {
  return createAttachQueue({
    upload: function (file, signal, kind) { return c.opts.upload(file, signal, kind); },
    onAppend: function (path, kind) { appendPath(c, path, kind); },
    onProgress: function (p) { showUploadProgress(c, p); },
    onSettle: function (r) {
      if (!r.total) return;
      const s = r.cancelled ? cancelledSummary(r.ok, r.total) : summarize(r.ok, r.total, r.failures);
      toast(s.text, s.tone, { icon: 'paperclip' });
    },
    log: function (e) {
      console.info('[composer] upload', e.name, e.bytes + ' B', e.ms + ' ms',
        e.ok ? 'ok' : 'failed: ' + e.reason);
    },
  });
}

function attachFiles(c, files, kind) {
  const list = files ? Array.prototype.slice.call(files) : [];
  if (!list.length) return Promise.resolve();
  // refocus NOW, synchronously in the caller's gesture tick — the
  // native picker dismissed the keyboard, and a post-upload focus() lands
  // outside the gesture: the caret shows but the keyboard stays down, so
  // the whole composer drops to the screen bottom, out of thumb reach.
  try { c.el.textarea.focus(); } catch (_) {}
  return c.attachQueue.enqueue(list, kind);
}

function wirePicker(c, input, kind) {
  input.addEventListener('change', function () {
    const picked = input.files;
    const list = picked && picked.length ? Array.prototype.slice.call(picked) : [];
    input.value = '';
    attachFiles(c, list, kind);
  });
}

function wireAttach(c) {
  const el = c.el;
  const host = c.host;
  wirePicker(c, el.attachInput);
  wirePicker(c, el.photoInput);
  wirePicker(c, el.largeInput, 'large');

  // Paste and drop reach attachFiles from every mount. They were only
  // wired to the terminal's xterm host (terminal-image.js), so Chat, which
  // has no terminal, took neither. A clipboard that also carries plain text
  // pastes as text: Office and similar apps put a rendered image of copied
  // text beside it, and that paste must never turn into an upload.
  el.textarea.addEventListener('paste', function (ev) {
    const files = pastedFiles(ev.clipboardData);
    if (!files.length) return;
    ev.preventDefault();
    attachFiles(c, files);
  });
  host.addEventListener('dragover', function (ev) {
    if (carriesFiles(ev.dataTransfer)) ev.preventDefault();
  });
  host.addEventListener('drop', function (ev) {
    const dt = ev.dataTransfer;
    if (!dt || !dt.files || !dt.files.length) return;
    ev.preventDefault();
    attachFiles(c, dt.files);
  });

  // While an upload runs a tap on 📎 cancels it instead of opening
  // the menu. Registered BEFORE the menu binds its own anchor handler, so
  // this listener wins the click and stops the menu from opening.
  el.image.addEventListener('click', function (ev) {
    if (!c.attachQueue.busy()) return;
    ev.stopImmediatePropagation();
    c.attachQueue.cancel();
  });
}

function mountImageMenu(c) {
  const el = c.el;
  const host = c.host;
  // Above the composer, right edges 10px in (mockup screen 3). The vendored
  // row-menu mounts the open menu on <body>, so the Board drawer's column
  // carousel, which clipped an absolutely-positioned menu, cannot.
  c.imageMenu = createRowMenu({
    className: 'composer-menu',
    placement: {
      against: function () { return host; },
      side: 'above', offset: 6, inset: 10, minWidth: 260,
    },
  });
  c.imageMenu.attach('image', el.image, [
    {
      glyph: 'image', label: _LABEL_PHOTO,
      dataset: { item: 'composer-menu-photo' },
      onTap: function () { el.photoInput.click(); },
    },
    {
      glyph: 'scan-text', label: _LABEL_OCR,
      dataset: { item: 'composer-menu-ocr' },
      hidden: function () { return !c.ocrOffered; },
      onTap: function () { el.ocrInput.click(); },
    },
    {
      glyph: 'file-text', label: _LABEL_ATTACH,
      dataset: { item: 'composer-menu-attach' },
      onTap: function () { el.attachInput.click(); },
    },
    {
      glyph: 'package', label: _LABEL_LARGE,
      dataset: { item: 'composer-menu-large' },
      onTap: function () { el.largeInput.click(); },
    },
  ]);
  labelImage(c, _TITLE_ATTACH);
}

// ---- send --------------------------------------------------------------
function finishSend(c) {
  c.hasImage = false;
  c.el.textarea.value = '';
  c.el.textarea.style.height = '';
  c.el.textarea.focus();
}

// The surface's gate is aria-disabled (see the module header) so a
// phone tap still reaches the button and can say why. The in-flight hold
// stays a real `disabled`: it lasts one request and has no reason to state.
// It must also *look* held: `disabled` alone left ➤ at full accent,
// so a send in progress read as a live button inviting a second tap.
function syncSend(c) {
  const send = c.el.send;
  send.disabled = c.sending;
  if (send.classList.contains('is-sending') !== c.sending) {
    send.classList.toggle('is-sending', c.sending);
    send.innerHTML = icon(c.sending ? 'hourglass' : 'send-horizontal');
  }
  if (c.sending) send.setAttribute('aria-busy', 'true');
  else send.removeAttribute('aria-busy');
  if (c.sendBlocked) send.setAttribute('aria-disabled', 'true');
  else send.removeAttribute('aria-disabled');
}

function submit(c) {
  if (c.sendBlocked) {
    toast(c.sendReason, '', { icon: 'send-horizontal' });
    return;
  }
  if (c.sending) return;
  // a dictation that just stopped is still finalizing until the
  // canonical transcript settles into the textarea. Reading + clearing the
  // buffer mid-window raced that settle — wait it out instead.
  if (c.dictation.isBusy()) {
    toast('Still transcribing — wait for the transcript, then tap Send', 'error', { icon: 'mic' });
    return;
  }
  const text = c.el.textarea.value;
  if (!text) return;
  let res;
  try {
    res = c.opts.send(text, { hasImage: c.hasImage });
  } catch (_) {
    return;
  }
  if (res && typeof res.then === 'function') {
    // Async delivery (Chat mode's /input route): hold Send and
    // keep the draft until the surface says it went through.
    c.sending = true;
    syncSend(c);
    res.then(function (ok) {
      if (ok !== false && ok !== null && ok !== undefined) finishSend(c);
    }, function () { /* the surface toasted; the draft stays */ })
      .finally(function () { c.sending = false; syncSend(c); });
    return;
  }
  // Sync delivery (the PTY WebSocket): clear inside the tap gesture so
  // the refocus keeps the phone keyboard up.
  if (res) finishSend(c);
}

function wireSend(c) {
  const ta = c.el.textarea;
  c.el.send.addEventListener('click', function () { submit(c); });
  ta.addEventListener('input', function () { composerGrow(c); });

  // Ctrl+Enter / Cmd+Enter sends — opt-in per surface, because only
  // Chat asked for it: the terminal overlay has its own input path and the
  // Board drawer keeps today's shape. Three things this deliberately is:
  //
  //  - Plain Enter is untouched and still inserts a newline. Multi-line
  //    prompts are the normal case here, so Enter-sends was never wanted.
  //  - Inert on a phone with no device sniff. An on-screen keyboard has no
  //    Ctrl and no Cmd key, so its return key arrives with both modifiers
  //    false and cannot match the guard — the soft keyboard's behaviour is
  //    unchanged by construction, not by a touch check.
  //  - Routed through submit(), never a second send path. The refusal toast
  //    for a gated Send, the in-flight hold and the still-finalizing
  //    dictation wait all apply to the shortcut exactly as they do to a tap.
  //
  // preventDefault() so the gesture doesn't also leave a stray newline behind
  // — including on the refusal paths, where the keystroke was still a send.
  if (c.opts.sendOnModEnter) {
    ta.addEventListener('keydown', function (ev) {
      if (ev.key !== 'Enter' || !(ev.ctrlKey || ev.metaKey)) return;
      ev.preventDefault();
      submit(c);
    });
  }
}

// ---- availability and handle -------------------------------------------
function setAvailability(c, a) {
  if (a && 'dictate' in a) {
    setButtonState(c.el.mic, !!a.dictate, _TITLE_MIC, _TITLE_MIC_OFF, _LABEL_MIC);
  }
  if (a && 'ocr' in a) c.ocrOffered = !!a.ocr;
}

function setSendable(c, enabled, reason) {
  c.sendBlocked = !enabled;
  c.sendReason = enabled ? '' : (reason || 'Sending is unavailable');
  c.el.send.title = enabled ? c.titleSend : c.sendReason;
  c.el.send.setAttribute('aria-label', enabled ? 'Send' : c.sendReason);
  syncSend(c);
}

function setPlaceholder(c, text) {
  c.el.textarea.placeholder = text;
  c.el.textarea.setAttribute('aria-label', text);
}

function setKeys(c, k, reason) {
  c.keysOpts = k || null;
  setButtonState(c.el.keys, !!c.keysOpts, _TITLE_KEYS, reason || _TITLE_KEYS_OFF);
  if (!c.keysOpts) c.keysPopover.close();
}

function closePopovers(c) {
  c.keysPopover.close();
  c.imageMenu.close();
}

// Leave-surface teardown: drop the draft, the staged screenshots and any
// in-flight recording. dispose() (not stop()) so the mic and the app-wide
// dictation mutex release even mid-permission-prompt or mid-finalize, and
// a late transcript is dropped rather than written into a bar about to be
// hidden and cleared.
function resetComposer(c) {
  c.el.textarea.value = '';
  c.el.textarea.style.height = '';
  c.hasImage = false;
  clearOcrStaging(c);
  c.dictation.dispose();
  closePopovers(c);
}

// Give back a draft the surface cleared on a send it later found unanswered
// Whatever was typed meanwhile stays, after the returned text.
function restoreDraft(c, text) {
  const cur = c.el.textarea.value;
  c.el.textarea.value = cur ? text + '\n' + cur : text;
  c.el.textarea.dispatchEvent(new Event('input', { bubbles: true }));
}

export function mountComposer(host, opts) {
  const el = render(host, opts.placeholder || 'Message for the agent');
  // The terminal-keys glyph isn't a conventional icon.
  bindLongPressHint(el.keys);
  const c = newComposerState(host, opts, el);
  if (opts.field) {
    host.classList.add('composer-field');
    el.send.hidden = true;
    el.keys.hidden = true;
  }

  c.dictation = createDictation({
    button: el.mic,
    getTextarea: function () { return el.textarea; },
    onRender: function () { composerGrow(c); },
    onStart: opts.onDictationStart || function () {},
  });
  el.mic.addEventListener('click', c.dictation.toggle);

  c.keysPopover = mountKeysPopover(el.keys, host, {
    send: function (bytes) { if (c.keysOpts) c.keysOpts.send(bytes); },
    onOpen: function () { if (c.keysOpts && c.keysOpts.onOpen) c.keysOpts.onOpen(); },
  });

  wireOcr(c);
  c.attachQueue = createComposerAttachQueue(c);
  wireAttach(c);
  mountImageMenu(c);
  wireSend(c);

  setAvailability(c, { dictate: voiceDictationAvailable(), ocr: false });
  setKeys(c, c.keysOpts, opts.keysOffReason);
  setSendable(c, true);

  return {
    root: host,
    textarea: el.textarea,
    attachFiles: function (files, kind) { return attachFiles(c, files, kind); },
    restoreDraft: function (text) { restoreDraft(c, text); },
    reset: function () { resetComposer(c); },
    closePopovers: function () { closePopovers(c); },
    setAvailability: function (a) { setAvailability(c, a); },
    setKeys: function (k, reason) { setKeys(c, k, reason); },
    setPlaceholder: function (text) { setPlaceholder(c, text); },
    setSendable: function (enabled, reason) { setSendable(c, enabled, reason); },
    isBusy: function () { return c.dictation.isBusy(); },
  };
}
