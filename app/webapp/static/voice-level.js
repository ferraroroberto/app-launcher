/* The recorder's input name for a dictation take.
 *
 * The live level and the silent-input verdict come from the vendored
 * rec-wave strip (`voice.js` mounts it); this file only names the input. */

// The name the browser gives the live input ("AirPods", "iPhone Microphone"),
// or '' when it withholds the label.
export function inputLabel(stream) {
  try {
    const tracks = stream && stream.getAudioTracks ? stream.getAudioTracks() : [];
    return (tracks[0] && tracks[0].label) || '';
  } catch (_) {
    return '';
  }
}
