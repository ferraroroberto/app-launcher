/* Settings > Sessions > Window browser: Chrome, Edge or the Windows default for a session's PC
 * window. The value is GET /api/config's `mirror_browser`, so a choice made on the phone holds on
 * the desktop too. A tap marks the segment at once and saves; a failed save repaints the stored one.
 */
import { state } from '../state.js';
import { toast } from '../api.js';
import { patchConfig } from './claude-options.js';

const LABELS = { chrome: 'Chrome', edge: 'Edge', system: 'the default browser' };

function control() {
  return document.getElementById('mirrorBrowserControl');
}

function paint(choice) {
  const group = control();
  if (!group) return;
  group.querySelectorAll('[data-mirror-browser]').forEach(function (btn) {
    btn.setAttribute('aria-pressed', String(btn.dataset.mirrorBrowser === choice));
  });
}

export function renderMirrorBrowser(body) {
  paint(body && LABELS[body.mirror_browser] ? body.mirror_browser : 'chrome');
}

export function wireMirrorBrowser() {
  const group = control();
  if (!group) return;
  group.addEventListener('click', async function (ev) {
    const btn = ev.target.closest('[data-mirror-browser]');
    if (!btn || !group.contains(btn)) return;
    const choice = btn.dataset.mirrorBrowser;
    paint(choice);
    if (await patchConfig({ mirror_browser: choice })) {
      toast('Session windows open in ' + LABELS[choice] + '.', 'good');
    } else {
      renderMirrorBrowser(state.config);
    }
  });
}
