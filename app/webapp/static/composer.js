/* The shared composer (#980, Step 1 of #979): the one input surface every
 * session view mounts — the terminal overlay today, Chat mode (#983) and the
 * Board drawer (#984) next. Owns its own markup so a second mount is one
 * call, not a second copy of the HTML:
 *
 *   ┌ OCR staging tray (hidden until a screenshot is staged) ┐
 *   │ tall predictive <textarea>            │ 🎤 mic  │ ⌨ keys │
 *   │                                       │ 🖼 image │ ➤ send │
 *
 * The 2×2 grid keeps today's shape on every surface (owner's decision, #979
 * decision log): mic · keys on row 1, image · send on row 2. OCR is not a
 * button of its own any more — it is the second option inside the image
 * button's menu ("Attach image or file" / "Extract text from screenshots",
 * the same row-menu component as the session ⚙️ gear); the small dot on the
 * button marks it as having options. With photo-ocr unconfigured the menu
 * would have one row, so the button opens the picker directly and the dot
 * hides. Keys took the slot OCR freed; the D-pad anchors above the composer
 * where the thumb reaches it (terminal-keys.js).
 *
 * Surface-specific behaviour comes in through `opts`, so the module never
 * touches a WebSocket, a session id or the PTY:
 *
 *   mountComposer(host, {
 *     placeholder:       textarea placeholder,
 *     send(text, meta):  deliver a composed prompt. `meta.hasImage` says the
 *                        buffer carries an attached-file path (#450). Return
 *                        a falsy value — or a promise that resolves falsy /
 *                        rejects — to KEEP the draft (a failed send must not
 *                        eat the text); anything else clears the buffer.
 *                        Send is disabled while a promise is pending.
 *     upload(file):      store one attachment and resolve its path (or null
 *                        on failure, after the caller's own error toast).
 *                        The composer appends the path to the text.
 *     keys:              { send(bytes), onOpen() } to drive a PTY from the
 *                        ⌨ D-pad, or null when the session has none — the
 *                        button then renders disabled with the reason.
 *     onDictationStart:  optional — fires when a recording starts (the
 *                        terminal silences an in-flight read-aloud, #190).
 *   }) → handle
 *
 * The handle: `root`, `textarea`, `attachFiles(files)` (paste / drop entry
 * point), `reset()` (leave-surface teardown), `closePopovers()`,
 * `setAvailability({ dictate, ocr })`, `setKeys(keysOpts | null)`,
 * `setPlaceholder(text)`, `setSendable(enabled, reason)`.
 *
 * `setSendable(false, reason)` gates ➤ Send alone (#983: a detached session
 * whose agent the console-input probe never proved) — the textarea, mic,
 * image and OCR stay usable, and the reason is the button's title.
 *
 * Unavailable mic / keys render DISABLED rather than hidden: a grid that
 * collapses to three buttons is a different shape (design.md button-disabled
 * — flat muted text, never opacity). The OCR *option* hides, as the issue
 * asks, since it lives inside a menu.
 *
 * Voice dictation (#165 / #168 / #489 / #755): the mic drops the transcript
 * into the textarea for review — never straight into a PTY. Send refuses
 * while a stopped recording is still finalizing (#489); reset() disposes the
 * instance so the mic and the app-wide dictation mutex release even
 * mid-permission-prompt (#755).
 *
 * Screenshot OCR (#171): the option *stages* screenshots into the tray;
 * Extract sends ALL of them to photo-ocr in one /api/ocr call so it collates
 * them into one deduplicated text (overlapping shots merged), which lands in
 * the textarea for review.
 *
 * Attach (#41 / #366 / #448 / #450): uploads run sequentially (never
 * Promise.all — the append reads then writes the textarea), every path is
 * appended at the very end as its own paragraph regardless of the caret,
 * one summary toast fires per pick, and the textarea is refocused
 * synchronously inside the picker's `change` tick so iOS keeps the keyboard
 * up (WebKit honours .focus()→keyboard only inside a user-activation tick).
 */

import { apiFailToast, apiRaw, toast } from './api.js';
import { readTerminalToken } from './webauthn.js';
import { createDictation, startWorkTimer, voiceDictationAvailable } from './voice.js';
import { createRowMenu } from './row-menu.js';
import { mountKeysPopover } from './terminal-keys.js';
import { icon } from './_vendored/icons/icons.js';

// Max visible rows before the textarea scrolls internally. Roomy enough
// for a long dictated voice note (#165) without the bar eating the whole
// screen when the keyboard is up. The CSS min-height floors it at 2 rows.
const _COMPOSE_MAX_ROWS = 8;

