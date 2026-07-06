# `disclosure` — the collapsible details/summary card

The fleet's canonical **disclosure row**: a `<details>` card whose `<summary>` is a fixed 52px header — leading glyph + `<h3>` title left, chevron pinned right — so a vertical stack of collapsible cards is pixel-identical whether open or closed. home-automation's strongest de-facto component, promoted to canon by the #358 polish round. Contract: `~/.claude/design.md` → `disclosure` token block + "Components" prose.

## Files

| File | Role |
| --- | --- |
| `disclosure.css` | Visual + structural contract. The one shared `.card--collapsible` modifier (padding-zeroing, closed height, open divider) + the summary/chevron/body rules. References design tokens only. |
| `disclosure.html` | Markup skeleton to copy and adapt. |

## How to vendor

1. Copy this `disclosure/` folder **verbatim** into your app's static dir. Do **not** edit `disclosure.css` per-app.
2. It layers on the [`card/`](../card/) component — link both:
   ```html
   <link rel="stylesheet" href="/static/_vendored/card/card.css">
   <link rel="stylesheet" href="/static/_vendored/disclosure/disclosure.css">
   ```
3. Paste the skeleton, rename the icon + title. To restore the initially-open state, add the `open` attribute to the `<details>`.

## Markup contract

```html
<details class="card card--collapsible">
  <summary class="collapse-summary">
    <span class="collapse-main">
      <svg class="icon">…</svg>
      <h3 class="collapse-title">Title</h3>
      <span class="collapse-count">…</span>  <!-- optional -->
    </span>
    <span class="collapse-chevron" aria-hidden="true">›</span>
  </summary>
  <div class="collapse-body">…</div>
</details>
```

**The four structural rules** (all carried by `.card--collapsible` — never re-implement them per-card):

1. The card's own padding is **zeroed** — it must not double up with the summary's padding (the root cause of every past closed-height regression).
2. The `<summary>` owns the closed-state box: **52px** tall, `0 14px` padding.
3. The open state adds a `border-muted` divider under the summary — the **only** divider.
4. The body uses `12px 14px 14px` padding (use a `padding-top: 0` override on your body element when its first child already carries top padding, e.g. a list).

## Required design tokens

On top of [`card/`](../card/)'s tokens:

| Token | Light value | Used for |
| --- | --- | --- |
| `--line-muted` | `#d8dee4` | open-state divider (`border-muted`) |
| `--muted` | `#656d76` | chevron, trailing count |
| `--font-body` | `1rem` | title |
| `--font-caption` | `0.78rem` | trailing count |
| `--font-heading-lg` | `1.5rem` | chevron glyph size |
| `--icon-title` | `18px` | leading glyph (`icons.size.title`) |

## Don't diverge

`disclosure.css` is vendored verbatim — to change the contract, change it **here in `project-scaffolding`** and re-vendor downstream. In particular, never re-introduce per-card `height`/`padding` enumerations: the shared modifier exists precisely because that enumeration is how the contract drifts. Streamlit POC spikes are exempt.
