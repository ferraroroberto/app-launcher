"""Windows event-loop shim for uvicorn (issue #388).

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
autoboot spawn in ``tests/e2e/conftest.py``) or
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
import sys


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


LOOP_FACTORY = "app.webapp.event_loop:selector_loop_factory"
