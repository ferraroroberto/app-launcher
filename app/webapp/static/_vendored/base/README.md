# `base` — element-level rules every component assumes

The fleet's **base layer**: element-selector rules that sit under every vendored component. Today it is one rule, which makes `<button>`, `<input>`, `<select>` and `<textarea>` inherit the app's font and text color.

Why it exists: form controls don't inherit `font-family` or `color`. The UA stylesheet gives them their own font: Arial at 13.33px on Windows Chrome, and the system font on iOS and Android, which is why the bug only shows on desktop. An app that sets its font stack on `body` alone renders every bare control, and every list row built as a `<button>`, in a second typeface. The audit that found this (project-scaffolding#266) counted 62 elements in Arial on one app-launcher screen. Component classes that already declare `font: inherit` (`button-primary`, `empty-state-action`, the modal's save button) were covered. Bare controls and the other classed ones were not.

## Files

| File | Role |
| --- | --- |
| `base.css` | The element-level rules. No markup, no tokens. |

## How to vendor

1. Copy this `base/` folder **verbatim** into your app's static dir (`app/webapp/static/_vendored/base/`). Do **not** edit `base.css` per-app.
2. Link it **before every other stylesheet**, component or app:
   ```html
   <link rel="stylesheet" href="/static/_vendored/base/base.css">
   ```
3. Keep setting your font stack and text color on `body`, as before. `base.css` passes them down to the controls. It does not define them.

## Contract

- `button, input, select, textarea { font: inherit; line-height: normal; color: inherit; }`. Controls take the family, size, weight and style from their parent, and take its text color.
- **`line-height` stays at the UA's `normal`.** The `font` shorthand would otherwise also inherit the body's line-height (often 1.5), and that grows every padded control's box. In the gallery that meant nav tab 34 → 40px and `button-tint` 48 → 52px. A component that wants the body's line-height sets it on its own class, as `button-primary` does with its own `font: inherit`.
- Element selectors only (specificity 0,0,1). Any component or app class overrides them.
- **What changes when you adopt it.** Unstyled controls go from 13.33px to your body size (usually 16px), and from the system color to your `--ink`. Controls that relied on the small UA size need their own `font-size` token. On Windows desktop, Segoe UI's taller `normal` line metrics add 1 to 4px to padded controls without a fixed `height` (gallery: nav tab 34 → 38px, `button-ghost` 28 → 30px). That is the intended typeface rendering at its real size. Phones don't change, because their UA control font was already the system font.
- Disabled bare controls now take the inherited color rather than the UA's grey. Use a component's disabled recipe (e.g. `button/`) rather than relying on the UA.

## Required design tokens

None. It inherits whatever the app sets on `body` (normally `--font` and `--ink` from `~/.claude/design.md`).

## Don't diverge

`base.css` is vendored verbatim. To change it, change it **here in `project-scaffolding`** and re-vendor downstream. Don't restate the rule in your app's stylesheet: a second copy drifts, and a shorthand at equal specificity later in the cascade silently wins. Streamlit POC spikes are exempt.
