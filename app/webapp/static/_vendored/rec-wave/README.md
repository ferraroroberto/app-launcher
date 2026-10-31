# `rec-wave` — the live recording waveform

The level strip every record button shows while the mic is open: bars that follow the input level plus an elapsed `m:ss`, so a take confirms at a glance that speech is being captured. It is the "timer and level meter" of the design spec's **capture control**. Normalized from grocery-shopping-automation's recording strip (`startWave()`), generalized from a canvas in one fixed element to DOM bars in any host.

## Files

| File | Role |
| --- | --- |
| `rec-wave.css` | Visual contract — bars, reduced-motion meter, silent and unmeasured states. References design tokens only. |
| `rec-wave.js` | `attachRecWave(host, stream, opts)` → `stop()`, and `createLevelSource(stream?)`, the level reader it uses. |

## How to vendor

1. Copy this `rec-wave/` folder **verbatim** into your app's static dir (`app/webapp/static/_vendored/rec-wave/`). Do **not** edit either file per-app.
2. Link the CSS and import the functions:
   ```html
   <link rel="stylesheet" href="/static/_vendored/rec-wave/rec-wave.css">
   ```
   ```js
   import { attachRecWave } from '/static/_vendored/rec-wave/rec-wave.js';
   ```

## Usage

```js
// In the tap handler that starts recording, with the stream the recorder already has.
const stopWave = attachRecWave(glyphSpan, stream);
// ...when recording ends (always, including on error):
stopWave();
```

- **The host is the element holding the record glyph**, usually a `<span>` inside the button. While the wave is on, every other child of the host is hidden and the wave takes its place; `stop()` puts them back. Keep the button's label outside the host so the button still names its action ("Stop"). The wave is `aria-hidden`; the button's own label and your silent-mic notice carry the meaning.
- **No second permission prompt.** The wave reads the stream you already have. It never calls `getUserMedia`, and `stop()` never stops the stream's tracks: the recorder owns them.
- **`stop()` releases.** It clears the timers, removes the wave and closes the `AudioContext` it opened, so no extra audio graph outlives the take. It is safe to call twice.
- **The strip takes the host's text colour** (`currentColor`), so it reads on a danger Stop button or a plain row in light and dark with no second palette.

### Options

| Option | Default | Meaning |
| --- | --- | --- |
| `bars` | `24` | Bar count, clamped to 4–64. |
| `height` | `24` | Strip height in px. |
| `reducedMotion` | `prefers-reduced-motion` | `true` draws one level meter instead of moving bars. |
| `timer` | `true` | Show the elapsed `m:ss`. |
| `silenceMs` | `1500` | Input quieter than `floor` for this long reads as silent. |
| `floor` | `0.002` | RMS under which the input counts as quiet. |
| `source` | one it opens | A `createLevelSource()` you own, to share one `AudioContext`; it is not closed on `stop()`. |
| `onSilent` | none | The input just went silent. |
| `onSignal` | none | Sound was heard: the first time, and again after a silent verdict. |

## States

`data-state` on the wave (mirrored as `data-rec-wave-state` on the host):

| State | When | Look |
| --- | --- | --- |
| idle | no wave attached | the record glyph |
| `live` | a level above the floor, or quiet for less than `silenceMs` | bars follow the level |
| `silent` | quiet for `silenceMs` | bars flat at a sliver and dimmed to 40% |
| `unmeasured` | no Web Audio, a refused graph, or a context that is not running | the timer alone |

`unmeasured` is its own state: an input that could not be measured is never shown or reported as silent.

## iOS Safari

An `AudioContext` starts suspended and resumes only inside a user gesture, and a suspended one reads zeros. `createLevelSource` calls `resume()` when it is built, so build it inside the tap. When the stream arrives after an `await` (the first `getUserMedia` prompt), build the source before the await and connect later:

```js
button.addEventListener('click', async () => {
  const source = createLevelSource();            // inside the gesture
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const stopWave = attachRecWave(glyphSpan, stream, { source });  // connects the stream
  // ...on stop: stopWave(); source.close();
});
```

## One level source for the strip and a silent-mic notice

`createLevelSource` is the shared reader, and `SILENCE_RMS` / `SILENCE_MS` are the shared silence verdict. An app that also warns about a dead input (app-launcher's `voice-level.js` is one) takes the warning from `onSilent` / `onSignal` on the same wave instead of opening a second `AudioContext` on the stream: pass `silenceMs` to match its window, and keep its banner in its own callbacks.

`source` is the seam for tests and demos: any object with `running`, `connect(stream)`, `read(bars)` → `{rms, levels}` (or `null` when it cannot read) and `close()` works.

## Required design tokens

| Token | Used for |
| --- | --- |
| `--space-sm` | gap between the strip and the timer |
| `--radius-pill` | bar and meter ends |
| `--font-label` | the timer |

The strip's colour is the host's `color`, not a token of its own.

## Don't diverge

`rec-wave.css` and `rec-wave.js` are vendored verbatim — to change the contract, change it **here in `project-scaffolding`** and re-vendor downstream. An app that draws its own recording strip or its own silence threshold is drifting from the spec; adopt this one instead. Streamlit POC spikes are exempt.
