# code

<!-- gen:begin feature -->
**Seeded from:** surface()

**How a user reaches it**

- Not declared in `.fleet.toml` or `CLAUDE.md`. Read the driving test: it opens the surface before it calls `surface()`.

| Sub-feature | Surface name | Driving test node(s) |
| --- | --- | --- |
| changes-empty | `code.changes-empty` | `tests/e2e/test_coding_changes_overlay.py::test_clean_tree_shows_the_empty_state` |
| changes-overlay | `code.changes-overlay` | `tests/e2e/test_coding_changes_overlay.py::test_overlay_lists_files_and_expands_a_tinted_diff` |
| tab | `code.tab` | `tests/e2e/test_smoke.py::test_pty_session_renders_with_both_stop_buttons` |

**Drive:** `.\.venv\Scripts\python.exe -m pytest tests/e2e/test_coding_changes_overlay.py::test_clean_tree_shows_the_empty_state tests/e2e/test_coding_changes_overlay.py::test_overlay_lists_files_and_expands_a_tinted_diff tests/e2e/test_smoke.py::test_pty_session_renders_with_both_stop_buttons -q --browser chromium` (the suite boots its own disposable instance).
<!-- gen:end feature -->

## Gotchas

_None recorded yet._ Add one line per trap: the symptom, then the fix. Regeneration never touches this section.
