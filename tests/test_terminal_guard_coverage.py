"""Every route under a terminal-grade prefix must be classified explicitly (#999).

``middleware._terminal_guard_level`` returns ``None`` for a path no row in
``_TERMINAL_GUARD_RULES`` matches, and ``None`` means "not a terminal endpoint
— ordinary bearer-token rules apply". That default **fails open**: a new route
added next to gated siblings silently inherits *less* protection than they
have, and nothing says so until a human reads the guard table side-by-side
with the router.

Two of those slipped through within two days of each other:

* #997 — the client side: ``sessions.js`` sent no ``X-Terminal-Token`` on
  ``/input``.
* #985 / PR #998 — the server side, and the dangerous direction: the new
  ``GET /api/claude-code/sessions/{sid}/transcript/entry`` matched no row,
  because the sibling ``/transcript`` row keys on a path *suffix* that
  ``.../transcript/entry`` does not satisfy. Uncapped turn text would have
  shipped behind the bearer token alone. Caught in review, not by a test.

So: enumerate the routes FastAPI actually registered under each terminal-grade
prefix and require every one of them to resolve to an explicit level — unless
it is named in ``DELIBERATELY_TOKEN_ONLY`` below with a reason. A new route
under a gated prefix now has to opt in deliberately: get itself classified, or
get itself allowlisted and say why.
"""

from __future__ import annotations

import re
from typing import Dict, Set


# Prefixes whose neighbourhood is terminal-grade: each already contains at
# least one row in ``_TERMINAL_GUARD_RULES``, so a *new* sibling under one of
# them is far more likely to need gating than not. Matched as "the prefix
# itself, or anything below it" — ``/api/board`` covers ``/api/board`` and
# ``/api/board/dispatch``, and does not reach an unrelated ``/api/boardgames``.
TERMINAL_PREFIXES = (
    "/api/claude-code/sessions",  # live PTY sessions: input, image, transcript
    "/api/board",                 # drill-down, dispatch, fleet chief
    "/api/life-os",               # private knowledge + skill spawns
    "/api/transcribe",            # voice dictation into the compose bar
    "/api/ocr",                   # screenshot OCR into the compose bar
    "/api/tts",                   # read-aloud of the agent's last reply
    "/api/webauthn",              # the ceremony that issues terminal tokens
    "/api/ports",                 # listener kill reaches :8446 and every PTY
    "/api/system-map",            # rendered fleet topology
)

# Routes under a prefix above that are deliberately left on the ordinary
# bearer token. One row per route, each with the reason — the same
# comment-per-row convention ``_TERMINAL_GUARD_RULES`` itself follows. Adding
# a row here is a decision, which is the point: the alternative used to be
# silence.
DELIBERATELY_TOKEN_ONLY: Dict[str, str] = {
    "/api/claude-code/sessions": (
        "Session *list* — ids, titles, agent, cwd. No conversation text, and "
        "the Coding tab must render off-tailnet to explain why the terminal "
        "is unreachable (see terminal_reachability)."
    ),
    "/api/claude-code/sessions/{sid}/ws": (
        "The PTY stream itself — gated, but not here: BaseHTTPMiddleware "
        "never sees a WebSocket handshake, so proxy_session_ws enforces "
        "tailnet + the ?tt= terminal token inline before accepting."
    ),
    "/api/claude-code/sessions/{sid}/stop": (
        "Lifecycle, not content: closes the mirror window and asks the "
        "session-host to quit. Surfaces no terminal text and injects none."
    ),
    "/api/claude-code/sessions/{sid}/rename": (
        "Sets the manual title override (#458). Writes a label, not input to "
        "the PTY, and reads nothing back."
    ),
    "/api/claude-code/sessions/{sid}/mirror": (
        "Opens/focuses the PC mirror window (#282) — a desktop-only window "
        "action; the terminal it shows is gated on its own /ws handshake."
    ),
    "/api/board": (
        "The kanban board: GitHub issue/PR metadata plus session pointers. "
        "The terminal-grade drill-downs beside it (/exchange, /dispatch, "
        "/issues/start, /chief/*) each carry their own passkey row."
    ),
    "/api/board/github/refresh": (
        "Forces a refresh of the cached GitHub issue/PR metadata the board "
        "already serves token-gated — no session or transcript access."
    ),
    "/api/life-os/skills": (
        "Skill list — folder ids and public SKILL.md titles. The private "
        "subtrees behind them (/files, /conversations) are passkey-gated."
    ),
    "/api/life-os/favorites": (
        "Stars a skill (#1070): writes a list of skill *ids* to the webapp "
        "config and echoes it back. Strictly less sensitive than the "
        "/api/life-os/skills list it reorders, which is itself deliberately "
        "token-only above - no private subtree is read, nothing is spawned, "
        "and no PTY is touched. Its Coding twin POST "
        "/api/claude-code/favorites sits outside every terminal prefix and "
        "is token-only for the same reason. Worst case for a bearer-token "
        "holder is reordering a list they can already read in full."
    ),
    "/api/life-os/recap-status": (
        "One stat() of the recap ledger — staleness state for the tab tile, "
        "no ledger content."
    ),
    # The two rows below record the status quo, and are the only two this
    # file is genuinely unsure about (#999). Both spawn a coding session,
    # which is exactly what earns /api/board/issues/start its passkey row —
    # but it also matches what /api/apps/{id}/launch and /api/jobs/{id}/run
    # already do on the bearer token alone, outside any terminal prefix. So
    # the fleet has two defensible readings: "spawning is terminal-grade" or
    # "spawning is ordinary launcher work, reading/injecting is what needs a
    # passkey". Re-gating is a client change too (the Life OS tab would have
    # to send X-Terminal-Token, cf. #997) and no test on this box can catch
    # getting that wrong — it reproduces only on a phone with the passkey
    # gate configured. Pinned as-is, deliberately, for the owner to settle.
    "/api/life-os/skills/{skill_id}/launch": (
        "Spawns a life-os session (#102), like /api/board/issues/start which "
        "IS passkey-gated. Token-only today; see the note above."
    ),
    "/api/life-os/recap/launch": (
        "Spawns a weekly-recap review session (#167). Same shape and same "
        "open question as the skill launch above."
    ),
    "/api/tts/health": (
        "Health probe only — the SPA decides read-aloud button visibility "
        "off-tailnet. Stated as deliberate in the /api/tts/speak guard row."
    ),
    "/api/ports/probe": (
        "Read-only listener list — the Apps tab renders its listeners panel "
        "off-tailnet. Stated as deliberate in the /api/ports/{port}/kill row."
    ),
    "/api/system-map/status": (
        "Visibility probe only — the SPA decides whether to show the section "
        "off-tailnet. Stated as deliberate in the /api/system-map/image row."
    ),
}

