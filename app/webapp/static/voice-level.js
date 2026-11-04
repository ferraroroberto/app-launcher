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

const _SILENT_DEFAULT = 'No sound from the microphone — check Bluetooth/AirPods';
const _SILENT_REMOTE =
  'No sound from the microphone — check the Remote Desktop audio recording setting ' +
  "('Record from this computer')";

// Windows names the redirected Remote Desktop input "Remote Audio"; a flat
// strip there is the RDP client's recording setting, not a Bluetooth headset.
export function silentText(label) {
  return /remote audio/i.test(label || '') ? _SILENT_REMOTE : _SILENT_DEFAULT;
}
