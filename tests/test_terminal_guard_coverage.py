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

import pytest


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


def _all_route_paths(app) -> Set[str]:
    """Every route path template this app serves, however deeply nested.

    ``app.routes`` used to hold every endpoint directly: ``include_router``
    copied the sub-router's routes into the parent's list, so a one-level walk
    saw all of them. FastAPI 0.141 / Starlette 1.6 changed that — each
    ``include_router`` call now appends a single lazy ``_IncludedRouter``
    wrapper and the endpoints live *inside* it, so the same one-level walk
    returns only ``/openapi.json``, ``/docs``, ``/docs/oauth2-redirect``,
    ``/redoc`` and the ``/static`` mount.

    That is #1041: this dev box had 0.136 and CI had 0.141 (``fastapi>=0.110``
    is a lower bound, so the two installs drifted), and on CI this file
    reported that the app "no longer registers" ``/api/board`` — a route the
    phone uses every day and that 2441 other tests hit successfully in the
    same run.

    ``fastapi.routing.iter_route_contexts`` is the accessor FastAPI's own
    OpenAPI generation uses on the new shape, and it yields plain routes
    unchanged, so it is correct on both. Fall back to the flat walk on a
    FastAPI too old to have it.
    """
    try:
        from fastapi.routing import iter_route_contexts
    except ImportError:  # FastAPI < 0.141 — app.routes is already flat.
        return {
            path
            for route in app.routes
            if (path := getattr(route, "path", ""))
        }

    paths: Set[str] = set()
    for context in iter_route_contexts(app.routes):
        # ``context.path`` carries any ``include_router(prefix=...)``, which a
        # route's own ``.path`` does not. It comes back empty for WebSocket
        # routes on 0.141.1, and the PTY stream is one — so fall back rather
        # than lose ``/api/claude-code/sessions/{sid}/ws``.
        path = getattr(context, "path", "") or getattr(
            getattr(context, "route", None), "path", ""
        )
        if path:
            paths.add(path)
    return paths


def _terminal_route_paths(app) -> Set[str]:
    """Route path templates registered under a terminal-grade prefix."""
    return {path for path in _all_route_paths(app) if _under_terminal_prefix(path)}


def _visible_terminal_routes(app) -> Set[str]:
    """``_terminal_route_paths``, but an empty result is an error, not a pass.

    Everything in this file is an argument of the form "walk the routes, and
    require each one to be classified". If the walk itself returns nothing,
    every one of those arguments becomes vacuous: the classification test
    passes having checked nothing, and the two rot-detectors report the
    opposite of the truth — that correct allowlist rows are stale and should
    be deleted. That is exactly what CI did for two days (#1041).

    A route table this file cannot see is an **unknown**, never a pass and
    never "the routes were removed". Say so in its own words, and name the
    two readings so the next person does not start by deleting good rows.
    """
    paths = _terminal_route_paths(app)
    if paths:
        return paths

    import fastapi
    import starlette

    # ASCII only, like the other messages here: read off a Windows console.
    raise AssertionError(
        "Terminal-guard coverage could not be established: walking this app "
        "found no route at all under any prefix in TERMINAL_PREFIXES, out of "
        f"{len(_all_route_paths(app))} route path(s) seen in total.\n\n"
        "This file proves nothing in that state, so it fails rather than "
        "passing. Two readings, in likelihood order:\n"
        "  1. The route walk in _all_route_paths no longer matches how this "
        "FastAPI version exposes routes registered via include_router - that "
        "was #1041. Installed here: "
        f"fastapi {fastapi.__version__}, starlette {starlette.__version__}.\n"
        "  2. Every terminal-grade route really was removed or renamed, in "
        "which case TERMINAL_PREFIXES is what needs updating.\n\n"
        "Check 1 before touching TERMINAL_PREFIXES or DELIBERATELY_TOKEN_ONLY."
    )


def test_every_terminal_prefixed_route_has_an_explicit_guard_level(webapp_client):
    """No route under a gated prefix may resolve to None without saying why."""
    _client, app, _overrides = webapp_client
    from app.webapp.middleware import _terminal_guard_level

    unclassified = sorted(
        path
        for path in _visible_terminal_routes(app)
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

    registered = _visible_terminal_routes(app)

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

    paths = _visible_terminal_routes(app)
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


def test_route_walk_sees_routes_registered_via_include_router():
    """The walk must survive how FastAPI exposes nested routers (#1041).

    Pinned against a hand-built app rather than the real one so the failure
    names the mechanism instead of the symptom. The nesting is the real
    shape: ``server.create_app`` includes ``sessions.router``, which itself
    included ``voice_ocr_tts.router`` at import time - two levels, plus a
    WebSocket route, which is the one ``iter_route_contexts`` reports with an
    empty ``context.path`` on 0.141.1.

    Status by installed version, measured: on fastapi 0.141.1 / starlette
    1.6.0 (what CI installs) this FAILS on the pre-fix walk and passes on the
    current one; on 0.136.1 / 1.0.0 (this dev box today) it passes either way,
    so here it is a guard rather than proof. ``fastapi>=0.110`` is a lower
    bound and both are in range - which is why the guard matters.
    """
    from fastapi import APIRouter, FastAPI

    inner = APIRouter()

    @inner.get("/api/tts/health")
    def _health() -> Dict[str, str]:
        return {}

    outer = APIRouter()

    @outer.get("/api/claude-code/sessions")
    def _sessions() -> Dict[str, str]:
        return {}

    @outer.websocket("/api/claude-code/sessions/{sid}/ws")
    async def _ws(websocket) -> None:  # pragma: no cover - never connected
        pass

    outer.include_router(inner)
    app = FastAPI()
    app.include_router(outer)

    assert _terminal_route_paths(app) == {
        "/api/tts/health",
        "/api/claude-code/sessions",
        "/api/claude-code/sessions/{sid}/ws",
    }


def test_an_invisible_route_table_fails_as_unknown():
    """No routes visible must read as "could not establish", not "stale rows".

    The #1041 wording told a reader to delete fifteen correct allowlist rows.
    An unknown that is phrased as a finding is worse than a plain red: it
    hands you a confident, wrong next action.
    """
    import types

    empty = types.SimpleNamespace(routes=[])

    with pytest.raises(AssertionError) as excinfo:
        _visible_terminal_routes(empty)

    message = str(excinfo.value)
    assert "could not be established" in message
    assert "no longer registers" not in message
    assert "drop the rows" not in message