# ``{sid}`` etc. are FastAPI templates; ``_terminal_guard_level`` matches on a
# concrete request path, so substitute a stand-in segment before asking it.
_PARAM = re.compile(r"\{[^}]+\}")
_SAMPLE_SEGMENT = "sample"


def _under_terminal_prefix(path: str) -> bool:
    return any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in TERMINAL_PREFIXES
    )


def _concrete(path: str) -> str:
    return _PARAM.sub(_SAMPLE_SEGMENT, path)


def _terminal_route_paths(app) -> Set[str]:
    """Route path templates registered under a terminal-grade prefix."""
    return {
        path
        for route in app.routes
        if (path := getattr(route, "path", "")) and _under_terminal_prefix(path)
    }


def test_every_terminal_prefixed_route_has_an_explicit_guard_level(webapp_client):
    """No route under a gated prefix may resolve to None without saying why."""
    _client, app, _overrides = webapp_client
    from app.webapp.middleware import _terminal_guard_level

    unclassified = sorted(
        path
        for path in _terminal_route_paths(app)
        if _terminal_guard_level(_concrete(path)) is None
        and path not in DELIBERATELY_TOKEN_ONLY
    )
    assert not unclassified, (
        # ASCII only: this message is read off a Windows console, where a
        # non-ASCII dash comes back as a replacement char and buries the point.
        "These routes sit under a terminal-grade prefix but match no row in "
        "middleware._TERMINAL_GUARD_RULES, so they ship behind the ordinary "
        "bearer token - less protection than their neighbours (#985/#999):\n  "
        + "\n  ".join(unclassified)
        + "\n\nAdd a row to _TERMINAL_GUARD_RULES, or - if the route really is "
        "safe on the bearer token alone - add it to DELIBERATELY_TOKEN_ONLY in "
        "this file with the reason."
    )


def test_token_only_allowlist_has_no_stale_rows(webapp_client):
    """Every allowlisted path still exists, and still resolves to None.

    Without this the allowlist rots in both directions: a row for a deleted
    route lingers as false documentation, and a row for a route that has since
    been gated claims the opposite of what the guard table now says.
    """
    _client, app, _overrides = webapp_client
    from app.webapp.middleware import _terminal_guard_level

    registered = _terminal_route_paths(app)

    vanished = sorted(set(DELIBERATELY_TOKEN_ONLY) - registered)
    assert not vanished, (
        "DELIBERATELY_TOKEN_ONLY names routes this app no longer registers - "
        f"drop the rows: {vanished}"
    )

    now_gated = sorted(
        path
        for path in DELIBERATELY_TOKEN_ONLY
        if _terminal_guard_level(_concrete(path)) is not None
    )
    assert not now_gated, (
        "These paths are allowlisted as deliberately token-only but now match "
        "a _TERMINAL_GUARD_RULES row - the allowlist reason is stale, drop the "
        f"row: {now_gated}"
    )


def test_guard_rules_still_classify_their_own_prefixes(webapp_client):
    """Sanity pin: the enumeration above is only meaningful if the prefixes
    it walks really do contain gated routes. A prefix that stopped matching
    anything would silently turn this file into a no-op."""
    _client, app, _overrides = webapp_client
    from app.webapp.middleware import _terminal_guard_level

    paths = _terminal_route_paths(app)
    barren = sorted(
        prefix
        for prefix in TERMINAL_PREFIXES
        if not any(
            (path == prefix or path.startswith(prefix + "/"))
            and _terminal_guard_level(_concrete(path)) is not None
            for path in paths
        )
    )
    assert not barren, (
        "These TERMINAL_PREFIXES no longer contain a single gated route, so "
        "requiring classification under them proves nothing - either the "
        "routes moved (update the prefix) or the gating was lost: "
        f"{barren}"
    )
