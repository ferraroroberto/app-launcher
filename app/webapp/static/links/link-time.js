/* When a link was last mentioned, as a row's time label.
 *
 * A plain function of the timestamp and "now" so tests/js/link_time.test.mjs
 * pins it under Node: "just now", "12 min ago" and "3 h ago" for the first day,
 * then the day and time, which stay true as the sheet ages. '' for a stamp the
 * transcript did not carry.
 */

import { agoFromMinutes } from '../shared/format.js';

export function timeLabel(iso, now) {
  const t = Date.parse(iso || '');
  if (Number.isNaN(t)) return '';
  const min = Math.floor(((now === undefined ? Date.now() : now) - t) / 60000);
  if (min < 24 * 60) return agoFromMinutes(min);
  return new Date(t).toLocaleString([], {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

// Where the read stopped, for "Read back to Oct 31, 07:28 PM". '' for a stamp
// the transcript did not carry.
export function cutoffLabel(iso) {
  const t = Date.parse(iso || '');
  if (Number.isNaN(t)) return '';
  return new Date(t).toLocaleString([], {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}
