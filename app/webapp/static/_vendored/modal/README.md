# `modal` — the editor `<dialog>` shell

The fleet's canonical **editor modal**: a native `<dialog>` with a `heading-lg` title + an unpainted 34px × close (an icon button, transparent at rest), stacked label/value rows on top-border dividers (value control ≥ 55%), and exactly one full-width primary button whose disabled state is the shared `button` recipe (muted on control fill: 4.93:1 light, 4.08:1 dark; WCAG 2.2 SC 1.4.3 exempts text in an inactive UI component). home-automation ships this one shell across its plug/zone/net-device/camera/AC editors. Contract: `~/.claude/design.md` → `modal` token block + "Component contracts".

## Files

| File | Role |
| --- | --- |
| `modal.css` | Visual + structural contract: the dialog shell, iOS anchoring + scroll-lock rules, header/close, rows, sticky action bar, full-width footer primary. References design tokens only. |
| `modal.html` | Markup skeleton to copy and adapt. |

## How to vendor

1. Copy this `modal/` folder **verbatim** into your app's static dir. Do **not** edit `modal.css` per-app — every iOS rule in it is on-device-validated (home-automation #214/#300/#303).
2. Link the CSS **together with its three companions**, paste the skeleton, and drive it with the native dialog API. `modal.css` does not re-declare the inline control, the primary button or the close — `select-native/select-native.css` owns `.input-native`/`.select-native` (the 36px control), `button/button.css` owns `.button-primary` and its disabled recipe, and `icon-button/icon-button.css` owns the close's unpainted box, glyph states and 44px target:
   ```html
   <link rel="stylesheet" href="/static/_vendored/select-native/select-native.css">
   <link rel="stylesheet" href="/static/_vendored/button/button.css">
   <link rel="stylesheet" href="/static/_vendored/icon-button/icon-button.css">
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
      <h2 tabindex="-1" autofocus>Title</h2>
      <button class="icon-button detail-close" aria-label="Close">…×…</button>
    </div>
    <div class="row"><span>Label</span> <input class="input-native"></div>
    <div class="row"><span>Label</span> <select class="select-native">…</select></div>
    <div class="detail-actions">
      <button class="button-primary detail-save-btn">Save</button>
    </div>
  </div>
</dialog>
```

- The row **label** is the row's first `span` child (`.detail-card .row > span:first-child`): `ink`, weight 600. Any other `span` in the row (a muted note, a fix-it line) is not styled by the modal and keeps your own class's colour and weight. Put the label first and give secondary text its own class.
- Use the **native** dialog API (`showModal()`/`close()`) — never a hand-rolled overlay (the shadcn Dialog rule in `design.md`).
- The **title takes focus on open**: `<h2 tabindex="-1" autofocus>`. The keyboard stays down on a phone and the sheet is announced to a screen reader. A title is not a control, so `modal.css` removes the app-wide `:focus-visible` ring from `dialog h2[tabindex="-1"]` and from nothing else — every control in the dialog keeps its ring. Do not add a local `h2:focus` override.
- Exactly **one** primary action; it starts `disabled` until something changes.
- The disabled recipe (control-fill/muted/line) is part of the contract, inherited from `button-primary` — opacity-based disabling drops sub-AA in both themes. `.detail-save-btn` only adds the footer's full width.
- On mobile the dialog is top-anchored (safe-area aware); on desktop it keeps UA centering. Don't override.

## Required design tokens

| Token | Light value | Used for |
| --- | --- | --- |
| `--card` | `#ffffff` | dialog card / action bar |
| `--ink` | `#1f2328` | text |
| `--muted` | `#656d76` | disabled text (the close glyph colour comes from `icon-button/`) |
| `--line` | `#d1d9e0` | row dividers |
| `--control-border` | `#818b98` (dark `#6e7681`) | inline control borders (WCAG 1.4.11) |
| `--accent-fill` | `#0969da` (dark `#1f6feb`) | primary button fill |
| `--accent-fg` | `#ffffff` | primary button text |
| `--accent-border-strong` | `color-mix(in srgb, var(--accent) 28%, transparent)` | primary button border |
| `--input-bg` | `#f6f8fa` (light) / `#21262d` (dark, `border-muted`) | control fill and disabled primary fill (fleet-config#1368, project-scaffolding#356) |
| `--radius` | `16px` | dialog/card corners |
| `--control-h` | `36px` | inline control height (the primary is a fixed 48px `button-primary`, independent of it) |
| `--space-lg` | `24px` | mobile top anchor gap |
| `--gap` | `12px` | mobile max-height reserve |
| `--font-heading-lg` | `1.5rem` | title |
| `--font-label` | `0.875rem` | control text |
| `--icon-title` | `18px` | close glyph (`icons.size.title`) |

## Don't diverge

`modal.css` is vendored verbatim — to change the contract, change it **here in `project-scaffolding`** and re-vendor downstream. The iOS anchoring and scroll-lock rules look redundant until they aren't: each one exists because a real iPhone regression proved it necessary. If your own CSS declares the same selector this file touches (e.g. `.app`, `.card`), use longhand properties or a disjoint media condition — a shorthand property at equal specificity is decided by source order, and can silently override a rule you didn't intend to touch. Streamlit POC spikes are exempt.
