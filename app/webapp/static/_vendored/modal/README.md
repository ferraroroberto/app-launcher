# `modal` — the editor `<dialog>` shell

The fleet's canonical **editor modal**: a native `<dialog>` with a `heading-lg` title + 34px square × close, stacked label/value rows on top-border dividers (value control ≥ 55%), and exactly one full-width primary button whose disabled state clears AA in both themes. home-automation ships this one shell across its plug/zone/net-device/camera/AC editors. Contract: `~/.claude/design.md` → `modal` token block + "Component contracts".

## Files

| File | Role |
| --- | --- |
| `modal.css` | Visual + structural contract: the dialog shell, iOS anchoring + scroll-lock rules, header/close, rows, the shared 36px inline control, sticky action bar + AA disabled recipe. References design tokens only. |
| `modal.html` | Markup skeleton to copy and adapt. |

## How to vendor

1. Copy this `modal/` folder **verbatim** into your app's static dir. Do **not** edit `modal.css` per-app — every iOS rule in it is on-device-validated (home-automation #214/#300/#303).
2. Link the CSS, paste the skeleton, and drive it with the native dialog API:
   ```html
   <link rel="stylesheet" href="/static/_vendored/modal/modal.css">
   ```
   ```js
   document.getElementById('exampleDialog').showModal();  // open
   // close: the × button, el.close(), or Esc (native)
   ```
3. The fleet nav hides itself while the dialog is open (`body:has(dialog[open])` — the nav component owns that rule). The scroll-lock rules here reference `.app`, the nav component's content wrapper — they are inert if your app doesn't use it.

## Markup contract

```html
<dialog class="detail-dialog">
  <div class="detail-card">
    <div class="detail-header">
      <h2>Title</h2>
      <button class="detail-close" aria-label="Close">…×…</button>
    </div>
    <div class="row"><span>Label</span> <input class="input-native"></div>
    <div class="row"><span>Label</span> <select class="select-native">…</select></div>
    <div class="detail-actions">
      <button class="detail-save-btn">Save</button>
    </div>
  </div>
</dialog>
```

- Use the **native** dialog API (`showModal()`/`close()`) — never a hand-rolled overlay (the shadcn Dialog rule in `design.md`).
- Exactly **one** primary action; it starts `disabled` until something changes.
- The disabled recipe (card-off/muted/line) is part of the contract — opacity-based disabling drops sub-AA in both themes.
- On mobile the dialog is top-anchored (safe-area aware); on desktop it keeps UA centering. Don't override.

## Required design tokens

| Token | Light value | Used for |
| --- | --- | --- |
| `--card` | `#ffffff` | dialog card / action bar |
| `--card-off` | `#f6f8fa` | disabled primary fill |
| `--ink` | `#1f2328` | text |
| `--muted` | `#656d76` | close glyph, disabled text |
| `--line` | `#d1d9e0` | row dividers, control borders |
| `--accent-fill` | `#0969da` (dark `#1f6feb`) | primary button fill |
| `--accent-fg` | `#ffffff` | primary button text |
| `--accent-border-strong` | `color-mix(in srgb, var(--accent) 28%, transparent)` | primary button border |
| `--close-bg` | `#f6f8fa` (light) / `#30363d` (dark) | close button fill |
| `--input-bg` | `#f6f8fa` (light) / `#0d1117` (dark) | control fill |
| `--radius` | `16px` | dialog/card corners |
| `--radius-md` | `12px` | close button, controls, primary |
| `--control-h` | `36px` | inline control height (the primary is a fixed 48px `button-primary`, independent of it) |
| `--space-lg` | `24px` | mobile top anchor gap |
| `--gap` | `12px` | mobile max-height reserve |
| `--font-heading-lg` | `1.5rem` | title |
| `--font-label` | `0.92rem` | control text |
| `--icon-title` | `18px` | close glyph (`icons.size.title`) |

## Don't diverge

`modal.css` is vendored verbatim — to change the contract, change it **here in `project-scaffolding`** and re-vendor downstream. The iOS anchoring and scroll-lock rules look redundant until they aren't: each one exists because a real iPhone regression proved it necessary. If your own CSS declares the same selector this file touches (e.g. `.app`, `.card`), use longhand properties or a disjoint media condition — a shorthand property at equal specificity is decided by source order, and can silently override a rule you didn't intend to touch. Streamlit POC spikes are exempt.
