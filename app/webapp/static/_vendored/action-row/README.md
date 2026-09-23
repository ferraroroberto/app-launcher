# `action-row` — a list row that does something

The fleet's canonical **action-row** (`~/.claude/design.md` → "Component contracts" → action-row, `ferraroroberto/fleet-config#965`): a flat list row whose **tap performs its primary action** (launch, run, open), with every other action behind **one trailing 44px kebab**. A row may add **at most one leading toggle** (favorite) and **at most one other visible action**, and only when that action is the row's dominant verb (Run on a job row). Title: `body` at 600, one line, ellipsized. Context: at most one `body-sm` line in muted. No vertical rules between controls. A list that can exceed ~12 rows gets a filter field above it.

Normalized from app-launcher's session rows (`app-launcher#1025`: one row button plus a kebab in its own slot) and the rendered UX audit's proposed row (`fleet-config#962`), to stop a fifth hand-built row shape (`project-scaffolding#268`).

## Files

| File | Role |
| --- | --- |
| `action-row.css` | Visual contract: the full-bleed list card, the row and its `rows` heights, the stretched main button, title/meta truncation, the 44px accessories, the filter field. References design tokens only. |
| `action-row.html` | Markup skeleton to copy and adapt. |

It also needs two glyphs from the vendored `icons/` sprite: `i-ellipsis-vertical` (the kebab) and, for a favorite toggle, `i-star` + `i-star-fill`. `i-search` and `i-play` are used by the skeleton's filter and verb.

## How to vendor

1. Copy this `action-row/` folder **verbatim** into your app's static dir (`app/webapp/static/_vendored/action-row/`). Do **not** edit `action-row.css` per-app.
2. Link it (any order relative to `card.css`; the list-card rule uses two classes, so it wins either way):
   ```html
   <link rel="stylesheet" href="/static/_vendored/action-row/action-row.css">
   ```
3. Paste the skeleton, drop the optional parts your rows don't have, and wire the behaviour (below).

## Markup contract

```html
<div class="card action-list">
  <label class="action-row-filter">                        <!-- optional: lists that can pass ~12 rows -->
    <svg class="icon" aria-hidden="true"><use href="#i-search"></use></svg>
    <input type="search" placeholder="Filter skills" aria-label="Filter skills">
  </label>
  <ul class="action-rows">
    <li class="action-row">
      <button type="button" class="action-row-fav" aria-pressed="false" aria-label="Favorite">  <!-- optional -->
        <svg class="icon action-row-fav-off" aria-hidden="true"><use href="#i-star"></use></svg>
        <svg class="icon action-row-fav-on" aria-hidden="true"><use href="#i-star-fill"></use></svg>
      </button>
      <button type="button" class="action-row-main">          <!-- or <a href> when the row navigates -->
        <span class="action-row-title">journal-daily</span>
        <span class="action-row-meta">Last run yesterday</span>  <!-- optional -->
      </button>
      <button type="button" class="action-row-verb" aria-label="Run">…</button>  <!-- optional, dominant verb only -->
      <button type="button" class="action-row-kebab" aria-label="More actions"
              aria-haspopup="menu" aria-expanded="false">
        <svg class="icon" aria-hidden="true"><use href="#i-ellipsis-vertical"></use></svg>
      </button>
    </li>
  </ul>
</div>
```

- **`.card action-list`** is a `.card` modifier that zeroes the card's padding: the rows are full-bleed, on a `--line-muted` hairline between rows (`.action-row + .action-row`), never nested cards. Rows placed in another host (a disclosure body) need that host's horizontal padding at zero too.
- **`.action-row-main`** is the primary action: a `<button>`, or an `<a>` when the row only navigates. It stretches to the row's full height, so the tap target is the row, not the words. The kebab (and the favorite, and the verb) are **siblings** of it, never children, so a tap on one never also fires the row.
- **Height** comes from the `rows` scale: a title-only row is `--row-md` (52px), a row with an `.action-row-meta` line is `--row-lg` (60px). Never a literal.
- **`.action-row-title` / `.action-row-meta`** are one line each, ellipsized. Put one context line at most; a row that needs more is a detail view.
- **`.action-row-fav`** is the one leading toggle. The caller flips `aria-pressed` only; CSS swaps the outline star for the filled one and colors it `--attention`. The fill is what reads without hue.
- **`.action-row-verb`** is the one extra visible action, the tint recipe at 44px. Use it only for the row's dominant verb (Run on a job row); everything else goes in the menu.
- **`.action-row-kebab`** opens the row menu. Keep `aria-expanded` in step with the menu; the kebab takes `--accent` while it is open.
- **`.action-row-filter`** is a 44px field with a `--control-border` boundary on `--card`. Filtering is the caller's job (set `hidden` on the `<li>`s that don't match).

All accessories are real 44×44 boxes placed side by side with no gap, so their hit rectangles touch and never overlap (`design.md` → "Touch targets", adjacent cluster).

## The row menu stays app-side (for now)

This component ships the **anchor**, not the floating menu. The reference implementation is app-launcher's `row-menu.js` (one anchor opens a vertical list of icon + label items; it survives a poll re-render and closes on outside tap, Escape and a second tap). Whatever the app uses, the menu keeps the contract: **destructive actions** (Kill, Stop, Delete) are its **last item, after a divider, in `--danger-text`, behind a confirm**. A row never shows a visible danger button.

## Required design tokens

| Token | Light value | Used for |
| --- | --- | --- |
| `--row-sm` | `44px` | accessory squares, filter height (`hit-target.min`) |
| `--row-md` | `52px` | one-line row height |
| `--row-lg` | `60px` | row height with a context line |
| `--line-muted` | `#d8dee4` | hairline between rows (`border-muted`) |
| `--ink` | `#1f2328` | title text |
| `--muted` | `#656d76` | context line, resting accessory glyphs, filter glyph |
| `--font-body` | `1rem` | title, filter input |
| `--font-body-sm` | `0.875rem` | context line (`typography.body-sm`) |
| `--icon-inline` | `16px` | accessory and filter glyphs (`icons.size.inline`) |
| `--radius-md` | `12px` | accessories, filter, main-button press shape |
| `--space-sm` | `8px` | filter glyph ↔ input |
| `--gap` | `12px` | filter margin |
| `--accent` | `#0969da` | open kebab, filter focus ring |
| `--accent-soft` | `color-mix(…16%…)` | verb fill, row press feedback |
| `--accent-border-soft` | `color-mix(…24%…)` | verb border |
| `--accent-text` | `#0550ae` | verb glyph (accent on a tint) |
| `--attention` | `#9a6700` | pressed favorite |
| `--control-border` | `#818b98` | filter boundary |
| `--card` | `#ffffff` | filter fill |

## Don't diverge

`action-row.css` is vendored verbatim: to change the contract, change it **here in `project-scaffolding`** and re-vendor downstream. Don't add a second visible action, a vertical rule, or a visible danger button per-app; those are the shapes this component exists to retire. If your own CSS declares a selector this file touches, use longhand properties or a disjoint media condition — a shorthand at equal specificity is decided by source order. Streamlit POC spikes are exempt.
