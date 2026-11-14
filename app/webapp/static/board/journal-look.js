/* Stamp "Roberto just looked" for the Board's journal card.
 *
 * The chat send, the drawer's reply box and the answer sheet each call
 * markLooked() after a message reached the chief. It imports only the API
 * layer, so those callers take no dependency on the card itself; the card
 * registers what to do once the stamp landed (re-read the journal).
 */

import { terminalJsonApi } from '../webauthn.js';

let afterLook = function () {};

export function onLooked(fn) { afterLook = fn; }

// A failed stamp changes nothing and says nothing: the card then keeps
// counting the entries it did before. Never throws, so a caller's send
// path is never failed by it.
export async function markLooked() {
  try {
    await terminalJsonApi('/api/board/chief/journal/looked', { method: 'POST' });
  } catch (_exc) {
    return;
  }
  await afterLook();
}
