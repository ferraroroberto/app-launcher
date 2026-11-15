# `toast` — the neutral frosted toast

The fleet's one transient message, for the result of a user-initiated command ("Saved", "Opening in Explorer", "Couldn't reach the server"). It is the floating nav bar's glass (translucent card fill, hairline border, `backdrop-filter: blur(20px) saturate(1.25)`), centred 8px above the nav so the nav never hides it, and above every overlay. **Only a real error tints** — a darkened `danger` fill, a `danger` border and `accent-fg` text. Success, info and warning stay neutral: the message says what happened, colour does not. Spec: `~/.claude/design.md` → `toast` component and its contract bullet; feedback altitude in "Async data & feedback". Normalized from app-launcher's "Neutral frosted toast" (ported from home-automation #782).

## Files

| File | Role |
| --- | --- |
| `toast.css` | Visual contract — the frosted surface, centring above the nav, `max-content` sizing, the error tint. References design tokens only. |
| `toast.js` | `showToast(message, kind?, opts?)` / `hideToast()`. Creates the element on first use. |
| `toast.html` | Optional skeleton, for a page that wants the live region in the markup from first paint. |

## How to vendor

1. Copy this `toast/` folder **verbatim** into your app's static dir (`app/webapp/static/_vendored/toast/`). Do **not** edit `toast.css` or `toast.js` per-app.
2. Link the CSS and import the function:
   ```html
   <link rel="stylesheet" href="/static/_vendored/toast/toast.css">
   ```
   ```js
   import { showToast } from '/static/_vendored/toast/toast.js';
   ```
3. Define the tokens below. The nav's two glass tokens (`--tabbar-bg`, `--tabbar-border`) are the ones an app that vendors `nav/` already has.

## Usage

```js
showToast('Saved');                                  // neutral, polite, 2.2 s
showToast('Could not reach the server', 'error');    // tinted, assertive, 4.5 s
showToast('Uploading…', undefined, { ms: 0 });       // stays until the next toast or hideToast()
showToast('Link copied', undefined, { icon: 'copy' }); // a leading glyph from the page's icons sprite
showToast('Sent', undefined, { host: sheetToastEl }); // inside an open modal sheet
showToast('Snoozed', undefined, { action: { label: 'Undo', onClick: undo } }); // one action, 10 s
```

- **One toast at a time.** A new call replaces the one on screen and restarts the timer. Each sheet toast keeps its own timer.
- **Plain text only.** The message is written as a text node (inside `span.toast-text`), so server text needs no escaping. A toast carries no HTML; a message that needs more than one line of text and one button is an inline status or a modal.
- **At most one action.** `opts.action = { label, onClick }` adds one `button-ghost` (`data-track="toast.action"`) after the message, for the undo a reversible command offers. A tap runs `onClick` and hides the toast; the toast stays 10 s by default so the offer can be reached. It is never the only way to an outcome, because the toast expires. The page needs the vendored `button` stylesheet.
- **Optional leading glyph.** `opts.icon` names a glyph in the page's vendored `icons` sprite (`'copy'` draws `#i-copy`); it takes the text colour, so it tints with an error. The page needs the sprite and the `.icon` rule (`icons/README.md`).
- **Inside a modal sheet.** A `<dialog>` opened with `showModal()` sits in the top layer, above any `z-index`, so the page's toast is hidden behind it. A sheet that confirms an action carries its own element, `<div class="sheet-toast" role="status" aria-live="polite" hidden></div>`, as the first child of its `.detail-actions` footer (the vendored `modal`'s sticky footer is its positioned ancestor), and passes it as `opts.host`. It rides 8px above the footer, inset by the footer's 18px padding. `hideToast(host)` hides that one.
- **Only `'error'` is a kind.** Any other value (or none) renders the neutral toast. There is deliberately no success variant: do not add a green one, and do not tint a warning.
- **Live region.** `showToast()` sets `role="status"` and `aria-live` (`assertive` for the error, `polite` otherwise) without moving focus. If you ship the `toast.html` skeleton, that is the element it uses (`id="toast"`).
- **Failure copy is sanitized** — no hostnames, URLs, exception classes or timeout internals in the message; the logs keep the detail (design.md, feedback altitude).
- **Reserve it** for a user-initiated command's progress or result. Passive background status renders inline beside the surface it describes, never as a toast.

