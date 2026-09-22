# `empty-state` — the canonical zero-items block

The fleet's canonical **empty state**: a centered column — feature-size muted glyph + one-line reason + optional single action — rendered by any list/grid that can legitimately be empty. Never a silent blank area, never a bare "—". Contract: `~/.claude/design.md` → `empty-state` token block + "Component contracts".

## Files

| File | Role |
| --- | --- |
| `empty-state.css` | Visual contract: the centered column + the quiet secondary action button. References design tokens only. |
| `empty-state.js` | ESM builder — `emptyStateEl(name, message, opts)`. Imports `icon()` from the sibling [`icons/`](../icons/) component. |

## How to vendor

1. Copy this `empty-state/` folder **verbatim** into your app's static dir. It expects the vendored [`icons/`](../icons/) component as a sibling (`../icons/icons.js` import) — vendor both.
2. Link the CSS:
   ```html
   <link rel="stylesheet" href="/static/_vendored/empty-state/empty-state.css">
   ```
3. Render from JS wherever a list/grid can come back empty:
   ```js
   import { emptyStateEl } from '/static/_vendored/empty-state/empty-state.js';
   grid.appendChild(emptyStateEl('lightbulb', 'No lights reachable', {
     actionLabel: 'Retry',          // optional
     onAction: () => refresh(),     // optional
   }));
   ```

## Markup contract (what the builder emits)

```html
<div class="empty-state">
  <svg class="icon empty-state-icon">…</svg>
  <p class="empty-state-message">No lights reachable</p>
  <button class="empty-state-action">Retry</button>  <!-- only with actionLabel -->
</div>
```

- `grid-column: 1 / -1` makes the block span a grid container's full width (inert in flex/block containers).
- The action is the **quiet secondary** look (card-off + hairline), never a solid primary — an empty state suggests, it doesn't demand.

## Required design tokens

| Token | Light value | Used for |
| --- | --- | --- |
| `--muted` | `#656d76` | glyph + text |
| `--ink` | `#1f2328` | action text |
| `--line` | `#d1d9e0` | action border |
| `--card-off` | `#f6f8fa` | action fill |
| `--radius-md` | `12px` | action corners |
| `--space-sm` | `8px` | column gap |
| `--space-md` | `16px` | horizontal padding |
| `--space-xl` | `32px` | vertical padding |
| `--font-body` | `1rem` | message |
| `--font-label` | `0.92rem` | action label |
| `--icon-feature` | `24px` | glyph (`icons.size.feature`) |

## Don't diverge

`empty-state.css` / `empty-state.js` are vendored verbatim — to change the contract, change it **here in `project-scaffolding`** and re-vendor downstream. If your own CSS declares the same selector this file touches (e.g. `.app`, `.card`), use longhand properties or a disjoint media condition — a shorthand property at equal specificity is decided by source order, and can silently override a rule you didn't intend to touch. Streamlit POC spikes are exempt.