const _TITLE_MIC = 'Dictate (voice → text)';
const _TITLE_MIC_OFF = 'Dictation unavailable — voice-transcriber not configured';
const _TITLE_KEYS = 'Keyboard keys';
const _TITLE_KEYS_OFF = 'No terminal keys — this session has no PTY';
const _TITLE_IMAGE = 'Attach image or file';
const _TITLE_IMAGE_MENU = 'Attach image or file · Extract text from screenshots';
const _TITLE_SEND = 'Send (with Enter)';
const _LABEL_ATTACH = 'Attach image or file';
const _LABEL_OCR = 'Extract text from screenshots';

// Auto-grow a composer textarea up to _COMPOSE_MAX_ROWS; the phone's return
// key adds newlines, only ➤ Send delivers the text.
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
    '<div class="compose-bar">' +
      '<textarea class="compose-input composer-input" rows="2" enterkeyhint="enter"></textarea>' +
      '<div class="compose-tools">' +
        '<button type="button" class="compose-record composer-mic" aria-pressed="false">' + icon('mic') + '</button>' +
        '<button type="button" class="compose-record composer-keys">' + icon('keyboard') + '</button>' +
        '<button type="button" class="compose-record composer-image">' + icon('image') + '</button>' +
        '<button type="button" class="compose-send composer-send">' + icon('send-horizontal') + '</button>' +
      '</div>' +
    '</div>' +
    // No accept filter on the attach input (#366): iOS then offers Photo
    // Library / Take Photo / Choose Files, so arbitrary files can be
    // attached. `multiple` (#448): one gallery tap picks several photos.
    '<input type="file" class="composer-attach-input" multiple hidden>' +
    '<input type="file" class="composer-ocr-input" accept="image/*" multiple hidden>';
  const ta = host.querySelector('.composer-input');
  ta.placeholder = placeholder;
  return {
    tray: host.querySelector('.ocr-tray'),
    thumbs: host.querySelector('.ocr-thumbs'),
    extract: host.querySelector('.ocr-extract'),
    textarea: ta,
    mic: host.querySelector('.composer-mic'),
    keys: host.querySelector('.composer-keys'),
    image: host.querySelector('.composer-image'),
    send: host.querySelector('.composer-send'),
    attachInput: host.querySelector('.composer-attach-input'),
    ocrInput: host.querySelector('.composer-ocr-input'),
  };
}

function setButtonState(btn, enabled, titleOn, titleOff) {
  btn.disabled = !enabled;
  btn.title = enabled ? titleOn : titleOff;
  btn.setAttribute('aria-label', btn.title);
}

