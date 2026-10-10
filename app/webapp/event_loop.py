"""Event-loop shim for uvicorn: Windows selector loop (issue #388) and the GIL
switch interval (issue #1492).

On Windows, asyncio's default proactor event loop closes its listening
socket the moment ``accept()`` raises any ``OSError`` (see CPython's
``proactor_events.py:_start_serving``'s accept loop) -- and a client
aborting a connection mid-handshake (a browser dropping the socket, a
phone roaming off Wi-Fi) surfaces as exactly such an ``OSError`` (WinError
64, "The specified network name is no longer available"). One aborted
client and the listener is gone; the process stays alive but every
subsequent connection fails, which is the ":8445 unresponsive until tray
restart" wedge (#386/#388).

The selector event loop's accept path has no such failure mode -- verified
empirically: 800 concurrent aborted connections against a bare
``SelectorEventLoop`` server left it accepting fine, while the same abuse
killed a ``ProactorEventLoop`` server after ~20. The webapp process spawns
no asyncio subprocesses in-process (``WebappManager`` shells out via plain
``subprocess.Popen``, never ``asyncio.create_subprocess_*``), so the
selector loop's lack of subprocess support is a non-issue here.

Wired into every place that spawns ``app.webapp.server:app`` under
uvicorn -- ``--loop app.webapp.event_loop:selector_loop_factory`` for CLI
invocations (``WebappManager._build_command``, ``webapp.bat``, the e2e
autoboot spawn in ``tests/e2e/conftest.py``, and the named-tunnel boot in
``scripts/run_named_tunnel.py``) or
``loop="app.webapp.event_loop:selector_loop_factory"`` for the
programmatic ``uvicorn.run()`` call in ``app/cli/commands/webapp_cmd.py``.
Keep all of them pointed at the same dotted path below.

For a *custom* ``--loop``/``loop=`` value (anything outside uvicorn's
built-in ``none``/``auto``/``asyncio``/``uvloop`` names), uvicorn imports
the target and uses it directly as the final zero-arg
``Callable[[], asyncio.AbstractEventLoop]`` passed to ``asyncio.run`` --
unlike the built-in names, it is *not* called with a ``use_subprocess=``
kwarg first (that indirection only applies to the built-in factories in
``uvicorn.config.LOOP_FACTORIES``). So ``selector_loop_factory`` below
must itself return an *instantiated* loop, not a loop class.
"""

from __future__ import annotations

import asyncio
import logging
import sys

logger = logging.getLogger(__name__)

# GIL hand-off latency (issue #1492). The webapp renders terminals with pure-
# Python ``pyte`` in worker threads (``board_exchange._terminal_rows`` is
# ~0.85 s of CPU per call, ``plan_picker.screen_lines`` ~0.06 s), and each
# such thread holds the GIL for as long as it runs. Every time the loop thread
# (or a request's other worker hops) needs the GIL back it waits out one
# switch interval *per competing thread*; at CPython's default 5 ms, a
# ``GET /api/board`` that is ~15 ms alone took 6.9 s beside three of those
# renders on a test instance and 15-48 s on the live one: the intermittent
# 13-19 s tail, with every handler on the loop slowed at once. 0.5 ms brings
# the same request back to ~10-25 ms (a hog thread loses a little throughput
# to extra hand-offs, which nothing here is sensitive to).
GIL_SWITCH_INTERVAL_S = 0.0005


def tune_gil_switch_interval() -> float:
    """Lower the interpreter's GIL switch interval to :data:`GIL_SWITCH_INTERVAL_S`.

    Process-wide and idempotent. Only ever lowers it: a value already at or
    below the target (a test, an operator's own setting) is left alone.
    Returns the interval in force afterwards.
    """
    current = sys.getswitchinterval()
    if current > GIL_SWITCH_INTERVAL_S:
        sys.setswitchinterval(GIL_SWITCH_INTERVAL_S)
        logger.info(
            "ℹ️ GIL switch interval %.1f ms → %.1f ms (#1492)",
            current * 1000, GIL_SWITCH_INTERVAL_S * 1000,
        )
    return sys.getswitchinterval()


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    tune_gil_switch_interval()
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


LOOP_FACTORY = "app.webapp.event_loop:selector_loop_factory"
