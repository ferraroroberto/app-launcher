/* Screen view: when to reconnect, and which monitor to reopen (#1603).
 *
 * The decisions as plain functions, so the backoff steps and the monitor
 * choice are pinned under plain Node (tests/js/screen_reconnect.test.mjs)
 * without a browser or a clock. screen-view.js owns the timers and the socket.
 */

// The wait before each automatic reconnect attempt after a drop, in order:
// 1 s, 2 s, 4 s, 8 s, then 15 s. The list is the whole budget: once every
// step has been tried (about a minute of waiting) the view gives up into the
// Retry card, so a dead network does not loop forever. The count resets when
// a frame is drawn again.
export const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000, 15000, 15000, 15000];

// The wait before attempt number `attempt` (0 is the first retry), or null
// once the budget is spent.
export function reconnectDelay(attempt) {
  return attempt >= 0 && attempt < RECONNECT_DELAYS_MS.length
    ? RECONNECT_DELAYS_MS[attempt]
    : null;
}

// The last monitor picked on this device (per-device, like the Control style).
export const MONITOR_KEY = 'launcher.screenMonitor';

// What is stored for a pick: the id and where the monitor sits on the
// virtual desktop. An id is only its position in the enumeration order, so
// the rectangle is what recognises it after the layout changed.
export function monitorChoice(monitor) {
  return {
    id: monitor.id, left: monitor.left, top: monitor.top,
    width: monitor.width, height: monitor.height,
  };
}

// A stored value back into a choice, or null when it is missing or not one.
export function parseMonitorChoice(raw) {
  if (!raw) return null;
  let value = null;
  try { value = JSON.parse(raw); } catch (_) { return null; }
  if (!value || typeof value !== 'object') return null;
  const keys = ['id', 'left', 'top', 'width', 'height'];
  for (const key of keys) {
    if (!Number.isFinite(value[key])) return null;
  }
  return monitorChoice(value);
}

// The monitor to open: the one at the stored rectangle, else the one with the
// stored id, else the primary (or the first). Never null for a non-empty list.
export function chooseMonitor(monitors, stored) {
  if (!monitors.length) return null;
  if (stored) {
    const byRect = monitors.find(function (m) {
      return m.left === stored.left && m.top === stored.top &&
        m.width === stored.width && m.height === stored.height;
    });
    if (byRect) return byRect;
    const byId = monitors.find(function (m) { return m.id === stored.id; });
    if (byId) return byId;
  }
  return monitors.find(function (m) { return m.primary; }) || monitors[0];
}
