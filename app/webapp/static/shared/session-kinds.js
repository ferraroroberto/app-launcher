/* What kind of session a row is (chief, channel, relay, job run) and the
   confirm wording for stopping one. */

// The standing fleet chief (cross-tab parity): one label="chief"
// PTY session. Same label-first, name-fallback match everywhere a session
// might be the chief — a session-host that predates the label field still
// reports the chief's launch name, so the fallback keeps crown/tint/confirm
// working across that host's last pre-label run. Single source of truth for
// board.js (card rendering), sessions.js (Coding tab rows + stop guard), and
// terminal.js (overlay title) — do not re-derive this check independently.
export function isChiefSession(s) {
  return !!s && (s.label === 'chief' || (s.kind === 'pty' && s.name === 'chief'));
}
// The themed confirm's wording (confirmDialog, never a native
// confirm()). Same words as the native dialogs they replaced.
export const CHIEF_KILL_CONFIRM = { title: 'Kill the chief session?', action: 'Kill' };

// A Telegram channel session: the session-host label is
// `telegram:<profile id>`, set only by the Life OS channel launch. Its
// "Telegram · Health" name is derived from the id, so the Board and the Coding
// tab need nothing but the label the host already echoes.
const CHANNEL_LABEL_PREFIX = 'telegram:';
export function isChannelSession(s) {
  return !!s && typeof s.label === 'string' && s.label.indexOf(CHANNEL_LABEL_PREFIX) === 0;
}
// A Life OS conversation the Telegram relay opened for a bot: label
// `relay:<profile id>`. The label itself is the chip, so a Board card reads as
// the bot's rather than the owner's own interactive session.
export function isRelaySession(s) {
  return !!s && typeof s.label === 'string' && s.label.indexOf('relay:') === 0;
}
// A scheduled job's own claude session: the session-state hook flags it when
// the job runner's env is present, and the Jobs card already tracks the run.
// Strict `true`, so a card without the field is an ordinary session.
export function isJobRunSession(s) {
  return !!s && s.job_run === true;
}
// A managed session has its conversation somewhere else (a Telegram chat, the
// Board chat bar) or is a job runner's own orchestrator, so it never counts as
// work waiting on the owner. Adding a managed kind is one more clause here.
export function isManagedSession(s) {
  return isChannelSession(s) || isChiefSession(s) || isJobRunSession(s);
}
export function channelSessionName(s) {
  if (!isChannelSession(s)) return '';
  const id = s.label.slice(CHANNEL_LABEL_PREFIX.length);
  return 'Telegram · ' + id.replace(/-/g, ' ').replace(/\b\w/g, function (c) { return c.toUpperCase(); });
}
export function channelKillConfirm(s) {
  return {
    title: 'Stop the ' + channelSessionName(s) + ' session?',
    message: 'Its bot goes offline until you start it again.',
    action: 'Stop',
  };
}
export const CHIEF_RESTART_CONFIRM = {
  title: 'Restart the chief?',
  message: 'It will stop the current one gracefully and resume the same ' +
    'conversation (falling back to a fresh one only if nothing is resumable).',
  action: 'Restart',
};
