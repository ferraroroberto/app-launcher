/* Settings > Dictation > Auto-stop: Off, 2 s, 3 s or 5 s of quiet after
 * speech end a take by themselves (voice-autostop.js does the listening).
 *
 * The value is GET /api/config's `dictation_auto_stop_seconds`, so a choice
 * made on the phone holds on the desktop too. A tap marks the segment at once
 * and saves; patchConfig re-reads the config, which repaints from the
 * server's value, and a failed save repaints the stored one.
 */
import { state } from '../state.js';
import { toast } from '../api.js';
import { patchConfig } from './claude-options.js';

function control() {
  return document.getElementById('dictationAutoStopControl');
}

function paint(seconds) {
  const group = control();
  if (!group) return;
  group.querySelectorAll('[data-autostop]').forEach(function (btn) {
    btn.setAttribute('aria-pressed', String(Number(btn.dataset.autostop) === seconds));
  });
}

export function renderDictationAutoStop(body) {
  const seconds = body && typeof body.dictation_auto_stop_seconds === 'number'
    ? body.dictation_auto_stop_seconds : 0;
  paint(seconds);
}

export function wireDictationAutoStop() {
  const group = control();
  if (!group) return;
  group.addEventListener('click', async function (ev) {
    const btn = ev.target.closest('[data-autostop]');
    if (!btn || !group.contains(btn)) return;
    const seconds = Number(btn.dataset.autostop);
    paint(seconds);
    if (await patchConfig({ dictation_auto_stop_seconds: seconds })) {
      toast(seconds ? 'Dictation stops after ' + seconds + ' s of quiet.' : 'Dictation auto-stop off.', 'good');
    } else {
      renderDictationAutoStop(state.config);
    }
  });
}
