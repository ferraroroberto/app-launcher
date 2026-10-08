# `icon-button` — the glyph-only control

The fleet's canonical **icon button**: a glyph-only control that is its own component, not a `button-surface`. A glyph on nothing — **no fill, border or shadow at rest**, one glyph size (`--icon-inline`, 16px, `icons.size.inline`), and hover and press change **the glyph colour only**, so a row of icon buttons never reads as a set of painted squares. Its 44px target is invisible, and two side by side sit far enough apart that their targets touch and never overlap. Normalized from task-os's shipped app-wide `.icon-btn` (task-os#357). Contract: `~/.claude/design.md` → "Component contracts" → icon button (fleet-config#1259).

`button-surface` (in [`button/`](../button/)) stays the **labelled** utility tier. A control with a visible text label is a button tier; a control that is only a glyph is this.

## Files

| File | Role |
| --- | --- |
| `icon-button.css` | Visual contract — the transparent box, the 16px glyph, the glyph-colour states (hover, pressed, danger, disabled), the invisible `::before` target, the `.icon-button-group` gap. References design tokens only. |
| `icon-button.html` | Markup skeletons to copy and adapt. |

## How to vendor

1. Copy this `icon-button/` folder **verbatim** into your app's static dir (`app/webapp/static/_vendored/icon-button/`). Do **not** edit `icon-button.css` per-app.
2. Link the stylesheet:
   ```html
   <link rel="stylesheet" href="/static/_vendored/icon-button/icon-button.css">
   ```
3. Put one inline Lucide glyph (from [`icons/`](../icons/)) inside each button and give it an `aria-label`.

## Markup contract

```html
<button type="button" class="icon-button" aria-label="Edit" title="Edit">
  <svg class="icon" aria-hidden="true"><use href="#i-pencil"></use></svg>
</button>
<button type="button" class="icon-button" aria-pressed="true" aria-label="Pin" title="Pin">
  <svg class="icon" aria-hidden="true"><use href="#i-pin"></use></svg>
</button>
<button type="button" class="icon-button danger" aria-label="Delete" title="Delete">
  <svg class="icon" aria-hidden="true"><use href="#i-trash-2"></use></svg>
</button>
<span class="icon-button-group">
  <button type="button" class="icon-button" aria-label="Previous" title="Previous">…</button>
  <button type="button" class="icon-button" aria-label="Next" title="Next">…</button>
</span>
```

- **At rest:** transparent background, no border, `box-shadow: none`, a `--muted` glyph at `--icon-inline` (16px). There is no filled or bordered variant.
- **Hover** darkens the glyph to `--ink`. **Pressed** (`aria-pressed="true"`, flipped by the caller) takes `--accent-text`. **Disabled** is the plain `disabled` attribute: a faint `--line` glyph and the default cursor. None of them paint a fill.
- **Destructive** (`.danger`): quiet at rest like every other one, then `--danger-text` on hover — state, not decoration.
- **Label:** the button has no visible text, so `aria-label` is required; add a `title` for the pointer tooltip.
- **Focus** is the app-wide `:focus-visible` ring (design.md's focus contract). The component ships no focus rule of its own, like `button/`.
- **The box** is `--icon-btn-box` square (default `28px`) — the hover and focus area around the glyph, never painted. A context that needs a different box sets the custom property, e.g. a toolbar at the control height: `style="--icon-btn-box: var(--control-h)"`. The glyph stays 16px whatever the box.
- **The target:** a `::before` grows the box to `--hit-min` (44px) on every side, `inset: (box − hit-min) / 2`. A box at or above the floor needs no expansion (the inset clamps to 0). The e2e harness asserts the effective target is at least 44px.
- **Neighbours** sit in an `.icon-button-group`. Its gap is `hit-min − box` (16px at the default box): the two half-expansions together, so the two effective targets touch and never overlap (design.md "Touch targets"). Set `--icon-btn-box` on the group, or an ancestor, rather than on one button, so the gap and the boxes read the same value. The harness asserts the non-overlap.
- **Consumers.** The vendored [`home-head/`](../home-head/) toggles, the [`modal/`](../modal/) × and the [`action-row/`](../action-row/) accessories are `.icon-button`s too — they carry `icon-button` beside their own class and keep only their placement and box (`--icon-btn-box`: 34px, 34px, and the 44px `--row-sm`), the title-size glyph on the first two, and (action-row) the favorite's glyph swap, the verb's tint and the open kebab's accent. A component that overrides an icon-button state colour uses a selector of higher specificity, never source order.

## Required design tokens

Define these CSS custom properties in your app's `:root` / `[data-theme="dark"]` blocks, **wired to `~/.claude/design.md` (+ `design.dark.md`)**. Reference values (light):

| Token | Light value | Used for |
| --- | --- | --- |
| `--muted` | `#656d76` | glyph at rest |
| `--ink` | `#1f2328` | glyph on hover |
| `--accent-text` | `#0550ae` (dark `#58a6ff`) | glyph when `aria-pressed="true"` |
| `--danger-text` | `#a40e26` (dark `#ff7b72`) | `.danger` glyph on hover |
| `--line` | `#d1d9e0` | disabled glyph |
| `--icon-inline` | `16px` | glyph size (`icons.size.inline`) |
| `--radius-sm` (fallback `8px`) | `8px` | box corners (the focus ring follows them) |
| `--hit-min` (fallback `44px`) | `44px` | effective target floor (`components.hit-target.min`) |

`--icon-btn-box` (default `28px`) is a per-context knob, not a design token: leave it unset for the default box, or set it on a container to pick another.

## Don't diverge

`icon-button.css` is vendored verbatim — to change the contract, change it **here in `project-scaffolding`** and re-vendor downstream. Never give an icon button a resting fill, border or shadow to make it "look clickable": that is the drift fleet-config#1259 removed. If your own CSS declares the same selector this file touches, use longhand properties or a disjoint media condition — a shorthand property at equal specificity is decided by source order, and can silently override a rule you didn't intend to touch. Streamlit POC spikes are exempt.
