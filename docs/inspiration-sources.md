# Inspiration sources

Settings › Connections › **Inspiration** edits the source list of the daily `/inspiration-scan` Job (fleet-config's skill): the newsletters, YouTube channels and X accounts it reads, and a note on what you like. The same sheet shows the latest digest's status and opens the one-time X login. The row shows the number of sources.

## Where the state lives

The list is the skill's `sources.toml` in its machine-local state root (`~/.claude/inspiration-scan/`, or `INSPIRATION_SCAN_ROOT`). It names the people and publications you follow, so it stays private: it is never committed, never logged (a save logs counts only) and only the authenticated app serves it.

The skill owns the format. The launcher loads the skill's own `sources.py` by path (the load Fleet scope uses, see [fleet-scope.md](fleet-scope.md)) and asks its `parse_config` to validate every document before it is written, so the format has one definition. The skill is found under the **fleet-config folder** from Settings › Folders.

## What a save does

A save sends the whole list with the `revision` the sheet loaded, and the server:

1. refuses a stale revision (`409`), so a hand edit made since the sheet opened is never overwritten;
2. checks each entry (a newsletter feed is an `http(s)` address, a channel id is `UC` plus 22 characters, a handle is up to 15 letters, digits or underscores, no entry is listed twice) and then runs the skill's `parse_config` over the result (`422` with the message, file unchanged);
3. merges the shown fields over the entry each row came from, so a key the sheet does not show survives, and keeps `[settings]`, `[profile]` keys other than `likes` and any unknown table as they were;
4. writes the text with `src/inspiration/toml_write.py`, reads it back and refuses if it does not parse to the same document;
5. keeps the previous text as `sources.toml.bak` (one backup, replaced each save) and swaps the new file in atomically.

Comments in the file are not kept: `tomllib` does not return them. A file that does not parse is shown with its error and is never overwritten; fix it by hand.

## API

- `GET /api/inspiration/sources` returns `{available, reason, exists, revision, editable, problem, likes, newsletter, youtube, x, digest}`. Each list entry carries its `index`. `problem` is the validator's message when the file as it stands would stop the scan.
- `PUT /api/inspiration/sources` with `{revision, likes, newsletter, youtube, x}` saves and returns the same payload.
- `GET` / `POST /api/inspiration/login-x` report and start the X login window: `{state, detail, started_at, ended_at}` with `state` one of `idle`, `running`, `closed`, `failed`.

A missing, old or unloadable skill reads `available: false` with a reason and a `503` on `PUT`; it is never an empty list.

## The digest status

The status line reads the newest `digests/YYYY-MM-DD.html` in the state root: its date, the ideas kept, and one line per source type (read of total, with the reason when not read; X skipped for no login says so). The digest has no machine-readable twin, so this is a read of the header the skill's `render.py` writes. When the header is not recognised the sheet says the digest could not be read; it never reports a quiet day. With no digest it says so.

## The X login

`POST /api/inspiration/login-x` starts `scan.py login-x` under fleet-config's own Python (`.venv`), in fleet-config's folder, with no console window. The skill opens its dedicated Chrome profile headed on the X login page; you log in there and close the window. The button polls until the process exits and then says the window closed, or the last line the helper printed when it failed. No credential passes through the app. One window at a time: a second tap while it is open reports the open one. The state is held in the webapp process, so after a webapp restart it reads `idle` even if the window is still open.
