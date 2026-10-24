/* Image lightbox: a full-screen overlay for any image the app already holds
 * as a URL — a transcript thumbnail's full size (#1265). The caller owns the
 * URL; tap anywhere (or ✕) to dismiss.
 *
 * HTML viewer: a sent HTML document's card preview and its full-size modal.
 * A document never renders same-origin or scriptable: it is shown only
 * through `srcdoc` in a frame whose sandbox attribute is empty (an opaque
 * origin, no script, popup, form, modal or top navigation), and its blob
 * stays application/octet-stream, so no same-origin text/html URL exists.
 */

import { els } from './state.js';
import { icon } from './_vendored/icons/icons.js';

export function openImageLightbox(url, alt) {
  if (!url || !els.imageLightbox) return;
  els.imageLightboxImage.src = url;
  els.imageLightboxImage.alt = alt;
  els.imageLightbox.hidden = false;
}

function closeLightbox() {
  if (!els.imageLightbox) return;
  els.imageLightbox.hidden = true;
  els.imageLightboxImage.removeAttribute('src');
}

export function wireImageLightbox() {
  if (els.imageLightbox) {
    els.imageLightbox.addEventListener('click', closeLightbox);
  }
  if (els.imageLightboxClose) {
    els.imageLightboxClose.addEventListener('click', function (ev) {
      ev.stopPropagation();
      closeLightbox();
    });
  }
}

// Above this a card shows a plain tile instead of a live preview, so a phone
// never parses a large document twice; the modal still renders it.
export const HTML_PREVIEW_MAX_BYTES = 5 * 1024 * 1024;

// The server's policy for an HTML file (session_outbox.HTML_CSP) without
// `sandbox`, which a <meta> cannot carry: the frame's empty sandbox attribute
// stands in for it. A srcdoc gets no response headers, so without this a
// document could still load a remote image and tell someone it was opened.
const DOC_CSP = "default-src 'none'; img-src data: blob:; style-src 'unsafe-inline'; font-src data:";

// The document with the policy as its first element, after any doctype so
// it keeps standards mode.
function withCsp(html) {
  const lead = /^﻿?\s*(<!doctype[^>]*>)?/i.exec(html)[0];
  return lead + '<meta http-equiv="Content-Security-Policy" content="' + DOC_CSP + '">' + html.slice(lead.length);
}

function sandboxedFrame(className, title) {
  const frame = document.createElement('iframe');
  frame.className = className;
  frame.setAttribute('sandbox', '');
  frame.title = title;
  return frame;
}

// The card's body once its bytes are in: a scaled-down first screen of the
// document (a plain tile past HTML_PREVIEW_MAX_BYTES) under one button that
// opens the modal, as the head's Open does. `got` is the card's
// {blob, objectUrl}; the blob is octet-stream, so the URL only downloads.
export async function fillHtmlCard(card, got, name) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'tr-file-html-open';
  btn.setAttribute('aria-label', 'Open ' + name);
  const show = function (ev) {
    ev.stopPropagation();
    openHtmlViewer(name, got, btn);
  };
  btn.addEventListener('click', show);
  // Wired before the await: the head's Open is already showing.
  card.querySelector('.tr-file-open').addEventListener('click', show);
  const box = document.createElement('div');
  box.className = 'tr-file-html';
  if (got.blob.size > HTML_PREVIEW_MAX_BYTES) {
    box.classList.add('tr-file-html-tile');
    box.innerHTML = icon('code-xml') + '<span>HTML document</span>';
  } else {
    const text = await got.blob.text();
    if (!card.isConnected) return;
    const frame = sandboxedFrame('tr-file-html-frame', name);
    frame.tabIndex = -1;
    frame.setAttribute('aria-hidden', 'true');
    frame.srcdoc = withCsp(text);
    box.appendChild(frame);
  }
  box.appendChild(btn);
  const note = document.createElement('div');
  note.className = 'tr-file-note';
  note.textContent = 'Shown without scripts';
  card.append(box, note);
  card.classList.add('tr-file-loaded');
}

// The element focus goes back to on close (Safari does not restore it from a
// showModal() on its own), and whether the back-gesture marker is ours.
let viewerReturn = null;
let viewerPushed = false;

function openHtmlViewer(name, got, returnTo) {
  const dlg = els.htmlViewer;
  if (!dlg || dlg.open) return;
  viewerReturn = returnTo;
  els.htmlViewerName.textContent = name;
  els.htmlViewerDownload.href = got.objectUrl;
  els.htmlViewerDownload.download = name;
  dlg.showModal();
  // The phone's back gesture closes the viewer instead of leaving the app.
  history.pushState({ htmlViewer: true }, '');
  viewerPushed = true;
  // A new frame carrying its srcdoc before it is inserted: re-pointing one
  // frame would add a joint-history entry, which the back gesture would then
  // step through instead of closing the viewer.
  got.blob.text().then(function (text) {
    if (!dlg.open || viewerReturn !== returnTo) return;
    const frame = sandboxedFrame('html-viewer-frame', name);
    frame.srcdoc = withCsp(text);
    els.htmlViewerBody.replaceChildren(frame);
  });
}

function onViewerClosed() {
  els.htmlViewerBody.replaceChildren();
  els.htmlViewerDownload.removeAttribute('href');
  if (viewerPushed) {
    viewerPushed = false;
    if (history.state && history.state.htmlViewer) history.back();
  }
  if (viewerReturn && viewerReturn.isConnected) viewerReturn.focus();
  viewerReturn = null;
}

export function wireHtmlViewer() {
  const dlg = els.htmlViewer;
  if (!dlg) return;
  dlg.addEventListener('close', onViewerClosed);
  // A click on the dialog element itself is the backdrop: the card covers
  // the rest of it.
  dlg.addEventListener('click', function (ev) {
    if (ev.target === dlg) dlg.close();
  });
  els.htmlViewerClose.addEventListener('click', function () { dlg.close(); });
  window.addEventListener('popstate', function () {
    if (!viewerPushed) return;
    viewerPushed = false;
    if (dlg.open) dlg.close();
  });
}