## Layout contract

- `position: fixed`, `left: 50%` + `translateX(-50%)`, `bottom` = nav height + 2× nav margin + `env(safe-area-inset-bottom)` + 8px, using nav-tabs.css's own `var(--bottom-tabs-*, fallback)` defaults, so an app that leaves those tokens undefined still lands on the right line.
- `width: max-content` with `max-width: min(100vw - 24px, 560px)`: one line whenever the message fits. Without `max-content` the box shrinks to the 50vw left of its anchor and wraps at half the screen. `text-wrap: balance` splits a longer message evenly.
- `padding: 12px 18px`, `font-size: var(--font-label)`, weight 700, line-height 1.5, centred.
- `z-index: 400`, above a full-screen overlay or modal backdrop.
- `.sheet-toast` keeps the same glass and type but is `position: absolute`, `bottom: calc(100% + 8px)` of its positioned footer, `inset-inline: 18px` with `width: fit-content` and `margin-inline: auto`, so it centres over the footer's buttons inside the sheet.

## Required design tokens

| Token | Light value | Used for |
| --- | --- | --- |
| `--tabbar-bg` | `rgba(255,255,255,0.62)` (dark `rgba(42,45,52,0.55)`) | glass fill (the nav bar's) |
| `--tabbar-border` | `rgba(0,0,0,0.08)` (dark `rgba(255,255,255,0.14)`) | hairline border (the nav bar's) |
| `--ink` | `#1f2328` | text |
| `--deficit` | `#cf222e` (dark `#f85149`) | error border, and the error fill (75% `--deficit` over black) |
| `--accent-fg` | `#ffffff` | error text |
| `--shadow` | `0 8px 24px rgba(31,35,40,0.12)` | the theme shadow (falls back to the nav bar's `0 10px 34px rgba(0,0,0,0.5)` if undefined) |
| `--radius-md` | `12px` | corners |
| `--font-label` | `0.875rem` | text size |
| `--bottom-tabs-height` / `--bottom-tabs-margin` | `61px` / `21px` | optional; fall back to the nav's own defaults |

## Error contrast

The error fill is opaque: `color-mix(in srgb, var(--deficit) 75%, black)`. `--deficit` stays the spec `danger` everywhere, so no adopter overrides it for the toast, and there is no extra token to define. White (`--accent-fg`) text on that fill is measured against WCAG AA's 4.5:1:

| Theme | `--deficit` | Fill | White text on the fill |
| --- | --- | --- | --- |
| Light | `#cf222e` | `#9b1a22` | 8.18:1 |
| Dark | `#f85149` | `#ba3d37` | 5.52:1 |

The fill is opaque, so the ratio does not depend on the page behind the toast. The former translucent fill measured 3.73:1 (light `--deficit`) and 2.55:1 (dark `--deficit`) over a white page. `tests/test_toast_contrast.py` recomputes the ratio from `toast.css` for both themes over every page ground in the gallery and fails below 4.5:1.

## Don't diverge

`toast.css` and `toast.js` are vendored verbatim — to change the contract, change it **here in `project-scaffolding`** and re-vendor downstream. An app that draws its own toast, or tints a success one, is drifting from the spec; adopt this one instead. If your own CSS declares the same selector this file touches (`.toast`), use longhand properties or a disjoint media condition: a shorthand property at equal specificity is decided by source order, and can silently override a rule you didn't intend to touch. Streamlit POC spikes are exempt.
