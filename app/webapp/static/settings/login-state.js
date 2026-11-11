/* The X login window's state, as Settings > Inspiration reads it.
 *
 * The server declares the closed set once, in src/inspiration/login.py
 * (LoginState); GET and POST /api/inspiration/login-x carry it in `state`.
 * This module is the browser's copy, so the sheet asks loginState() /
 * isOpen() instead of re-spelling the strings.
 * tests/unit/core/test_inspiration_login_state.py fails when the set drifts
 * from the server's.
 */

export const LoginState = Object.freeze({
  IDLE: 'idle',
  RUNNING: 'running',
  CLOSED: 'closed',
  FAILED: 'failed',
});

const KNOWN = new Set(Object.values(LoginState));

// The LoginState member a login status is in, or null for anything outside the set.
export function loginState(status) {
  return status && KNOWN.has(status.state) ? status.state : null;
}

// True while the login window is open: the button stays disabled and the sheet keeps polling.
export function isOpen(status) {
  return loginState(status) === LoginState.RUNNING;
}
