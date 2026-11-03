/* Feature-use events for Screen time's "By feature" block.
 *
 * A feature is counted when it succeeded (a transcript landed, an upload
 * finished), which is no click, so trackFeature() hands the id to the usage
 * log's direct `track` call.
 *
 * Privacy contract: an id from FEATURE_IDS and nothing else. No text, file name
 * or transcript ever reaches the log; an id outside the list is dropped.
 * src/usage/feature_counts.py holds the same list and a test pins them equal.
 */

import { usage } from '../usage.js';

export const FEATURE_IDS = [
  'feat.send.typed',
  'feat.send.dictated',
  'feat.dictation',
  'feat.attach.image',
  'feat.attach.file',
  'feat.attach.large',
  'feat.ocr',
  'feat.paste-pc',
  'feat.attach.board',
];

export function trackFeature(id) {
  try {
    if (FEATURE_IDS.indexOf(id) === -1) return;
    // dynamic-id-exempt: the id was just checked against the closed FEATURE_IDS list
    usage.track(id);
  } catch (_) { /* instrumentation never breaks the app */ }
}
