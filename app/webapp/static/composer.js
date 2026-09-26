/* The shared composer (#980, Step 1 of #979): the one input surface every
 * session view mounts — the terminal overlay, Chat mode (#983) and the Board
 * drawer (#984). Owns its own markup so a second mount is one
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
 *     keysOffReason:     optional — the disabled ⌨ button's title when the
 *                        default ("this session has no PTY") is not the
 *                        reason (the Board drawer, #984, has no socket).
 *     onDictationStart:  optional — fires when a recording starts (the
 *                        terminal silences an in-flight read-aloud, #190).
 *     sendOnModEnter:    optional — bind Ctrl+Enter / Cmd+Enter in the
 *                        textarea to Send (#1072). Off by default; only
 *                        Chat mode asks for it.
 *   }) → handle
 *
 * The handle: `root`, `textarea`, `attachFiles(files)` (the entry point the
 * image button, the composer's own paste and drop (#1206) and the terminal
 * host's paste and drop all use), `reset()` (leave-surface teardown), `closePopovers()`,
 * `setAvailability({ dictate, ocr })`, `setKeys(keysOpts | null, reason?)`,
 * `setPlaceholder(text)`, `setSendable(enabled, reason)`.
 *
 * `setSendable(false, reason)` gates ➤ Send alone (#983: a detached session
 * whose agent the console-input probe never proved) — the textarea, mic,
 * image and OCR stay usable. The gate is `aria-disabled`, never `disabled`:
 * a genuinely disabled button fires no tap event on iOS, so the reason could
 * only ever surface through a `title` hover a phone does not have, leaving a
 * dead button and no explanation (#1069). A tap still lands and toasts the
 * reason — the same convention as the Board drawer's actions and the
 * overlay's mode segments (#982).
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
import { bindLongPressHint } from './long-press-hint.js';
import { mountKeysPopover } from './terminal-keys.js';
import { icon } from './_vendored/icons/icons.js';

// Max visible rows before the textarea scrolls internally. Roomy enough
// for a long dictated voice note (#165) without the bar eating the whole
// screen when the keyboard is up. The CSS min-height floors it at 2 rows.
const _COMPOSE_MAX_ROWS = 8;

const _TITLE_MIC = 'Dictate (voice to text)';
const _TITLE_MIC_OFF = 'Dictation unavailable — voice-transcriber not configured';
const _TITLE_KEYS = 'Keyboard keys';
const _TITLE_KEYS_OFF = 'No terminal keys — this session has no PTY';
const _TITLE_IMAGE = 'Attach image or file';
const _TITLE_IMAGE_MENU = 'Attach image or file · Extract text from screenshots';
// The button's own title. The plain one is the honest default: on a phone,
// and on the surfaces that don't opt into `sendOnModEnter`, ➤ is the only way
// to deliver. The shortcut variant names the binding that actually exists
// (#1072) — it replaced 'Send (with Enter)', which promised a shortcut no
// code implemented and described the opposite of what the return key does.
const _TITLE_SEND = 'Send';
const _TITLE_SEND_MOD_ENTER = 'Send (Ctrl/Cmd+Enter)';
const _LABEL_ATTACH = 'Attach image or file';
const _LABEL_OCR = 'Extract text from screenshots';

// Auto-grow a composer textarea up to _COMPOSE_MAX_ROWS; the return key adds
// newlines on every surface, and ➤ Send delivers the text — plus Ctrl/Cmd+Enter
// on a surface that opted into `sendOnModEnter` (#1072).
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

function setButtonState(btn, enabled, titleOn, titleOff) {
  btn.disabled = !enabled;
  btn.title = enabled ? titleOn : titleOff;
  btn.setAttribute('aria-label', btn.title);
}

export function mountComposer(host, opts) {
  const el = render(host, opts.placeholder || 'Message for the agent');
  // The terminal-keys glyph isn't a conventional icon (#1238 J-05).
  bindLongPressHint(el.keys);
  let keysOpts = opts.keys || null;
  let ocrOn = false;
  // Fixed for the life of the mount: the surface opts into the shortcut at
  // mount time, so the title can't drift from the binding (#1072).
  const titleSend = opts.sendOnModEnter ? _TITLE_SEND_MOD_ENTER : _TITLE_SEND;
  // #450: the buffer carries an attached-file path, so the surface's send
  // may hold its submitting CR back for the path→attachment conversion.
  let hasImage = false;
  // #983: Send's own gate (setSendable) — kept apart from the in-flight
  // disable an async send holds, so settling a send never re-enables a
  // button the surface gated off.
  let sendBlocked = false;
  let sendReason = '';
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

  // Paste and drop reach attachFiles from every mount (#1206). They were only
  // wired to the terminal's xterm host (terminal-image.js), so Chat, which
  // has no terminal, took neither. A clipboard that also carries plain text
  // pastes as text: Office and similar apps put a rendered image of copied
  // text beside it, and that paste must never turn into an upload.
  el.textarea.addEventListener('paste', function (ev) {
    const files = pastedFiles(ev.clipboardData);
    if (!files.length) return;
    ev.preventDefault();
    attachFiles(files);
  });
  host.addEventListener('dragover', function (ev) {
    if (carriesFiles(ev.dataTransfer)) ev.preventDefault();
  });
  host.addEventListener('drop', function (ev) {
    const dt = ev.dataTransfer;
    if (!dt || !dt.files || !dt.files.length) return;
    ev.preventDefault();
    attachFiles(dt.files);
  });

  // The image button's two options. Registered BEFORE the menu binds its own
  // anchor handler, so with OCR unavailable this listener wins the click,
  // opens the picker directly and stops the (one-row) menu from opening.
  el.image.addEventListener('click', function (ev) {
    if (ocrOn) return;
    ev.stopImmediatePropagation();
    el.attachInput.click();
  });
  // Floated against the composer while open (#996): the Board drawer mounts
  // this composer inside the column carousel, and an absolutely-positioned
  // menu rising above the *top* card's drawer was clipped by that scroller.
  const imageMenu = createRowMenu('composer-menu', {
    placeAgainst: function () { return host; },
  });
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

  // The surface's gate is aria-disabled (see the module header, #1069) so a
  // phone tap still reaches the button and can say why. The in-flight hold
  // stays a real `disabled`: it lasts one request and has no reason to state.
  // It must also *look* held (#1239): `disabled` alone left ➤ at full accent,
  // so a send in progress read as a live button inviting a second tap.
  function syncSend() {
    el.send.disabled = sending;
    if (el.send.classList.contains('is-sending') !== sending) {
      el.send.classList.toggle('is-sending', sending);
      el.send.innerHTML = icon(sending ? 'hourglass' : 'send-horizontal');
    }
    if (sending) el.send.setAttribute('aria-busy', 'true');
    else el.send.removeAttribute('aria-busy');
    if (sendBlocked) el.send.setAttribute('aria-disabled', 'true');
    else el.send.removeAttribute('aria-disabled');
  }

  function submit() {
    if (sendBlocked) {
      toast(sendReason, '', { icon: 'send-horizontal' });
      return;
    }
    if (sending) return;
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

  // Ctrl+Enter / Cmd+Enter sends (#1072) — opt-in per surface, because only
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
  if (opts.sendOnModEnter) {
    el.textarea.addEventListener('keydown', function (ev) {
      if (ev.key !== 'Enter' || !(ev.ctrlKey || ev.metaKey)) return;
      ev.preventDefault();
      submit();
    });
  }

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
    sendReason = enabled ? '' : (reason || 'Sending is unavailable');
    el.send.title = enabled ? titleSend : sendReason;
    el.send.setAttribute('aria-label', enabled ? 'Send' : sendReason);
    syncSend();
  }

  function setPlaceholder(text) {
    el.textarea.placeholder = text;
    el.textarea.setAttribute('aria-label', text);
  }

  function setKeys(k, reason) {
    keysOpts = k || null;
    setButtonState(el.keys, !!keysOpts, _TITLE_KEYS, reason || _TITLE_KEYS_OFF);
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
  setKeys(keysOpts, opts.keysOffReason);
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
