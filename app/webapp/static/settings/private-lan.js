/* Settings > Terminal: the two private-network switches.
 *
 * "Allow the private network" lets a phone on a trusted home or office
 * network reach the terminal-grade routes with no Tailscale; the screen view
 * (the whole desktop) has its own switch below it that only works while the
 * first is on. Both save at once, off by default, and turning either ON asks
 * first: the server re-checks who is asking, so a refusal comes back as a
 * failed save and the switch returns to off.
 */
import { els } from '../state.js';
import { setSwitch } from '../_vendored/switch/switch.js';
import { confirmDialog } from '../confirm-dialog.js';

const WARNING = {
  terminal_allow_private_lan: {
    title: 'Allow the private network?',
    message:
      'Every device on a private network that can reach this PC will be trusted on the ' +
      'terminal routes: transcripts, the terminal, input and uploads. The token and ' +
      'passkey still apply, but anything else on that network can reach the sign-in. ' +
      'The Tailscale allowlist is the narrower choice.',
  },
  screen_allow_private_lan: {
    title: 'Allow the screen view on the private network?',
    message:
      'The screen view shows this PC’s whole desktop, terminals included. Any device on ' +
      'a private network that passes the token and passkey will be able to watch it.',
  },
};

export function renderPrivateLan(body) {
  const terminalOn = body.terminal_allow_private_lan === true;
  setSwitch(els.terminalPrivateLanToggle, terminalOn);
  setSwitch(els.screenPrivateLanToggle, terminalOn && body.screen_allow_private_lan === true);
  // The screen switch cannot be armed behind a closed gate; it can still be
  // turned off, so it only locks while it is off.
  if (els.screenPrivateLanToggle) {
    els.screenPrivateLanToggle.disabled =
      !terminalOn && els.screenPrivateLanToggle.getAttribute('aria-checked') !== 'true';
  }
}

function wireOne(el, key, patchConfig) {
  if (!el) return;
  el.addEventListener('click', async function () {
    if (el.disabled) return;
    const next = el.getAttribute('aria-checked') !== 'true';
    if (next && !(await confirmDialog({ ...WARNING[key], action: 'Turn on', tone: 'danger' }))) {
      return;
    }
    setSwitch(el, next);
    if (!(await patchConfig({ [key]: next }))) setSwitch(el, !next);
  });
}

export function wirePrivateLan(patchConfig) {
  wireOne(els.terminalPrivateLanToggle, 'terminal_allow_private_lan', patchConfig);
  wireOne(els.screenPrivateLanToggle, 'screen_allow_private_lan', patchConfig);
}
