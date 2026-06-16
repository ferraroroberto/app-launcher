/* Fleet system map (issue #173): a foldable Coding-tab section that shows
 * the claude-config architecture PNG, with tap-to-zoom full-screen.
 *
 * The section hides unless /api/system-map/status reports the rendered PNG
 * exists. The image is lazy-loaded the first time the panel is expanded —
 * fetched through api() (so the bearer token rides along; no token leaks
 * into an <img src>) and shown via an object URL. The image endpoint is
 * Tailscale-only server-side, so a fetch may 403 over the Cloudflare tunnel;
 * we surface the reason rather than a broken image.
 */

import { els, state } from './state.js';
import { api } from './api.js';

// --------------------------------------------------------- availability
export async function fetchSystemMapStatus() {
  try {
    const res = await api('/api/system-map/status');
    if (!res.ok) throw new Error('status ' + res.status);
    const body = await res.json();
    state.systemMapAvailable = !!body.available;
  } catch (_) {
    state.systemMapAvailable = false;
  }
  if (els.systemMapCard) els.systemMapCard.hidden = !state.systemMapAvailable;
}

// --------------------------------------------------------- image loading
function setStatus(msg) {
  if (!els.systemMapStatus) return;
  els.systemMapStatus.textContent = msg || '';
  els.systemMapStatus.hidden = !msg;
}

function revokeObjectUrl() {
  if (state.systemMapObjectUrl) {
    URL.revokeObjectURL(state.systemMapObjectUrl);
    state.systemMapObjectUrl = null;
  }
}

async function loadImage() {
  setStatus('Loading…');
  els.systemMapImage.hidden = true;
  try {
    const res = await api('/api/system-map/image');
    if (res.status === 403) {
      setStatus('The system map is Tailscale-only — open the launcher over your Tailscale URL to view it.');
      return;
    }
    if (res.status === 404) {
      setStatus('System map not found — run /system-map in claude-config, or point Claude-config dir in Settings at a claude-config checkout.');
      return;
    }
    if (!res.ok) throw new Error('status ' + res.status);
    const blob = await res.blob();
    revokeObjectUrl();
    state.systemMapObjectUrl = URL.createObjectURL(blob);
    els.systemMapImage.src = state.systemMapObjectUrl;
    els.systemMapImage.hidden = false;
    setStatus('');
  } catch (_) {
    setStatus('Could not load the system map.');
  }
}

// --------------------------------------------------------- lightbox
function openLightbox() {
  if (!state.systemMapObjectUrl || !els.systemMapLightbox) return;
  els.systemMapLightboxImage.src = state.systemMapObjectUrl;
  els.systemMapLightbox.hidden = false;
}

function closeLightbox() {
  if (!els.systemMapLightbox) return;
  els.systemMapLightbox.hidden = true;
  els.systemMapLightboxImage.removeAttribute('src');
}

// --------------------------------------------------------- wiring
export function wireSystemMap() {
  if (!els.systemMapCard) return;

  // Lazy-load on first expand; reload via the 🔄 button.
  els.systemMapCard.addEventListener('toggle', function () {
    if (els.systemMapCard.open && !state.systemMapObjectUrl) {
      loadImage();
    }
  });

  if (els.systemMapRefresh) {
    els.systemMapRefresh.addEventListener('click', function (ev) {
      // Inside <summary>: stop the click from also toggling the panel.
      ev.preventDefault();
      ev.stopPropagation();
      revokeObjectUrl();
      els.systemMapCard.open = true;
      loadImage();
    });
  }

  if (els.systemMapImage) {
    els.systemMapImage.addEventListener('click', openLightbox);
  }
  if (els.systemMapLightbox) {
    els.systemMapLightbox.addEventListener('click', closeLightbox);
  }
  if (els.systemMapLightboxClose) {
    els.systemMapLightboxClose.addEventListener('click', function (ev) {
      ev.stopPropagation();
      closeLightbox();
    });
  }
}
