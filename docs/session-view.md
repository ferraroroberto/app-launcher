# Session view — reference

The session view is the launcher's main interaction surface for a running coding session: one full-screen overlay over the Coding tab, with two modes — **Terminal** and **Chat** — and a shared bar. This page is the durable reference; the README carries a short summary and links here.

Contents: [Opening a session](#opening-a-session) · [The shared bar and menu](#the-shared-bar-and-menu) · [Stop and kill](#stop-and-kill) · [Terminal ⇄ Chat](#terminal--chat) · [Chat mode: reading](#chat-mode-reading) · [Live refresh](#live-refresh) · [Loading older and newer turns](#loading-older-and-newer-turns) · [Answering a question the agent asks](#answering-a-question-the-agent-asks) · [Approving a plan from the phone](#approving-a-plan-from-the-phone) · [Resuming a session](#resuming-a-session) · [Sending from Chat](#sending-from-chat) · [Detached sessions](#detached-sessions) · [Rename and link](#rename-and-link) · [Agent readers and their limits](#agent-readers-and-their-limits)

## Opening a session

Running sessions are listed above the project tiles, each marked with its agent's icon and tagged `⚡ full control` or `☁️ detached`. Each row has one **⋮ kebab** pinned to its right edge and centred against the full row height. It opens a floating menu of icon + label rows:

- **⌨ Terminal** — full-control rows only.
- **💬 Chat** — every row whose agent has a transcript reader.
- **✏️ Rename / link**.
- **✕ Stop-and-kill**.

Tapping the kebab opens the menu and never the session. **Tap the row itself** to open the session view in the mode it was last viewed in:

- A full-control row starts in Terminal on the phone and in Chat on a desktop browser (the desktop's Terminal, its PC window, is one tap away on the mode toggle).
- A detached row whose agent has a transcript reader — Claude, Codex, Grok Build, Pi, Antigravity CLI and GitHub Copilot CLI — opens in Chat.
- **Every** row opens, including a detached one of an agent with no reader: both mode segments are greyed and Chat shows the reason, because the view is where that session's Rename and Stop live.

The Board tab's drill-down drawer offers the same four actions as one row of equal buttons.

## The shared bar and menu

The bar carries *‹* back, the title, an icon-only **Terminal ⇄ Chat** toggle, 🔊 read-aloud, and the **⋮** menu:

- **Rename · Copy link · Stop and kill** — always.
- **Show tool calls**, **Load new** and **Reload** — while Chat is showing.
- **Compact** — for a Claude session. One tap sends `/compact` through the same verified input route as Chat's Send, in either mode, and the toast says whether it landed.
- **Changed files** — for any agent with a transcript reader, in either mode. See [Changed files](#changed-files).

The phone's floating tab bar hides while the session view is open.

## Changed files

The ⋮ menu's **Changed files** lists every file this session's edits created, modified or deleted, in the same panel as the Coding tab's Show changes, so the two viewers match (#1349):

- The summary line says what was read: `3 files · +9 −3 · from this session's transcript`.
- Each file carries a badge — **A** created by the session, **M** modified, **D** deleted — and its `+N −M`. The totals are the sum of the Chat steps' own counts; a failed step counts nowhere.
- Tapping a file opens each of the session's edits to it, in order, labelled *Created* / *Edit 2 of 3* / *Deleted* with the time, each drawn like a Chat step. Claude's diffs are numbered; the other agents record only an edit's own text, so theirs show no line numbers and the panel says so.
- **Built from the transcript, never `git diff`.** The working tree mixes this session's edits with any other work in the folder, and shows nothing once the work is committed or a worktree removed; the transcript still holds after both.
- **Deleted is mostly best effort.** Apart from a Codex patch's own *Delete File* step, the session deletes through shell commands, which record no diff, so **D** usually means "the session edited it and it is no longer on disk". With the project folder itself gone (a removed worktree) nothing is marked deleted, and the panel says why.
- **Bounded:** one read of the transcript per open, and at most its newest part, which the panel says when it applies. Claude's fold parses only its recorded-diff lines (about 0.1 s for a 111 MB file, newest 256 MB). Every other agent's fold runs the same entry reader Chat uses, in 16 MiB windows over at most the newest 64 MiB (the largest Codex rollout on the dev box, 33.8 MB, takes 0.22 s). It does not update itself; ↻ re-reads.
- **Other agents (#1356).** Codex, Pi, Grok, Antigravity and Copilot edits are folded from the steps Chat already draws, skipping any step the harness marked failed, so the totals equal the sum of the Chat steps. Codex edits through `apply_patch`, direct or inside an `exec` script; a patch that Codex rejected (its own `apply_patch verification failed` message) counts nowhere. Codex, Antigravity and Copilot record only some tool failures (see Chat's outcome note), so a failed edit they did not mark still counts. Relative paths (a Codex patch names files from its working folder) are resolved against the session's project folder. Copilot's `apply_patch` tool is not recognised yet (only `edit` and `create`, whose keys were probed).
- A session with no edits, no transcript, or one that has ended each gets its own sentence.

## Stop and kill

Stop asks the agent to quit cleanly with its own command (`/quit`, Copilot's `/exit`) so its shutdown hooks run, waits briefly for the clean exit, then force-terminates as a fallback. The window always closes; there is no confirm, except for the fleet chief.

The quit command is typed as **three separate keystrokes with a beat between them** — Esc, the command, then Enter — because sending them as one burst broke on four of the six agents:

- A TUI that binds Alt-keys reads an Esc arriving together with the following `/` as a single meta-key press and swallows the slash (Pi, Antigravity and GitHub Copilot all did, so the harness saw a bare `quit` and answered it as a prompt).
- A command arriving together with its Enter races the harness's own slash-command popup (Codex left `/quit` sitting in an open popup and never acted on the Enter, so every Codex stop fell through to the force-terminate).

Same bytes, same order, just not glued together.

## Terminal ⇄ Chat

Switching modes never reconnects or reloads anything: the live terminal keeps its socket and scrollback while Chat is up (no repaint, no duplicated output), and Chat keeps its loaded pages while the terminal is up.

A segment the session can't offer is greyed, and a tap on it says why — *Detached session — no terminal* (also shown under a detached chat), or *No transcript reader for <agent>*.

For the terminal itself — the socket, the key bar, the composer and the security model — see [Interactive terminal](../README.md#interactive-terminal-from-the-phone).

## Chat mode: reading

Chat shows the session's whole conversation as a chat-style list that reads on the phone:

- Your typed prompts and the agent's replies are expanded. Each prompt/reply card collapses on tap, and URLs in any text are clickable.
- Everything else — tool calls with their results, thinking, harness plumbing, sub-agent traffic — is folded per run into one line (`3 tool calls · 1 thinking`) you can open, then open item by item.
- Those groups are **hidden by default**. The ⋮ menu's **Show tool calls** reveals them, still folded, so the view opens as a plain you ↔ agent exchange.

### Edit steps open as their diff

An Edit, Write or MultiEdit step (and the other agents' edit and write tools the reader recognises) opens as a unified diff: added lines green, removed lines red, each keeping its `+`/`−` prefix, with the tool's own "updated" line underneath in the quiet result style (#1349).

- **Line numbers only where they are real.** A Claude step uses the diff Claude Code records after the edit ran (`toolUseResult.structuredPatch`), so it shows a line-number gutter and `@@` headers; a Write that created a file counts from line 1. Everything else is worked out from the tool's own old and new text, which carries no file position, so it shows no gutter and separates its hunks with `⋯`, rather than inventing numbers.
- **Counts follow the recorded diff.** For a Claude step the row's `+N −M` comes from that recorded diff, so a Write over an existing file counts the lines it replaced.
- **Capped per step.** A page carries at most 80 lines (6 KB) of each step's diff; **Show full diff** fetches the rest (`GET /api/claude-code/sessions/{sid}/transcript/diff?offset=&n=`, capped at 200 KB like Show changes). Long lines wrap inside the diff box; the page never scrolls sideways.
- A failed step keeps its usual body: it changed nothing, so it shows no diff.

### Failed tool calls

A tool call that failed is marked: a red glyph and a `failed` chip on its row, and a `1 failed` chip on the group's closed header so you see it without unfolding. Nothing is guessed from the text of a result — a `grep` that successfully finds the word "error" is not a failure.

How far the mark can be trusted depends on the agent, and the card says so rather than letting silence read as success — see [Agent readers and their limits](#agent-readers-and-their-limits).

### Errors and empty states

Agents without a history file and missing or unreadable files say so explicitly: "No transcript found" is a different line from "Couldn't read the transcript".

## Live refresh

The conversation keeps itself up to date while you are reading it: new prompts, replies and tool calls appear on their own, with no pull-to-refresh and no tap.

- **Only for the chat you are looking at.** A window showing Terminal does not also fetch chat, a closed session view fetches nothing, and a backgrounded tab or a locked phone stops entirely and does one catch-up fetch when you come back, rather than replaying every tick it missed. On the PC, where several session windows can be open at once, each window refreshes only its own conversation, and only while it is showing Chat.
- **Incremental.** Only what the agent has appended is fetched and added to the bottom, so your scroll position, an open tool-call group and read-aloud are never disturbed. An unchanged session costs the PC one file-size check rather than a re-read (measured flat at ~2.8 ms of server time per check whatever the session's size, versus 5-45 ms to re-read the newest page of a long one).
- **A session that ends** stops refreshing and says so instead of polling a dead session.

Two menu items are the manual escape hatches:

- **Reload** restarts refresh after repeated network failures or a finished session, and re-reads the conversation from scratch.
- **Load new** fetches only what came after the newest turn shown — the live refresh's own read, forced now — and appends it without rebuilding anything. It says *No new messages* briefly when there are none.

On the phone, the same read as Load new is a **pull up** past the bottom of the list, and a **pull down** at the top loads older turns (this also works on a list too short to scroll). Both gestures only read the drag, so ordinary scrolling, iOS's bounce and the Latest pill behave as before.

## Loading older and newer turns

The launcher reads the agent's own history — Claude Code's session JSONL, a Codex rollout, Grok Build's `updates.jsonl` stream, a Pi session JSONL, an Antigravity CLI conversation log, or a GitHub Copilot CLI `events.jsonl` — in **bounded pages**. The newest turns load first, and **Load older** (or scrolling to the top) pulls in the previous page, so a multi-MB session is never read whole.

One tap always surfaces an older prompt or reply. A stretch of heavy tool output (screenshot results are ~1 MB each) could fill a page with nothing but hidden tool calls, so the reader:

- charges a huge line only 64 KB against the page's 2 MiB budget, capped at 16 MiB of real reads per request (worst case measured at ~75 ms);
- keeps fetching on the phone, up to 8 pages or 2.5 s, until a turn arrives;
- when it hits that bound, says on the button how many tool calls it loaded;
- when the file start holds only tool calls, ends the list on *Start of transcript — no older messages*.

## Answering a question the agent asks

When Claude Code calls `AskUserQuestion`, Chat shows the question with its numbered options (bold label, muted description) as its own card, visible even with tool calls hidden, instead of a folded `AskUserQuestion` row.

- While it is the question the session is waiting on, **tap an option** to answer it. One tap sends a single-choice question; a multi-choice or several-question call takes your picks and a **Submit answers**. A single-choice question also takes a typed answer (Claude Code's *Type something*).
- The launcher re-checks against the transcript that the question is still waiting before it types anything, then types the picker's own keystrokes: raw keys over the terminal socket for a full-control session, one console-input call per key for a detached one.
- The card locks until the agent's answer lands in the transcript, then shows what was picked.
- An older question, or one the agent has moved past, is plain history with nothing to tap. So is every card in the Life OS conversation viewer.

## Approving a plan from the phone

A plan the agent asks you to approve is a card too. Claude Code's `ExitPlanMode` shows the whole plan rendered as markdown, like a reply, and how it was answered: *Approved* (or *Approved after your edits*), *Sent back with feedback* with your feedback quoted, *Not approved*, or *Never shown* when the agent called it outside plan mode.

**You can approve or send back a waiting plan from Chat, for a full-control session.**

The transcript can't be trusted to say a plan is waiting (Claude Code can hold the pending call back from its history file until it is answered), and the picker's first option changes with the session's permission mode ("auto-accept edits", or "switch to BYPASS PERMISSIONS" in a skip-permissions session). So Chat reads the **terminal's screen** instead:

1. While the picker is up, a panel under the conversation shows its options exactly as the terminal lists them, with the plan itself when the transcript doesn't show it yet. A waiting plan card points at that panel.
2. Tap a *Yes* option to approve, or type what should change and tap **Send back**.
3. Right before it types anything, the launcher reads the screen again, and it sends nothing if that option number no longer carries the label you tapped or the terminal is mid-answer. A tap can never approve into a mode you didn't see.

When Chat can't see the picker it says so and to answer in the terminal. For a detached session, which has no screen the launcher can read, it says to answer in the PC console.

## Resuming a session

Resuming is a searchable card. It appears while a full-control Claude session's terminal shows Claude Code's own `/resume` picker, or when you type `/resume` in Chat's composer (which then sends nothing to the terminal). It also comes up for a session launched with `--resume`, which opens on that picker with no transcript yet, and takes the place of the *No transcript found* line.

- Chat lists the project's conversations newest first, each by its title (the custom one, else Claude's own, else the first prompt) and how long ago it was active, with a search box that filters as you type. Nothing else from those transcripts reaches the phone. Print-mode (`claude -p`) runs are left out, because `/resume` can't resume them.
- **Tap one to resume it.** The launcher checks the list and the screen again first.
- **Picker up:** it never leaves the picker with Escape, because Escape on a `--resume` launch's picker exits Claude Code. Instead it moves the picker's highlight down one row at a time, reads the screen after each step, and presses Enter only once the highlighted row's title can be nothing but the one you tapped. If no row can be told apart that way (another session shares the title, or the row shows too little of it), it selects nothing and says to pick in the terminal.
- **No picker up:** it types `/resume <id>`, which resumes exactly that conversation (probed on Claude Code 2.1.283).
- It then reads the result back off the terminal, and the toast says *Resumed*, *could not find that session* or *Sent: check the terminal*. If the picker has closed by the time you tap, nothing is sent.

## Sending from Chat

The same composer as the terminal — tall predictive textarea plus mic · keys / image · send — is docked under the chat. **➤ Send** posts the message to the session's input route, and the sent turn appears a few seconds later through the same live refresh as everything else, appended without rebuilding the pane. A full-control session sends this way even while its terminal is connected, because Chat can't show the terminal and the route's verdict is the only feedback it has.

### What the toast says

The toast reports what actually happened rather than just "sent":

| Toast | Meaning |
|---|---|
| *Sent* | The session-host confirmed the submit. |
| *Sent, not confirmed* | Enter went in but nothing verified it. |
| *Queued* | The agent was still busy; Enter goes in once it settles. |
| *Not submitted* (red) | The text reached the agent but Enter never did. |
| *Send failed* | The terminal never echoed it, the console did not take it, or the session exited. The text is kept to retry. |

### Keyboard, attachments and keys

- **Ctrl+Enter sends** on a desktop browser with a real keyboard (⌘+Enter on a Mac). The plain return key still inserts a newline everywhere — multi-line prompts are the normal case, and a phone's on-screen keyboard has no Ctrl or Cmd key, so nothing about the soft keyboard changes. The shortcut goes through the same Send as a tap, so on a session whose Send is greyed out it says why rather than sneaking the message through. Chat mode only; the terminal keeps its own input path.
- The **image button** attaches a file the same way it does in the terminal: the file is stored next to the project and its path is added to the message for review, so attach works for a detached session too. For a full-control session, Send holds Enter until Claude Code has turned an attached image path into its `[Image #N]` attachment, as the terminal does, so the conversion can't swallow it.
- The **⌨ keys** drive a full-control session's live terminal even while Chat is showing (answer a y/n prompt without switching) once Terminal has been shown for that session. A detached session has no terminal, so its keys are greyed out.

## Detached sessions

Chat never needs the launcher's terminal capture, so a detached session of a readable agent started on the PC reads exactly the same from the phone. A detached row of an agent with no transcript reader has no Chat item.

For a detached session, Send types the text into the session's console window on the PC and presses Enter, so a long detached run can be steered from the phone without converting it to a full-control session. Its chat shows *Detached session — no terminal. Delivery is typed into the PC console and not confirmed.* above the composer, and the placeholder reads *Message for the agent*.

- **Delivery is unconfirmed by design.** A detached console has no output stream the launcher can read, so the API reports `delivered: "unconfirmed"` (never `true`) and the toast says *Sent, not confirmed* — check the console, or the chat after its reload, if it matters.
- **Which agents.** Send is enabled only for agents a recorded probe proved take console input this way, and all six do: Claude Code, Codex, Pi, GitHub Copilot and Antigravity, plus Grok Build (its probe was blocked on a device-code login the first time round, never on a failure). An agent added later without a probe keeps the composer usable but greys ➤ Send out.
- **A tap on a greyed Send says why.** It is `aria-disabled`, not a genuinely disabled button, because a disabled button fires no tap at all on a phone — the reason would be stranded in a hover tooltip no phone can show.
- **Stale session-host.** If the PC's session-host is still running a build from before detached input, the toast says *session-host restart needed* (with the running build's sha) rather than a bare HTTP error.
- **Newlines.** A newline in the message becomes a soft line break in the agent's composer; the one Enter that submits is sent separately after a short settle.

## Rename and link

The menu's pencil opens **Rename / link** for Claude and Codex full-control sessions:

- Claude exposes and copies the provider-native `claude.ai/code/session_…` remote-control URL captured from its terminal.
- Codex says **Not available yet**, because its local sessions currently have no web URL.

Detached sessions and other agents keep the rename-only dialog.

## Agent readers and their limits

The reader for each agent, and how much of a tool failure it can see:

| Agent | Reads | Tool failures recorded |
|---|---|---|
| Claude Code | session JSONL | all |
| Grok Build | `updates.jsonl` stream | all |
| Pi | session JSONL | all |
| Antigravity CLI | conversation log | some — a tool that could not run is recorded; a shell command that merely exits non-zero is not |
| GitHub Copilot CLI | `events.jsonl` | some — same as Antigravity |
| Codex | rollout | none |

Where a reader records all failures, an unmarked call really did work. For Antigravity CLI, GitHub Copilot CLI and Codex, an opened card whose call is unmarked says the outcome was never recorded.
