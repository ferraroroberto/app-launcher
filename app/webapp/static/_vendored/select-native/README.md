# `select-native` — the native select at control height

The fleet's canonical **native `<select>`**: sized to the `control` token (36px) so a row of inline controls (select / input / toggle) lines up on one line — canvas-subtle fill, hairline border, `rounded.md` corners, label-size text. Contract: `~/.claude/design.md` → "Base UI" → **Select** (sized to `control`, 36px), and the `control` component token. Normalized from home-automation's shipped `styles.css` `.select-native`.

## Files

| File | Role |
| --- | --- |
| `select-native.css` | Visual contract — the `control`-height native select. References design tokens only. |
| `select-native.html` | Markup skeleton to copy and adapt. |

## How to vendor

1. Copy this `select-native/` folder **verbatim** into your app's static dir (`app/webapp/static/_vendored/select-native/`). Do **not** edit `select-native.css` per-app.
2. Link the CSS:
   ```html
   <link rel="stylesheet" href="/static/_vendored/select-native/select-native.css">
   ```
3. Paste the skeleton, adapt the `<option>`s, give it a stable `id`/`name`, and wire change handling in your own JS.

## Markup contract

```html
<select class="select-native" aria-label="…">
  <option value="a">Option A</option>
  <option value="b">Option B</option>
</select>
```

- The `.select-native` class is the whole contract — a bare native `<select>`, no wrapper required.
- Height comes from an explicit `height: var(--control-h)` (36px). **Never** give a `<select>` its height via `min-height` or padding: iOS Safari ignores `min-height` on a bare `<select>` and renders it at its stubby intrinsic height, and a padding-derived height drifts off the 36px `control` lockstep. This gotcha — encoded once here — is why per-app selects kept rendering stubby.
- Width is unconstrained (a bare `<select>` sizes to content); set `width: 100%` in your own layout context (e.g. a stacked field or a grid cell) when you want it to fill.
- The **modal** component (`_vendored/modal/`) inlines this same `.select-native` recipe for its inline row selects — the two declarations are intentionally byte-identical, so an app vendoring both gets one consistent `.select-native`. To change the select contract, change it in **both** places here.

## Required design tokens

| Token | Light value | Used for |
| --- | --- | --- |
| `--control-h` | `36px` | select height (the `control` lockstep) |
| `--font-label` | `0.92rem` | option/label text |
| `--radius-md` | `12px` | corners |
| `--line` | `#d1d9e0` | border |
| `--input-bg` | `var(--card-off)` | fill |
| `--ink` | `#1f2328` | text |

## Don't diverge

`select-native.css` is vendored verbatim — to change the contract, change it **here in `project-scaffolding`** (and the mirrored declaration in `_vendored/modal/modal.css`) and re-vendor downstream. In particular, never re-introduce a `min-height`-based or padding-derived select height (the stubby-on-iOS regression this component exists to prevent). If your own CSS declares the same selector this file touches, use longhand properties or a disjoint media condition — a shorthand property at equal specificity is decided by source order, and can silently override a rule you didn't intend to touch. Streamlit POC spikes are exempt.
