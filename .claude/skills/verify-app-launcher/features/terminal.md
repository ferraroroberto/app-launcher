# terminal

<!-- gen:begin feature -->
**Seeded from:** surface()

**How a user reaches it**

- Not declared in `.fleet.toml` or `CLAUDE.md`. Read the driving test: it opens the surface before it calls `surface()`.

| Sub-feature | Surface name | Driving test node(s) |
| --- | --- | --- |
| attach-menu | `terminal.attach-menu` | `tests/e2e/compose/test_composer_image_menu.py::test_attach_button_menu_reaches_all_four_options` |
| keys-popover | `terminal.keys-popover` | `tests/e2e/terminal/test_keys_popover.py::test_keys_popover_sends_escape_sequences` |
| overlay | `terminal.overlay` | `tests/e2e/terminal/test_terminal_session_menu.py::test_menu_copy_link_and_rename_update_the_bar` |
| rename-dialog | `terminal.rename-dialog` | `tests/e2e/terminal/test_terminal_session_menu.py::test_menu_copy_link_and_rename_update_the_bar` |
| session-menu | `terminal.session-menu` | `tests/e2e/terminal/test_terminal_session_menu.py::test_menu_copy_link_and_rename_update_the_bar` |

**Drive:** `.\.venv\Scripts\python.exe -m pytest tests/e2e/compose/test_composer_image_menu.py::test_attach_button_menu_reaches_all_four_options tests/e2e/terminal/test_keys_popover.py::test_keys_popover_sends_escape_sequences tests/e2e/terminal/test_terminal_session_menu.py::test_menu_copy_link_and_rename_update_the_bar -q --browser chromium` (the suite boots its own disposable instance).
<!-- gen:end feature -->

## Gotchas

_None recorded yet._ Add one line per trap: the symptom, then the fix. Regeneration never touches this section.
