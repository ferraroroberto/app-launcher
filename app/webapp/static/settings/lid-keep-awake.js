/* Settings > Awake with lid closed.
 *
 * The state is GET /api/config's live `lid_keep_awake` block (powercfg, read
 * on every fetch), so the switch never claims a position it cannot back: an
 * unreadable state paints as `mixed` with its reason, never as off, and a PC
 * with no lid hides the row. A tap changes the power plan; a refusal shows
 * Windows' plain reason beside the switch and the config is re-read, so the
 * switch shows what powercfg now says rather than an optimistic flip.
 */
import { els, state } from '../state.js';
import { apiFailToast, jsonApi, toast } from '../api.js';
import { setSwitch } from '../_vendored/switch/switch.js';

function lidOf(body) {
  return (body && body.lid_keep_awake) || null;
}

function showStatus(text) {
  const status = els.lidKeepAwakeStatus;
  if (!status) return;
  status.textContent = text || '';
  status.hidden = !text;
}

export function renderLidKeepAwake(body) {
  const row = els.lidKeepAwakeRow;
  const btn = els.lidKeepAwakeToggle;
  if (!row || !btn) return;
  const lid = lidOf(body);
  const visible = !!lid && lid.state !== 'unsupported';
  row.hidden = !visible;
  if (els.lidKeepAwakeHelp) els.lidKeepAwakeHelp.hidden = !visible;
  if (!visible) {
    showStatus('');
    return;
  }
  const known = lid.state === 'on' || lid.state === 'off';
  setSwitch(btn, lid.state === 'on');
  if (!known) {
    btn.setAttribute('aria-checked', 'mixed');
    btn.querySelector('.toggle-label').textContent = 'UNKNOWN';
    showStatus((lid.reason || 'The lid close setting could not be read.') + ' It is shown as unknown.');
  } else {
    showStatus('');
  }
}

async function toggle(refresh) {
  const lid = lidOf(state.config);
  const next = !(lid && lid.state === 'on');
  try {
    await jsonApi('/api/settings/lid-keep-awake', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: next }),
    });
    await refresh();
    toast(next ? 'The PC stays awake with the lid closed.' : 'Closing the lid works as before.', 'good');
  } catch (exc) {
    const detail = exc && exc.body && exc.body.detail;
    await refresh();
    showStatus(typeof detail === 'string' ? detail : 'The lid close setting could not be changed.');
    apiFailToast('Awake with lid closed failed', exc);
  }
}

export function wireLidKeepAwake(refresh) {
  if (els.lidKeepAwakeToggle) {
    els.lidKeepAwakeToggle.addEventListener('click', function () { toggle(refresh); });
  }
}
