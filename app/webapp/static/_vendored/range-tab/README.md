# `range-tab` — the ghost segmented control

The fleet's canonical **range-tab**: a row of equal-width pills (Day/Week/Month, timer presets, …) — resting is `card-off` fill + hairline border + muted text, active is `accent-soft` fill + `accent-text` text + a solid `accent` border. One height everywhere (`--control-h`, 36px — home-automation issue #361's census found 34/32/24px across five call sites before this settled on one canonical height). At most five pills per row, each label on one line. `~/.claude/design.md` has no range-tab entry yet, so this README is the contract. It draws on the spec's `control` height, the `accent-soft` active tint (shared with `nav-tab-active`) and "Touch targets".

## Files

| File | Role |
| --- | --- |
| `range-tab.css` | Visual contract — the pill row, resting/active/disabled states, the `::before` tap-target extension. References design tokens only. |
| `range-tab.html` | Markup skeleton to copy and adapt. |

## How to vendor

1. Copy this `range-tab/` folder **verbatim** into your app's static dir (`app/webapp/static/_vendored/range-tab/`). Do **not** edit `range-tab.css` per-app.
2. Link the CSS:
   ```html
   <link rel="stylesheet" href="/static/_vendored/range-tab/range-tab.css">
   ```
3. Paste the skeleton, adapt the `data-*` attribute and labels to your range values.

## Markup contract

```html
<nav class="range-tabs" aria-label="History range">
  <button type="button" class="range-tab active" data-range="day">Day</button>
  <button type="button" class="range-tab" data-range="week">Week</button>
  <button type="button" class="range-tab" data-range="month">Month</button>
</nav>
```

- `.range-tabs` is the flex row container; `.range-tab` is each pill.
- Exactly one `.range-tab` carries `.active` at a time — toggling it is the caller's job (the driving value differs per use: a range string, a seconds count, a day index), so there is no shared JS builder here. The typical wiring is a one-line `classList.toggle`:
  ```js
  btns.forEach((btn) => btn.classList.toggle('active', btn.dataset.range === range));
  ```
- Disabled is the plain `disabled` attribute on any pill — the CSS handles the look (opacity 0.45).
- The visible pill height is `--control-h` (36px). The `::before` pseudo-element extends the tap target to the 44px floor without inflating the visual box. The expansion is vertical only, so neighbouring pills' hit areas never overlap (design.md "Touch targets", adjacent cluster). Width is left alone, since every pill in a row of five or fewer is already wider than 44px. The gallery harness asserts both on a coarse pointer.
- **At most five options.** Six or more is a `select-native` (`_vendored/select-native/`): past five, the pills drop toward the 44px width floor and the labels start to wrap.
- **One-line labels.** Pills set `white-space: nowrap`. A label too long for its pill overflows instead of breaking onto a second line, so use a shorter label ("Extra high" → "Max") or a select.
- **The selected state reads without color.** The active pill's solid `accent` border is at least 3:1 against the resting `line` border in both themes. It is the one channel that survives greyscale: the `accent-soft` fill and accent text are close to 1:1 with the resting pill once hue is gone.

## Required design tokens

| Token | Light value | Used for |
| --- | --- | --- |
| `--card-off` | `#f6f8fa` | resting fill |
| `--line` | `#d1d9e0` | resting border |
| `--muted` | `#656d76` | resting text |
| `--accent` | `#0969da` | active border |
| `--accent-text` | `#0550ae` (dark `#58a6ff`) | active text |
| `--accent-soft` | `color-mix(in srgb, var(--accent) 16%, transparent)` | active fill |
| `--radius-md` | `12px` | pill corners |
| `--control-h` | `36px` | pill height |
| `--font-label` | `0.92rem` | pill text |

## Don't diverge

`range-tab.css` is vendored verbatim — to change the contract, change it **here in `project-scaffolding`** and re-vendor downstream. In particular, never re-introduce a per-view height override (the census in home-automation#361 is exactly how the old drift happened) — a smaller font-size in a compact context is fine, the box height is not. If your own CSS declares the same selector this file touches (e.g. `.app`, `.card`), use longhand properties or a disjoint media condition — a shorthand property at equal specificity is decided by source order, and can silently override a rule you didn't intend to touch. Streamlit POC spikes are exempt.
