"""The Web Audio stand-in the read-aloud e2e tests inject (headless WebKit has
no real audio pipeline), so prepareHub()/speakHubInto() can run."""

# Stub the Web Audio API: record AudioContext lifecycle + every scheduled buffer
# so the test can assert PCM was decoded and scheduled, without a real audio
# pipeline (headless WebKit has none). Shadows both the standard and webkit
# constructors so the SPA's `window.AudioContext || window.webkitAudioContext`
# resolves to the fake.
AUDIO_MOCK = """
(() => {
  window.__audioLog = {
    contexts: 0, resumed: 0, buffers: 0, started: 0, closed: 0, samples: 0,
    startedAt: [], stopped: 0,
  };
  class FakeAudioBuffer {
    constructor(ch, len, sr) {
      this.length = len; this.sampleRate = sr;
      this.duration = len / Math.max(1, sr);
    }
    copyToChannel() {}
  }
  class FakeNode {
    constructor() { this.buffer = null; this.onended = null; }
    connect() {}
    start(t) {
      window.__audioLog.started += 1;
      window.__audioLog.startedAt.push(t);
      window.__lastNode = this;
    }
    stop() { window.__audioLog.stopped += 1; }
  }
  // scheduleHubBuffer() (issue #599) routes every node through a GainNode for
  // a click-avoiding fade; the fake just needs the shape (connect + a gain
  // param with the AudioParam automation methods it calls).
  class FakeGainNode {
    constructor() {
      this.gain = {
        setValueAtTime() {}, linearRampToValueAtTime() {},
      };
    }
    connect() {}
  }
  class FakeAudioContext {
    constructor() {
      this.currentTime = 0; this.destination = {};
      window.__audioLog.contexts += 1;
      window.__lastCtx = this;
    }
    resume() { window.__audioLog.resumed += 1; return Promise.resolve(); }
    createBuffer(ch, len, sr) {
      window.__audioLog.buffers += 1; window.__audioLog.samples += len;
      return new FakeAudioBuffer(ch, len, sr);
    }
    createBufferSource() { return new FakeNode(); }
    createGain() { return new FakeGainNode(); }
    close() { window.__audioLog.closed += 1; return Promise.resolve(); }
  }
  for (const name of ['AudioContext', 'webkitAudioContext']) {
    Object.defineProperty(window, name, {
      configurable: true, writable: true, value: FakeAudioContext,
    });
  }
  // Make document.visibilityState scriptable so a test can simulate a
  // screen-lock (hidden) → unlock (visible) cycle and drive visibilitychange.
  window.__setVisibility = (state) => {
    Object.defineProperty(document, 'visibilityState', {
      configurable: true, get: () => state,
    });
    document.dispatchEvent(new Event('visibilitychange'));
  };
})()
"""
