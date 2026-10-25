/* Screen view: quality presets and the live readout.
 *
 * The preset table the picker shows, the choice remembered per device, and the
 * readout's arithmetic, as plain functions and one small class, so the numbers
 * are pinned under plain Node (tests/js/screen_quality.test.mjs) without a
 * browser or a clock. The server owns what each preset means (quality, pace,
 * width cap: src/desktop/capture.py PRESETS); the ids here are the values of
 * the connect query's `preset=`. screen-view.js owns the socket and the DOM.
 */

// The last preset picked on this device (per-device, like the monitor and the
// Control style).
export const PRESET_KEY = 'launcher.screenPreset';
export const DEFAULT_PRESET = 'balanced';

export const PRESETS = [
  { id: 'sharp', label: 'Sharp', note: 'Best picture: JPEG quality 85, about 7 frames a second.' },
  { id: DEFAULT_PRESET, label: 'Balanced', note: 'The default: quality 70, about 7 frames a second.' },
  { id: 'saver', label: 'Data saver', note: 'Smallest pictures: at most 960 px wide, quality 50, about 3 frames a second.' },
];

// A stored value back into a preset id; anything unknown is Balanced, as the
// server treats an unknown query value.
export function presetChoice(raw) {
  const id = typeof raw === 'string' ? raw.trim().toLowerCase() : '';
  return PRESETS.some(function (p) { return p.id === id; }) ? id : DEFAULT_PRESET;
}

export function presetNote(id) {
  const preset = PRESETS.find(function (p) { return p.id === id; });
  return preset ? preset.note : '';
}

// The readout looks at the last few seconds of frames: long enough to even
// out the gaps between frames, short enough to follow a change of link.
export const READOUT_WINDOW_MS = 5000;
// A window younger than this is measured over this long, so the first frame
// after switching on does not read as an enormous rate.
export const READOUT_MIN_SPAN_MS = 1000;

/* What the client sees of the stream, from the frames it receives.
 *
 *   frame(bytes, now)  a frame arrived (its size in bytes)
 *   acked(now)         the client told the server that frame was drawn
 *   snapshot(now)      {frames, fps, kBps, avgKB, ackMs} over the window
 *
 * kB is 1000 bytes. kBps is bytes received in the window divided by its span,
 * which is exactly kB per second (1 byte/ms = 1 kB/s). ackMs is the mean time
 * from sending an ack to the next frame arriving: the server's pause between
 * frames, its capture and encode, and the link, in one number; null when no
 * frame arrived in the window (a still screen sends none).
 */
export class Readout {
  constructor(now) {
    this.since = now;
    this.frames = [];     // {at, bytes}
    this.gaps = [];       // {at, ms}
    this.ackAt = null;
  }

  frame(bytes, now) {
    this.frames.push({ at: now, bytes: bytes });
    if (this.ackAt !== null) {
      this.gaps.push({ at: now, ms: now - this.ackAt });
      this.ackAt = null;
    }
    this._trim(now);
  }

  acked(now) {
    this.ackAt = now;
  }

  _trim(now) {
    const cutoff = now - READOUT_WINDOW_MS;
    while (this.frames.length && this.frames[0].at <= cutoff) this.frames.shift();
    while (this.gaps.length && this.gaps[0].at <= cutoff) this.gaps.shift();
  }

  snapshot(now) {
    this._trim(now);
    const span = Math.min(READOUT_WINDOW_MS, Math.max(READOUT_MIN_SPAN_MS, now - this.since));
    const count = this.frames.length;
    let bytes = 0;
    for (const f of this.frames) bytes += f.bytes;
    let gapSum = 0;
    for (const g of this.gaps) gapSum += g.ms;
    return {
      frames: count,
      fps: count * 1000 / span,
      kBps: bytes / span,
      avgKB: count ? bytes / count / 1000 : 0,
      ackMs: this.gaps.length ? gapSum / this.gaps.length : null,
    };
  }
}

// The overlay's lines. `server` is the last `stats` message (mean ms per
// capture stage, null for a stage that did not run), or null before the first.
export function readoutLines(snap, server) {
  const first = snap.fps.toFixed(1) + ' fps · ' + Math.round(snap.kBps) + ' kB/s';
  const second = snap.frames
    ? 'avg ' + snap.avgKB.toFixed(1) + ' kB · ack to frame '
      + (snap.ackMs === null ? '–' : Math.round(snap.ackMs) + ' ms')
    : 'no frames (a still screen sends none)';
  if (!server) return [first, second, 'PC times: waiting…'];
  const stage = function (name) {
    const ms = server[name];
    return name + ' ' + (ms === null || ms === undefined ? '–' : ms);
  };
  return [
    first,
    second,
    'PC ' + stage('grab') + ' · ' + stage('digest') + ' ms',
    'PC ' + stage('resize') + ' · ' + stage('encode') + ' ms',
  ];
}
