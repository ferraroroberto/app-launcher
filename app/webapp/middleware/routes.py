"""Which paths skip auth, and which are terminal-grade (Tailscale, passkey)."""

from __future__ import annotations

from typing import Callable, Optional, Tuple

AUTH_EXEMPT_PREFIXES = ("/static/", "/healthz")
AUTH_EXEMPT_EXACT = frozenset({"/", "/healthz", "/api/login"})


def _is_webhook_hook_path(path: str) -> bool:
    """``POST /api/jobs/<id>/hook`` (issue #73) authenticates itself via the
    job's provider-specific signature, never the bearer token — an external
    service (GitHub, Stripe, …) can't carry it, and the whole point is a URL
    that's safe to hand to a third party in plaintext.
    """
    return path.startswith("/api/jobs/") and path.endswith("/hook")

# Gated-endpoint inventory driving _terminal_guard_level, as a table of
# ``(predicate, level, comment)`` instead of an accreting if-chain (one check
# per feature/issue number) — a new gated endpoint is one row here, and the
# full inventory stays scannable in one place instead of spread across prose
# comments. Order matters only in that the first matching predicate wins;
# in practice every predicate here targets a disjoint path shape.
#
# The rule a new route is measured against: anything that spawns a
# coding session, or reads or injects terminal-grade content, is passkey-gated.
# Starting an app or a job (/api/apps, /api/jobs) is ordinary launcher work
# and stays on the bearer token.
_TerminalGuardRule = Tuple[Callable[[str], bool], str, str]