export function mountComposer(host, opts) {
  const el = render(host, opts.placeholder || 'Message for the agent');
  let keysOpts = opts.keys || null;
  let ocrOn = false;
  // #450: the buffer carries an attached-file path, so the surface's send
  // may hold its submitting CR back for the path→attachment conversion.
  let hasImage = false;
  // #983: Send's own gate (setSendable) — kept apart from the in-flight
  // disable an async send holds, so settling a send never re-enables a
  // button the surface gated off.
  let sendBlocked = false;
  let sending = false;

  function grow() { growTextarea(el.textarea); }

  // ---- dictation -------------------------------------------------------
  const dictation = createDictation({
    button: el.mic,
    getTextarea: function () { return el.textarea; },
    onRender: grow,
    onStart: opts.onDictationStart || function () {},
  });
  el.mic.addEventListener('click', dictation.toggle);

  // ---- keys D-pad --------------------------------------------------------
  const keysPopover = mountKeysPopover(el.keys, host, {
    send: function (bytes) { if (keysOpts) keysOpts.send(bytes); },
    onOpen: function () { if (keysOpts && keysOpts.onOpen) keysOpts.onOpen(); },
  });

  // ---- OCR staging tray ----------------------------------------------------
  let ocrStaged = [];      // File objects awaiting a collated extraction
  let ocrThumbUrls = [];   // object URLs to revoke when the tray re-renders

  function renderOcrTray() {
    el.thumbs.innerHTML = '';
    ocrThumbUrls.forEach(function (u) { URL.revokeObjectURL(u); });
    ocrThumbUrls = [];
    if (!ocrStaged.length) {
      el.tray.hidden = true;
      return;
    }
    el.tray.hidden = false;
    ocrStaged.forEach(function (file, idx) {
      const cell = document.createElement('div');
      cell.className = 'ocr-thumb';
      const img = document.createElement('img');
      const url = URL.createObjectURL(file);
      ocrThumbUrls.push(url);
      img.src = url;
      img.alt = 'staged screenshot ' + (idx + 1);
      const rm = document.createElement('button');
      rm.type = 'button';
      rm.className = 'ocr-thumb-x';
      rm.innerHTML = icon('x');
      rm.title = 'Remove';
      rm.addEventListener('click', function () {
        ocrStaged.splice(idx, 1);
        renderOcrTray();
      });
      cell.appendChild(img);
      cell.appendChild(rm);
      el.thumbs.appendChild(cell);
    });
    el.extract.innerHTML =
      icon('camera') + ' Extract text (' + ocrStaged.length + ')';
  }

  function clearOcrStaging() {
    ocrStaged = [];
    renderOcrTray();
  }

  // Run OCR over EVERY staged image in one call so photo-ocr deduplicates
  // the overlap. Same bearer + passkey terminal token as an upload.
  async function runOcrExtraction() {
    const list = ocrStaged.slice();
    if (!list.length) return;
    const fd = new FormData();
    list.forEach(function (f, i) {
      fd.append('files', f, f.name || ('screenshot-' + (i + 1) + '.png'));
    });
    el.extract.disabled = true;
    el.image.disabled = true;
    const stopTimer = startWorkTimer(
      el.extract, icon('camera') + ' Extract text', icon('hourglass') + ' Reading '
    );
    try {
      const res = await apiRaw('/api/ocr', {
        method: 'POST', terminalToken: readTerminalToken(), body: fd,
      });
      if (!res.ok) {
        const b = await res.json().catch(function () { return null; });
        throw new Error((b && b.detail) || ('HTTP ' + res.status));
      }
      const body = await res.json().catch(function () { return null; });
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
      grow();
      ta.focus();
      clearOcrStaging();
      toast(
        'Text extracted from ' + list.length + ' image' +
          (plural ? 's' : '') + ' — review, then tap Send.',
        'good',
        { icon: 'camera' }
      );
    } catch (exc) {
      apiFailToast('OCR failed', exc);
    } finally {
      stopTimer();
      el.extract.disabled = false;
      el.image.disabled = false;
    }
  }

  el.ocrInput.addEventListener('change', function () {
    const picked = el.ocrInput.files;
    const list = picked && picked.length ? Array.prototype.slice.call(picked) : [];
    el.ocrInput.value = '';
    // Stage, don't send — accumulate across taps; Extract collates them all.
    if (list.length) {
      ocrStaged = ocrStaged.concat(list);
      renderOcrTray();
    }
  });
  el.extract.addEventListener('click', runOcrExtraction);

  // ---- attach ------------------------------------------------------------
  function appendPath(path) {
    const ta = el.textarea;
    // Always append at the very end as its own paragraph (#366) — never
    // splice at the caret, which glued the path onto whatever the cursor
    // happened to sit on. A blank line separates it from existing text, so
    // sequential attachments stack cleanly: <text>\n\n<path1>\n\n<path2>.
    const cur = ta.value;
    const sep = cur ? (/\n\n$/.test(cur) ? '' : (/\n$/.test(cur) ? '\n' : '\n\n')) : '';
    ta.value = cur + sep + path;
    ta.selectionStart = ta.selectionEnd = ta.value.length;
    grow();
    ta.focus();
    hasImage = true;
  }

  async function attachFiles(files) {
    const list = files ? Array.prototype.slice.call(files) : [];
    if (!list.length) return;
    // #450: refocus NOW, synchronously in the caller's gesture tick — the
    // native picker dismissed the keyboard, and a post-upload focus() lands
    // outside the gesture: the caret shows but the keyboard stays down, so
    // the whole composer drops to the screen bottom, out of thumb reach.
    try { el.textarea.focus(); } catch (_) {}
    let ok = 0;
    for (let i = 0; i < list.length; i++) {
      const path = await opts.upload(list[i]);
      if (path) { appendPath(path); ok++; }
    }
    if (!ok) return;
    const plural = ok > 1;
    toast(
      'Uploaded ' + ok + ' file' + (plural ? 's' : '') +
        ' — path' + (plural ? 's' : '') + ' added to the message.',
      'good',
      { icon: 'paperclip' }
    );
  }

  el.attachInput.addEventListener('change', function () {
    const picked = el.attachInput.files;
    const list = picked && picked.length ? Array.prototype.slice.call(picked) : [];
    el.attachInput.value = '';
    attachFiles(list);
  });

  // The image button's two options. Registered BEFORE the menu binds its own
  // anchor handler, so with OCR unavailable this listener wins the click,
  // opens the picker directly and stops the (one-row) menu from opening.
  el.image.addEventListener('click', function (ev) {
    if (ocrOn) return;
    ev.stopImmediatePropagation();
    el.attachInput.click();
  });
  const imageMenu = createRowMenu('composer-menu');
  const menu = imageMenu.attach('image', el.image, [
    {
      glyph: 'image', label: _LABEL_ATTACH, text: _LABEL_ATTACH,
      className: 'composer-menu-attach',
      onTap: function () { el.attachInput.click(); },
    },
    {
      glyph: 'scan-text', label: _LABEL_OCR, text: _LABEL_OCR,
      className: 'composer-menu-ocr',
      onTap: function () { el.ocrInput.click(); },
    },
  ]);
  host.appendChild(menu);
  const ocrItem = menu.querySelector('.composer-menu-ocr');

  // ---- send --------------------------------------------------------------
  function finishSend() {
    hasImage = false;
    el.textarea.value = '';
    el.textarea.style.height = '';
    el.textarea.focus();
  }

  function syncSend() {
    el.send.disabled = sendBlocked || sending;
  }

  function submit() {
    if (sendBlocked || sending) return;
    // #489: a dictation that just stopped is still finalizing until the
    // canonical transcript settles into the textarea. Reading + clearing the
    // buffer mid-window raced that settle — wait it out instead.
    if (dictation.isBusy()) {
      toast('Still transcribing — wait for the transcript, then tap Send', 'error', { icon: 'mic' });
      return;
    }
    const text = el.textarea.value;
    if (!text) return;
    let res;
    try {
      res = opts.send(text, { hasImage: hasImage });
    } catch (_) {
      return;
    }
    if (res && typeof res.then === 'function') {
      // Async delivery (Chat mode's /input route, #983): hold Send and
      // keep the draft until the surface says it went through.
      sending = true;
      syncSend();
      res.then(function (ok) {
        if (ok !== false && ok !== null && ok !== undefined) finishSend();
      }, function () { /* the surface toasted; the draft stays */ })
        .finally(function () { sending = false; syncSend(); });
      return;
    }
    // Sync delivery (the PTY WebSocket): clear inside the tap gesture so
    // the refocus keeps the phone keyboard up.
    if (res) finishSend();
  }
  el.send.addEventListener('click', submit);
  el.textarea.addEventListener('input', grow);

  // ---- availability --------------------------------------------------------
  function setAvailability(a) {
    if (a && 'dictate' in a) {
      setButtonState(el.mic, !!a.dictate, _TITLE_MIC, _TITLE_MIC_OFF);
    }
    if (a && 'ocr' in a) {
      ocrOn = !!a.ocr;
      ocrItem.hidden = !ocrOn;
      el.image.classList.toggle('has-options', ocrOn);
      el.image.title = ocrOn ? _TITLE_IMAGE_MENU : _TITLE_IMAGE;
      el.image.setAttribute('aria-label', el.image.title);
      el.image.setAttribute('aria-haspopup', ocrOn ? 'menu' : 'false');
      if (!ocrOn) imageMenu.close();
    }
  }

  function setSendable(enabled, reason) {
    sendBlocked = !enabled;
    el.send.title = enabled ? _TITLE_SEND : (reason || 'Sending is unavailable');
    el.send.setAttribute('aria-label', enabled ? 'Send' : el.send.title);
    syncSend();
  }

  function setPlaceholder(text) {
    el.textarea.placeholder = text;
    el.textarea.setAttribute('aria-label', text);
  }

  function setKeys(k) {
    keysOpts = k || null;
    setButtonState(el.keys, !!keysOpts, _TITLE_KEYS, _TITLE_KEYS_OFF);
    if (!keysOpts) keysPopover.close();
  }

  function closePopovers() {
    keysPopover.close();
    imageMenu.close();
  }

  // Leave-surface teardown: drop the draft, the staged screenshots and any
  // in-flight recording. dispose() (not stop()) so the mic and the app-wide
  // dictation mutex release even mid-permission-prompt or mid-finalize, and
  // a late transcript is dropped rather than written into a bar about to be
  // hidden and cleared (#755).
  function reset() {
    el.textarea.value = '';
    el.textarea.style.height = '';
    hasImage = false;
    clearOcrStaging();
    dictation.dispose();
    closePopovers();
  }

  setAvailability({ dictate: voiceDictationAvailable(), ocr: false });
  setKeys(keysOpts);
  setSendable(true);

  return {
    root: host,
    textarea: el.textarea,
    attachFiles: attachFiles,
    reset: reset,
    closePopovers: closePopovers,
    setAvailability: setAvailability,
    setKeys: setKeys,
    setPlaceholder: setPlaceholder,
    setSendable: setSendable,
  };
}
