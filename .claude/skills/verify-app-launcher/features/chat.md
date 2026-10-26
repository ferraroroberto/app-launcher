# chat

<!-- gen:begin feature -->
**Seeded from:** surface()

**How a user reaches it**

- Not declared in `.fleet.toml` or `CLAUDE.md`. Read the driving test: it opens the surface before it calls `surface()`.

| Sub-feature | Surface name | Driving test node(s) |
| --- | --- | --- |
| overlay | `chat.overlay` | `tests/e2e/session/test_session_mode_toggle.py::test_toggle_keeps_terminal_scrollback_and_socket` |

**Drive:** `.\.venv\Scripts\python.exe -m pytest tests/e2e/session/test_session_mode_toggle.py::test_toggle_keeps_terminal_scrollback_and_socket -q --browser chromium` (the suite boots its own disposable instance).
<!-- gen:end feature -->

## Gotchas

_None recorded yet._ Add one line per trap: the symptom, then the fix. Regeneration never touches this section.
