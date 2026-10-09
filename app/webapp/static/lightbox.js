/* Image lightbox: a full-screen overlay for any image the app already holds
 * as a URL — a transcript thumbnail's full size (#1265). The caller owns the
 * URL; tap anywhere (or ✕) to dismiss.
 */

import { els } from './state.js';

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
