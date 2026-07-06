# `card` — the base content group

The fleet's canonical **card**: an elevated surface one step above the canvas (surface color + hairline border, no shadow), `rounded.lg` corners, `spacing.md` padding, and a one-row header — leading title-size glyph + bold title + optional muted meta, right-pinned chevron/meta. Contract: `~/.claude/design.md` → "Component contracts" → **card**.

## Files

| File | Role |
| --- | --- |
| `card.css` | Visual contract — the `.card` base + `.card-head` header row. References design tokens only. |
| `card.html` | Markup skeleton to copy and adapt. |

## How to vendor

1. Copy this `card/` folder **verbatim** into your app's static dir (`app/webapp/static/_vendored/card/`). Do **not** edit `card.css` per-app.
2. Link the CSS and paste the skeleton where a content group goes:
   ```html
   <link rel="stylesheet" href="/static/_vendored/card/card.css">
   ```
3. Icons come from the vendored [`icons/`](../icons/) component (the `#i-NAME` sprite).

## Markup contract

```html
<div class="card">
  <div class="card-head">
    <h3 class="card-title"><svg class="icon">…</svg> Title</h3>
    <span class="card-head-meta">meta</span>   <!-- optional -->
  </div>
  <p class="card-meta">…</p>                    <!-- optional -->
  …body…
</div>
```

- The header is optional — a bare `.card` is just the elevated surface.
- A **collapsible** card is a different component: use [`disclosure/`](../disclosure/) (`<details class="card card--collapsible">`), which zeroes the card's own padding so closed cards align (the padding-doubling rule in `design.md`).

## Required design tokens

Define these CSS custom properties in your app's `:root` / `[data-theme="dark"]` blocks, **wired to `~/.claude/design.md` (+ `design.dark.md`)**. Reference values (light):

| Token | Light value | Used for |
| --- | --- | --- |
| `--card` | `#ffffff` | card surface |
| `--line` | `#d1d9e0` | hairline border |
| `--ink` | `#1f2328` | primary text |
| `--muted` | `#656d76` | header meta / meta line |
| `--radius` | `16px` | corners (`rounded.lg`) |
| `--space-md` | `16px` | padding |
| `--space-sm` | `8px` | header gap |
| `--font-body` | `1rem` | title |
| `--font-label` | `0.92rem` | right-pinned meta |
| `--font-caption` | `0.78rem` | meta line |
| `--icon-title` | `18px` | leading glyph (`icons.size.title`) |

## Don't diverge

`card.css` is vendored verbatim — to change the contract, change it **here in `project-scaffolding`** and re-vendor downstream. Streamlit POC spikes are exempt.
