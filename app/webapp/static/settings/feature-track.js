/* Feature-use events for Screen time's "By feature" block.
 *
 * The vendored usage log records a tap only for a click on a `data-track`
 * element, and it has no call to record one directly. A feature is counted when
 * it succeeded (a transcript landed, an upload finished), which is no click, so
 * trackFeature() clicks one hidden probe element carrying the id. The log's
 * delegated listener reads the attribute synchronously, so one probe serves
 * every call.
 *
 * Privacy contract: an id from FEATURE_IDS and nothing else. No text, file name
 * or transcript ever reaches the attribute; an id outside the list is dropped.
 * src/usage/feature_counts.py holds the same list and a test pins them equal.
 */

export const FEATURE_IDS = [
  'feat.send.typed',
  'feat.send.dictated',
  'feat.dictation',
  'feat.attach.image',
  'feat.attach.file',
  'feat.attach.large',
  'feat.ocr',
  'feat.paste-pc',
];

let probe = null;

export function trackFeature(id) {
  try {
    if (FEATURE_IDS.indexOf(id) === -1) return;
    if (!probe) {
      probe = document.createElement('span');
      probe.hidden = true;
      document.body.appendChild(probe);
    }
    probe.setAttribute('data-track', id);
    probe.click();
    probe.removeAttribute('data-track');
  } catch (_) { /* instrumentation never breaks the app */ }
}
