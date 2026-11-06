# Documents

Settings › **Documents** lists the fleet's sources of truth (the fleet guide, the decision register) and opens each in a reading sheet, rendered live from the file on this PC, Mermaid diagrams included. It renders the same on any harness, because the app reads the repo's file itself.

## What the owner sees

- One row per configured document: its title over the repo it comes from and when the file last changed ("fleet-config · Updated 26 h ago"). A file that cannot be read shows "Unavailable" in place of the time.
- A tap opens the sheet: back chevron to Settings, ✕, the title, the "Updated … · repo" line with **Open on GitHub**, then the document. At phone width (520 px and under) the sheet is the whole screen, clear of the notch and home bar; on a wider screen it is a 900 px sheet. The header, the "Updated" line and the Done footer stay put while only the document scrolls. Tables scroll sideways inside their own box. A diagram is scaled to the sheet and scrolls sideways inside its box when it is still wider, so the page never does.
- **Open on GitHub** opens the file's page on github.com in a new tab. It is hidden when the document's repo has no GitHub remote.
- **Copy Markdown** (beside Open on GitHub, shown once the text has loaded) puts the document's raw file text on the clipboard, byte for byte as the file reads, not the rendered page.
- Settings › Chief has a **Grok Bot directive** row whose Open button shows the `grok-bot-skill-pack` document in this same sheet, stacked on the Chief sheet so Back returns there. The owner reads the directive's updated time and copies the Markdown into Grok Bot when it is newer than the pasted copy. Any control carrying `data-open-document="<id>"` opens that document the same way; an id missing from the configured list shows "no such document" with Retry.
- No document configured: the group is hidden. The list cannot be read: one muted row with Retry, never an empty group.
- A document that cannot be read says why in the sheet ("file not found", the size limit, "Passkey unlock required") with Retry.

## Configuration

`documents` in `config/webapp_config.json` (gitignored; `config/webapp_config.sample.json` has the first entries, including `grok-bot-skill-pack` for the Chief sheet's Grok Bot directive row; the live file needs that entry added by hand):

```json
"documents": [
  {"id": "fleet-guide", "title": "Fleet guide", "repo": "fleet-config", "path": "docs/fleet-guide.md"}
]
```

`id` is a short lowercase slug, unique in the list. `repo` is the folder name of a primary checkout under `projects_dir`. `path` is relative, uses forward slashes and ends in `.md`. An entry that fails these checks is dropped with a warning in the log and the rest still load.

## What can be read

The browser never sends a path. `GET /api/documents` returns `{documents: [{id, title, repo, updated}]}` and `GET /api/documents/{id}` returns one document's text plus `github_url`, so the only thing a request can name is an id from the configured list.

`github_url` is `https://github.com/<owner>/<repo>/blob/HEAD/<path>`, built from the repo's `origin` remote (a plain read of `.git/config`, no `git` spawn) and the configured `path`, percent-encoded. `HEAD` is the ref because github.com redirects it to the default branch. A repo with no `origin`, or a non-GitHub remote, gives `null`, and no local path is ever part of the link.

`src/documents/` is the only place an id becomes a file, in two layers:

- **Entry validation.** `repo` is one bare folder name (no separator, no leading dot, not a `-wt-N` worktree, so a worktree is never read); `path` has no `..`, `.`, empty segment, drive letter, leading slash or backslash.
- **The jail.** The repo folder and the file are resolved with symlinks and junctions followed. The repo folder must sit directly under the fleet root and the file inside the repo folder, so a link pointing out is refused.

A read can fail four distinct ways: no such id (`404 no such document`), file missing (`404 file not found`), outside the jail (`403`), over 512 KB (`413`, never truncated).

The text is gated like the Life OS file reader: Tailscale-only, refused over the public tunnel, passkey-required. The list carries titles and times only, stays on the bearer token and so draws without a passkey prompt. "Last changed" is the file's modification time, a plain `stat` with no `git` spawn.

## Rendering

The sheet body is `renderMarkdown()` in a `.tr-md` box, the same renderer as the Changed files Preview, with `hideComments` on so a generated-section marker line does not show. The reader restyles that box through `.document-body` in `styles/settings.css` to read like GitHub's Markdown preview: 16 px text on a 1.5 line height, 16 px between blocks, h1 and h2 on a hairline rule, code on the subtle surface, zebra tables, and a 780 px measure on a wide sheet. It is built from the design tokens, so it follows light and dark. The rules are scoped to the reader, so the Changed files Preview keeps the shared `.tr-md` look.

`app/webapp/static/shared/mermaid-render.js` is the one shared diagram renderer. `markdown.js` stays DOM-free and renders a ` ```mermaid ` fence as a code block; a view calls `renderMermaidIn(root)` once its HTML is in the page, and each such block becomes an SVG. The Documents sheet and the Changed files Preview call it; Chat does not, so a Mermaid fence in a reply stays a code block.

- **Library.** One pinned build under `static/vendor/` (MIT, the version in the file name so a bump cannot be served stale), loaded by a `<script>` the first time a block needs drawing, never at startup. Re-vendor by replacing the file and the `SCRIPT_URL` in the module together.
- **Safe mode.** `securityLevel: 'strict'` and no auto-start.
- **Theme.** Mermaid's `base` theme fed from the app's colour tokens at draw time; a theme change redraws each diagram from its kept source.
- **Failure.** A block that will not parse, or a script that will not load, keeps its code listing and gains the note "Diagram couldn't be drawn".

Known limits of the shared renderer: no nested lists, images or links between documents (relative links stay plain text, only `https://` links are live), and only a comment that fills a whole line is hidden.

## Usage log

Each row is cloned from a `<template>` that carries the one constant id `settings.document-open`, and Open on GitHub carries the one constant id `document.github`, Copy Markdown `document.copy` and the Chief row `chief.grokbot-directive`; no per-document ids exist. The reading sheet is a dialog over Settings, so its time counts under the Settings view.
