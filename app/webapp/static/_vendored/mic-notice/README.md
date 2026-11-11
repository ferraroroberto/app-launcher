# `mic-notice` — the recorder's input name and silent-mic warning

The notice under a record button while the mic is open: the input's name ("Mic: AirPods"), the live level strip and, when the input carries no sound for 3 seconds, a warning under them. It is built on [`rec-wave/`](../rec-wave/)'s `onSilent` / `onSignal`, so one `AudioContext` serves the strip and the warning. Normalized from app-launcher's dictation notice (`voice.js` + `voice-level.js`) and voice-transcriber's `mic-notice.js`; the warning texts are app-launcher's current ones.

## Files

| File | Role |
| --- | --- |
| `mic-notice.css` | Visual contract — the centred column, the muted input line, the accent strip, the attention warning. References design tokens only. |
| `mic-notice.js` | `attachMicNotice(noticeEl, stream, opts)` → `stop()`, plus `inputLabel(stream)`, `silentText(label)` and `SILENT_MS`. |

It imports `../rec-wave/rec-wave.js`, so vendor `rec-wave/` beside it.

## How to vendor

1. Copy this `mic-notice/` folder **verbatim** next to `rec-wave/` in your app's static dir (`app/webapp/static/_vendored/mic-notice/`). Do **not** edit either file per-app.
2. Link both stylesheets and import the function:
   ```html
   <link rel="stylesheet" href="/static/_vendored/rec-wave/rec-wave.css">
   <link rel="stylesheet" href="/static/_vendored/mic-notice/mic-notice.css">
   ```
   ```js
   import { attachMicNotice } from '/static/_vendored/mic-notice/mic-notice.js';
   ```
3. Add one empty, hidden container where the notice belongs. The component builds everything inside it:
   ```html
   <div id="micNotice" hidden></div>
   ```

## Usage

```js
// In the tap that starts recording, with the stream the recorder already has.
const stopNotice = attachMicNotice(document.getElementById('micNotice'), stream);
// ...when recording ends (always, including on error):
stopNotice();
```

- **The component owns the markup.** `attachMicNotice` adds the `mic-notice` class to the container, fills it with `.mic-notice-input`, `.mic-notice-wave` and `.mic-notice-silent` (a `role="status"` region), and unhides it. The strip runs for the whole take, so the notice shows even when the browser withholds the input's name (the input line is then hidden).
- **No second permission prompt.** It reads the stream you already have and never calls `getUserMedia`; `stop()` never stops the stream's tracks.
- **`stop()` releases.** It empties and hides the container and removes the strip (closing the `AudioContext` it opened). It is safe to call twice.
- **The words and the window are the component's.** An app never words a warning or sets a threshold.

### The warning

| Input | Text |
| --- | --- |
| a label matching `Remote Audio` (Windows' redirected Remote Desktop input) | `No sound from the microphone — check the Remote Desktop audio recording setting ('Record from this computer')` |
| every other input, or no label | `No sound from the microphone — check Bluetooth/AirPods` |

The warning appears once the input has stayed under rec-wave's floor for `SILENT_MS` (3000 ms) and clears the moment sound returns. While it shows, the container also carries `is-silent`.

### Options

| Option | Default | Meaning |
| --- | --- | --- |
| `source` | one it opens | A `createLevelSource()` you own, to build the `AudioContext` inside the tap on iOS (see rec-wave's README); it is not closed on `stop()`. |
| `timer` | `true` | Show rec-wave's elapsed `m:ss` beside the strip. |
| `onSilent` | none | `(text, label)` — the warning just showed. For a haptic, a toast when the container is detached, or a log line. |
| `onSignal` | none | Sound was heard and the warning cleared. |

## Required design tokens

| Token | Used for |
| --- | --- |
| `--muted` | the input line |
| `--accent` | the strip (rec-wave takes its colour from the host) |
| `--attention`, `--attention-text` | the warning's border and text |
| `--space-xs`, `--space-sm`, `--radius-sm` | spacing and the warning's corners |
| `--font-label` | the notice's type size |

Plus the tokens rec-wave names. The warning is a border and a bold line of text, never colour alone.

## Testing without a mic

`tests/e2e/_mic_notice_checks.py` (a case of `test_vendored_rec_wave.py`) drives the real module with a fixed level source (`opts.source`), a stream that only names its input, and Playwright's fake clock, so the 3000 ms window is checked to the millisecond with no real waiting. An app's own test does the same; a real mic is never needed.

## Don't diverge

`mic-notice.css` and `mic-notice.js` are vendored verbatim — to change the window, a text or the markup, change it **here in `project-scaffolding`** and re-vendor downstream. An app that words its own silent-mic warning or keeps its own copy of this logic is drifting from the spec; adopt this one instead. Streamlit POC spikes are exempt.