_TERMINAL_GUARD_RULES: Tuple[_TerminalGuardRule, ...] = (
    (
        lambda p: p.startswith("/api/webauthn/"),
        "tailnet",
        "WebAuthn ceremony endpoints — Tailscale-only; no passkey check (that's what they issue).",
    ),
    (
        lambda p: p.startswith("/api/claude-code/sessions/") and p.endswith("/image"),
        "passkey",
        "Coding-tab image paste into a live PTY.",
    ),
    (
        lambda p: p.startswith("/api/claude-code/sessions/") and p.endswith("/large-file"),
        "passkey",
        "Compose-bar large-file attach (#1430): writes a file of up to "
        "large_upload_max_mb into a session's project, the same reach as /image.",
    ),
    (
        lambda p: p.startswith("/api/claude-code/sessions/") and p.endswith("/input"),
        "passkey",
        "Board drill-down (#301): reply proxy writes into a live PTY.",
    ),
    (
        lambda p: p.startswith("/api/claude-code/sessions/") and "/pending-input/" in p,
        "passkey",
        "Chat queued chip: discarding a pending message presses keys in a live "
        "PTY, the same write power as /input.",
    ),
    (
        lambda p: p.startswith("/api/board/sessions/") and p.endswith("/exchange"),
        "passkey",
        "Board drill-down (#301): last-exchange surfaces transcript text (terminal-grade content).",
    ),
    (
        lambda p: p.startswith("/api/claude-code/sessions/") and p.endswith("/transcript"),
        "passkey",
        "Coding-tab transcript view (#953): the whole conversation, terminal-grade content.",
    ),
    (
        lambda p: p.startswith("/api/claude-code/sessions/") and p.endswith("/answer"),
        "passkey",
        "Chat question card (#1149): types an answer into a live PTY or console — "
        "the same write power as /input.",
    ),
    (
        lambda p: p.startswith("/api/claude-code/sessions/") and p.endswith("/plan-picker"),
        "passkey",
        "Chat plan card (#1151): reads the plan picker, and the plan, off a live "
        "terminal screen — terminal-grade content.",
    ),
    (
        lambda p: p.startswith("/api/claude-code/sessions/") and p.endswith("/plan-answer"),
        "passkey",
        "Chat plan card (#1151): types a plan answer into a live PTY — the same "
        "write power as /input.",
    ),
    (
        lambda p: p.startswith("/api/claude-code/sessions/") and p.endswith("/resume-sessions"),
        "passkey",
        "Chat resume card (#1300): lists the project's conversations by title "
        "and first prompt, read from the transcripts: terminal-grade content.",
    ),
    (
        lambda p: p.startswith("/api/claude-code/sessions/") and p.endswith("/resume"),
        "passkey",
        "Chat resume card (#1300): types `/resume <id>` into a live PTY, the "
        "same write power as /input.",
    ),
    (
        lambda p: p.startswith("/api/claude-code/sessions/") and p.endswith("/context"),
        "passkey",
        "Overlay context ring (#1223): reads a live terminal screen (the same read "
        "as /plan-picker) — terminal-grade content, however small the answer.",
    ),
    (
        lambda p: p.startswith("/api/claude-code/sessions/") and p.endswith("/transcript/entry"),
        "passkey",
        "Coding-tab transcript copy (#985): one turn's uncapped text — the same "
        "sensitivity as /transcript above, and a distinct row because it does not "
        "end with '/transcript' so the rule above does not already cover it.",
    ),
    (
        lambda p: p.startswith("/api/claude-code/sessions/") and p.endswith("/transcript/diff"),
        "passkey",
        "Chat per-step diffs (#1349): one edit's whole diff out of the transcript, "
        "fetched on demand: the transcript's own sensitivity.",
    ),
    (
        lambda p: p.startswith("/api/claude-code/sessions/") and p.endswith("/changed-files"),
        "passkey",
        "Session Changed files (#1349): every file a session edited, with counts, "
        "folded from the transcript: the transcript's own sensitivity.",
    ),
    (
        lambda p: p.startswith("/api/claude-code/sessions/") and p.endswith("/links"),
        "passkey",
        "Session Links: every URL a session's transcript mentioned, "
        "credential-looking query strings removed: the transcript's own sensitivity.",
    ),
    (
        lambda p: p.startswith("/api/claude-code/sessions/") and p.endswith("/background-tasks"),
        "passkey",
        "Session Background tasks (#1955): what a session launched in the background "
        "and a task's output tail, read from its own transcript and task files: "
        "terminal-grade content.",
    ),
    (
        lambda p: p.startswith("/api/claude-code/sessions/") and p.endswith("/changed-files/diff"),
        "passkey",
        "Session Changed files (#1349): one file's edits out of the transcript — "
        "a distinct row, since it does not end with '/changed-files'.",
    ),
    (
        lambda p: p.startswith("/api/claude-code/sessions/") and p.endswith("/changed-files/content"),
        "passkey",
        "Changed files' File view (#1689): one changed file's text off the disk, "
        "jailed to the session's own edited set: terminal-grade content.",
    ),
    (
        lambda p: p.startswith("/api/claude-code/sessions/") and p.endswith("/changed-files/open"),
        "passkey",
        "Changed files' Open on PC (#1689): opens a changed file in the PC's editor. "
        "The route itself also refuses anyone but the PC (403).",
    ),
    (
        lambda p: p.startswith("/api/claude-code/sessions/") and p.endswith("/transcript/image"),
        "passkey",
        "Chat transcript images (#1265): a pasted image or screenshot out of the "
        "transcript, decoded on demand — the transcript's own sensitivity.",
    ),
    (
        lambda p: p.startswith("/api/claude-code/sessions/") and p.endswith("/transcript/file"),
        "passkey",
        "Chat sent files (#1530): a file a Claude SendUserFile call named, read "
        "from disk through the outbox's guards: the transcript's own sensitivity.",
    ),
    (
        lambda p: p.startswith("/api/claude-code/sessions/") and (
            p.endswith("/outbox") or "/outbox/" in p
        ),
        "passkey",
        "Chat sent files (#1530): a session's outbox list and one file's bytes. "
        "The POST that fills it is loopback-only in its handler, so this row "
        "only ever admits the phone to read.",
    ),
    (
        lambda p: p.startswith("/api/board/proof/"),
        "passkey",
        "Board Done proof chip: a merged UI PR's walkthrough, real-app screenshots "
        "read from the machine-local store: the transcript's own sensitivity.",
    ),
    (
        lambda p: p == "/api/board/issues/start",
        "passkey",
        "Board drill-down (#301): issue-start spawns a coding session.",
    ),
    (
        lambda p: p == "/api/board/owner-checks/answer",
        "passkey",
        "Board owner checks: removes labels and posts comments on GitHub with "
        "the owner's own gh account.",
    ),
    (
        lambda p: p.startswith("/api/board/chief/") or p == "/api/board/chief-plan/prune",
        "passkey",
        "Fleet chief (#245): ensure spawns + types into a PTY, settings steer "
        "it — both terminal-grade; /answered (#1487) records what the answer "
        "sheet typed into it; /chief-plan/prune (#1866) rewrites the plan "
        "file the chief steers by. Loopback (the daily job, the /chief skill) "
        "bypasses like everything else.",
    ),
    (
        lambda p: p == "/api/transcribe" or p.startswith("/api/transcribe/"),
        "passkey",
        "Voice dictation (#165/#168): the recording is as sensitive as terminal "
        "input — covers the single-shot proxy and the streamed/SSE session proxy.",
    ),
    (
        lambda p: p == "/api/ocr",
        "passkey",
        "Screenshot OCR (#171): extracted text drops straight into the terminal compose bar.",
    ),
    (
        lambda p: p in ("/api/tts/speak", "/api/tts/summarize"),
        "passkey",
        "Read-aloud hub TTS (#203/#206) + summarize-&-read (#210): both POST the "
        "agent's last reply — terminal content. /api/tts/health stays token-only "
        "so the SPA can decide button visibility even off-tailnet.",
    ),
    (
        lambda p: p == "/api/life-os/file" or p.startswith("/api/life-os/file/"),
        "passkey",
        "Life OS private-content browser (#102): file read/delete/rename, and "
        "the conversation viewer's parsed read /file/transcript (#1119), "
        "surface gitignored private knowledge. The skills list stays token-gated.",
    ),
    (
        lambda p: (
            p == "/api/life-os/recap/launch" or
            # exactly /skills/{id}/launch — /conversations/launch has its own row
            (p.startswith("/api/life-os/skills/") and p.endswith("/launch")
             and p.count("/") == 5)
        ),
        "passkey",
        "Life OS skill launch (#102) + weekly-recap launch (#167): both spawn a "
        "coding session, so they are terminal-grade like /api/board/issues/start "
        "(#1036). The Life OS tab sends X-Terminal-Token on both.",
    ),
    (
        lambda p: (
            p.startswith("/api/life-os/channels/")
            and (p.endswith("/launch") or p.endswith("/restart"))
        ),
        "passkey",
        "Life OS Telegram channel launch (#1366) and restart (#1675): each "
        "spawns a coding session with a chat bot attached (a restart stops the "
        "old one first), so they are terminal-grade like the skill launch "
        "above. The profile list (GET /api/life-os/channels) carries no path or "
        "secret and stays token-gated.",
    ),
    (
        lambda p: p.startswith("/api/life-os/channels/") and p.count("/") == 4,
        "passkey",
        "Life OS Telegram bot settings (#1902): PATCH rewrites a bot's profile "
        "(its skill, relay switch and model), which steers what the next "
        "launch hands the bot, so it is as terminal-grade as the launch. Exactly "
        "/channels/{id}; the launch, restart and relay rows beside it keep "
        "their own suffix.",
    ),
    (
        lambda p: p.startswith("/api/life-os/channels/") and "/relay/" in p,
        "passkey",
        "Life OS Telegram relay (#1639): finds, resumes and steers a skill's "
        "conversations and reads their replies — terminal-grade. The routes "
        "also refuse every caller but the PC itself and demand the profile's "
        "relay token, so this row is the off-box backstop, not the gate.",
    ),
    (
        lambda p: p.startswith("/api/life-os/skills/") and p.endswith("/files"),
        "passkey",
        "Life OS per-skill file tree (#102) — same sensitivity as the file endpoint above.",
    ),
    (
        lambda p: (
            p.startswith("/api/life-os/skills/") and
            (p.endswith("/conversations") or p.endswith("/conversations/launch")
             or p.endswith("/conversations/resume-latest"))
        ),
        "passkey",
        "Life OS conversation index (#727): the digest — topic and summary "
        "— is a distillation of terminal content, so it is gated exactly "
        "like the captures it summarises. Resume-latest (#1503) reattaches "
        "to one, so it carries the same gate.",
    ),
    (
        lambda p: p == "/api/life-os/archived" or p.startswith("/api/life-os/archived/"),
        "passkey",
        "Life OS archived conversations: the zombie cleanup's backup holds "
        "captures, so listing, reading and restoring them is gated like the "
        "captures themselves.",
    ),
    (
        lambda p: p == "/api/life-os/conversations/search",
        "passkey",
        "Life OS conversation search (#727): searches the full capture text, so "
        "a hit can quote anything the terminal ever showed.",
    ),
    (
        lambda p: p.startswith("/api/screen/"),
        "passkey",
        "Screen view (#1520): the PC's desktop, live — whatever is on screen, "
        "terminals included, so terminal-grade. The /api/screen/ws stream is "
        "a WebSocket this middleware never sees; desktop_screen.screen_ws "
        "re-applies these legs inline.",
    ),
    (
        lambda p: p.startswith("/api/ports/") and p.endswith("/kill"),
        "tailnet",
        "Listener kill (#1002): force-kills whatever process owns the port, "
        "including :8446 and every live PTY under it — the same blast radius "
        "as the rows above, so it carries the same reachability requirement. "
        "No passkey — /api/ports/probe stays token-only so the Apps tab can "
        "still render the listeners panel off-tailnet, like /api/tts/health.",
    ),
    (
        lambda p: p.startswith("/api/system/restart/"),
        "tailnet",
        "PC load Restart: stops one allowlisted process (src/process_restart.py) "
        "by its id alone, resolved to pids server side. Far narrower than the "
        "listener kill above, so the same tailnet reachability without a passkey; "
        "the phone's health read (/api/system/health) stays token-only.",
    ),
)


def _terminal_guard_level(path: str) -> Optional[str]:
    """Classify a request path's terminal-gating requirement.

    ``"passkey"`` — Tailscale-only **and** a valid passkey terminal token.
    ``"tailnet"`` — Tailscale-only (the WebAuthn ceremony endpoints).
    ``None``      — not a terminal endpoint; normal bearer-token rules apply.

    Driven by :data:`_TERMINAL_GUARD_RULES` (issue #408).
    """
    for predicate, level, _comment in _TERMINAL_GUARD_RULES:
        if predicate(path):
            return level
    return None
