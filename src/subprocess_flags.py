"""Windows console-suppression flags for subprocess spawns.

A console subprocess launched from a windowless parent (the ``pythonw`` tray,
or any of its descendants — the webapp uvicorn process, a detached
``launcher.py`` run, ...) gets a **new, visible** console window from Windows
unless the child's creation flags say otherwise. This is the one place that
combination lives, so a call site imports it instead of re-deriving its own
module-level constant or inlining ``subprocess.CREATE_NO_WINDOW`` /
``getattr(subprocess, "CREATE_NO_WINDOW", 0)`` directly — the fleet-wide
convention in the global CLAUDE.md "Subprocess spawns must suppress the
console window" gotcha, with sibling implementations in
``local-llm-hub/scripts/_lib.py`` and
``whatsapp-radar/src/subprocess_flags.py``.

Safe to pass even when the parent *does* have a console, as long as the
child's output is captured (piped/redirected) rather than read from a console
directly. Both resolve to ``0`` — a pure no-op — on non-Windows, so a call
site can pass them unconditionally without a ``sys.platform`` guard.
"""

from __future__ import annotations

import subprocess

#: Suppress the console window for a one-shot, output-captured child.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: For a long-lived child that may later be stopped via a process-group
#: signal (e.g. ``CTRL_BREAK_EVENT``) rather than killed outright —
#: cloudflared, the webapp's own uvicorn subprocess.
NO_WINDOW_NEW_GROUP = NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
