// Unit pin for dictation's PCM capture pieces (shared/voice-pcm.js, voice-wav.js):
// the resampler turns a 48 kHz or 44.1 kHz microphone into 16 kHz samples of the
// right count and level, and the WAV writers produce a valid 16 kHz mono header,
// closed for a finished take and open-ended for a streamed one. Plain Node against
// the real ES modules. Invoked by tests/unit/static_ui/test_voice_pcm.py.

import assert from 'node:assert/strict';
import '../_browser_stubs.mjs';

const { createResampler } = await import('../../../app/webapp/static/shared/voice-pcm.js');
const { encodeWav, pcm16Bytes, wavHeader } = await import('../../../app/webapp/static/shared/voice-wav.js');

function resample(inRate, input, batch) {
  const rs = createResampler(inRate, 16000);
  const out = [];
  for (let i = 0; i < input.length; i += batch) out.push(...rs.push(input.slice(i, i + batch)));
  out.push(...rs.flush());
  return out;
}

for (const rate of [48000, 44100]) {
  const one = new Float32Array(rate).fill(0.5); // one second of a constant level
  const out = resample(rate, one, 1024);
  assert.ok(Math.abs(out.length - 16000) <= 1, `${rate} Hz: ${out.length} samples for one second`);
  assert.ok(out.every((v) => Math.abs(v - 0.5) < 1e-6), `${rate} Hz: the level changed`);
  const odd = resample(rate, one, 333); // odd-sized batches resample identically
  assert.equal(odd.length, out.length, `${rate} Hz: batching changed the length`);
}

// A tone above the new Nyquist (12 kHz at 48 kHz) is averaged away, not aliased loud.
const hiss = Float32Array.from({ length: 48000 }, (_, i) => Math.sin(2 * Math.PI * 12000 * i / 48000));
assert.ok(Math.max(...resample(48000, hiss, 2048).map(Math.abs)) < 0.5, 'a 12 kHz tone survived the downsample');
assert.deepEqual(resample(48000, new Float32Array(0), 1024), []);

// A finished take: a closed 44-byte header over little-endian 16-bit samples.
const samples = Float32Array.from([0, 0.5, -0.5, 1, -1, 2]);
const wav = Buffer.from(await encodeWav(samples, 16000).arrayBuffer());
assert.equal(wav.length, 44 + samples.length * 2);
assert.equal(wav.toString('ascii', 0, 4), 'RIFF');
assert.equal(wav.toString('ascii', 8, 16), 'WAVEfmt ');
assert.equal(wav.readUInt16LE(20), 1, 'PCM');
assert.equal(wav.readUInt16LE(22), 1, 'mono');
assert.equal(wav.readUInt32LE(24), 16000);
assert.equal(wav.readUInt16LE(34), 16, 'bits per sample');
assert.equal(wav.readUInt32LE(4), wav.length - 8);
assert.equal(wav.readUInt32LE(40), samples.length * 2);
assert.deepEqual([1, 2, 3, 4, 5].map((i) => wav.readInt16LE(44 + i * 2)), [16383, -16384, 32767, -32768, 32767]);

// A streamed take: the same header with both lengths open-ended.
const open = Buffer.from(wavHeader(16000, null));
assert.equal(open.length, 44);
assert.equal(open.readUInt32LE(4), 0xffffffff);
assert.equal(open.readUInt32LE(40), 0xffffffff);
assert.equal(open.readUInt32LE(24), 16000);
assert.equal(pcm16Bytes(samples).length, samples.length * 2);

console.log('voice-pcm: ok');
