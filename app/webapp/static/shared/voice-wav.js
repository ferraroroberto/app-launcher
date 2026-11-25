/* 16 kHz mono 16-bit PCM WAV, the audio shape the speech backend takes without
 * converting it. voice-pcm.js records the samples straight from the microphone
 * and writes them out here, so no recorder container (iPhone Safari's
 * fragmented MP4 above all) is ever decoded and the PC needs no ffmpeg.
 */

export const WAV_HEADER_BYTES = 44;
// The RIFF and data lengths of a WAV whose end is not known yet: a decoder reads
// to the end of the file, which is what a streamed take needs.
const OPEN_ENDED = 0xffffffff;

// The 44-byte RIFF header for `dataBytes` of 16-bit mono samples, or an
// open-ended one when `dataBytes` is null.
export function wavHeader(rate, dataBytes) {
  const view = new DataView(new ArrayBuffer(WAV_HEADER_BYTES));
  const text = function (at, s) {
    for (let i = 0; i < s.length; i++) view.setUint8(at + i, s.charCodeAt(i));
  };
  text(0, 'RIFF');
  view.setUint32(4, dataBytes === null ? OPEN_ENDED : 36 + dataBytes, true);
  text(8, 'WAVE');
  text(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true); // bytes per second
  view.setUint16(32, 2, true); // bytes per frame
  view.setUint16(34, 16, true); // bits per sample
  text(36, 'data');
  view.setUint32(40, dataBytes === null ? OPEN_ENDED : dataBytes, true);
  return new Uint8Array(view.buffer);
}

// Float samples in -1..1 as little-endian 16-bit PCM bytes.
export function pcm16Bytes(samples) {
  const view = new DataView(new ArrayBuffer(samples.length * 2));
  for (let i = 0; i < samples.length; i++) {
    const v = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(i * 2, v < 0 ? v * 0x8000 : v * 0x7fff, true);
  }
  return new Uint8Array(view.buffer);
}

// The finished take: a header and every sample, as one WAV Blob.
export function encodeWav(samples, rate) {
  const body = pcm16Bytes(samples);
  return new Blob([wavHeader(rate, body.length), body], { type: 'audio/wav' });
}
