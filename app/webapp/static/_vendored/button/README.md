# `button` — the four-tier action vocabulary

The fleet's canonical **button**: four tiers covering every action, settled by the button-tier sweep (fleet-config#296) after a deterministic CSS parse of ~135 button rule variants across 7 apps found the same class names meaning different buttons per app. Contract: `~/.claude/design.md` → "Component contracts" → **button tiers**.

- `.button-primary` — solid accent fill. The one main action per view.
- `.button-tint` — accent-soft fill, accent text, soft accent border. Secondary emphasis. **A tint is not a ghost.**
- `.button-ghost` — transparent fill, hairline border, muted text. Quiet tertiary actions. **Ghost means transparent** — a tinted fill is a tint, never a "ghost".
- `.button-surface` — card-off fill at control height. Toolbar/utility/icon buttons.
- `.button-tint.danger` — the tint recipe restated on `--deficit` for a destructive action.
- One shared `:disabled` recipe applies to all four tiers (home-automation#362) — the flat card-off/line/muted trio, never opacity on a solid fill.

## Files

| File | Role |
| --- | --- |
| `button.css` | Visual contract — the four tiers, the danger variant, the shared disabled recipe. References design tokens only. |
| `button.html` | Markup skeletons to copy and adapt. |

## How to vendor

1. Copy this `button/` folder **verbatim** into your app's static dir (`app/webapp/static/_vendored/button/`). Do **not** edit `button.css` per-app.
2. Link the CSS and use the class that matches the action's weight:
   ```html
   <link rel="stylesheet" href="/static/_vendored/button/button.css">
   ```

## Markup contract

```html
<button type="button" class="button-primary">Save</button>
<button type="button" class="button-tint">Run now</button>
<button type="button" class="button-ghost">Cancel</button>
<button type="button" class="button-surface">Options</button>
<button type="button" class="button-primary" disabled>Save</button>
<button type="button" class="button-tint danger">Delete</button>
```

- Exactly **one** `.button-primary` per view — secondary emphasis is `.button-tint`, never a second solid fill.
- Disabled is the plain `disabled` attribute on any tier, not a separate class — the shared recipe handles the look.
- `.danger` only composes with `.button-tint` (the vendored recipe covers that one combination; a primary/ghost/surface destructive action is out of scope until a real usage shows up).

## Legacy-class migration

Per-app adoption is tracked separately (fleet-config#279) — this table is the mapping when a consuming app re-skins its own buttons to the vendored classes:

| Legacy class | Vendored class |
| --- | --- |
| `.detail-save-btn` | `.button-primary` |
| `.big-btn` | `.button-tint` |
| `.ghost-btn` | `.button-ghost` |
| `.shaded-btn` | `.button-surface` |

## Required design tokens

Define these CSS custom properties in your app's `:root` / `[data-theme="dark"]` blocks, **wired to `~/.claude/design.md` (+ `design.dark.md`)**. Reference values (light):

| Token | Light value | Used for |
| --- | --- | --- |
| `--accent` | `#0969da` | primary fill, tint text |
| `--accent-fg` | `#ffffff` | primary text |
| `--accent-soft` | `color-mix(in srgb, var(--accent) 16%, transparent)` | tint fill |
| `--accent-border-soft` | `color-mix(in srgb, var(--accent) 24%, transparent)` | tint border |
| `--accent-border-strong` | `color-mix(in srgb, var(--accent) 28%, transparent)` | primary border |
| `--card-off` | `#f6f8fa` | surface fill, disabled fill (all tiers) |
| `--line` | `#d1d9e0` | ghost/surface border, disabled border |
| `--muted` | `#656d76` | ghost/surface text, disabled text |
| `--deficit` | `#cf222e` | danger text |
| `--deficit-soft` | `color-mix(in srgb, var(--deficit) 12%, transparent)` | danger fill |
| `--deficit-border-soft` | `color-mix(in srgb, var(--deficit) 30%, transparent)` | danger border |
| `--radius-md` | `12px` | corners (`rounded.md`), all tiers |
| `--control-h` | `36px` | surface tier height |
| `--font-body` | `1rem` | tint text |
| `--font-label` | `0.92rem` | surface text |
| `--font-caption` | `0.78rem` | ghost text |

## Don't diverge

`button.css` is vendored verbatim — to change a tier's recipe, change it **here in `project-scaffolding`** and re-vendor downstream. The modal's own `.detail-save-btn` (`_vendored/modal/modal.css`) is a separate, still-current embedded copy scoped to the footer context (full width, `--control-h`, the tap-target `::before` extension); this component generalizes the same visual recipe for standalone use at the spec's 48px height. If your own CSS declares the same selector this file touches (e.g. `.app`, `.card`), use longhand properties or a disjoint media condition — a shorthand property at equal specificity is decided by source order, and can silently override a rule you didn't intend to touch. Streamlit POC spikes are exempt.
