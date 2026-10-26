# apps

<!-- gen:begin feature -->
**Seeded from:** surface()

**How a user reaches it**

- Not declared in `.fleet.toml` or `CLAUDE.md`. Read the driving test: it opens the surface before it calls `surface()`.

| Sub-feature | Surface name | Driving test node(s) |
| --- | --- | --- |
| other-ports-sheet | `apps.other-ports-sheet` | `tests/e2e/apps/test_apps_tab_glance.py::test_running_card_badges_header_and_other_ports` |
| tab | `apps.tab` | `tests/e2e/apps/test_apps_tab_glance.py::test_running_card_badges_header_and_other_ports` |

**Drive:** `.\.venv\Scripts\python.exe -m pytest tests/e2e/apps/test_apps_tab_glance.py::test_running_card_badges_header_and_other_ports -q --browser chromium` (the suite boots its own disposable instance).
<!-- gen:end feature -->

## Gotchas

_None recorded yet._ Add one line per trap: the symptom, then the fix. Regeneration never touches this section.
