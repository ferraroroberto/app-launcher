# lifeos

<!-- gen:begin feature -->
**Seeded from:** surface()

**How a user reaches it**

- Not declared in `.fleet.toml` or `CLAUDE.md`. Read the driving test: it opens the surface before it calls `surface()`.

| Sub-feature | Surface name | Driving test node(s) |
| --- | --- | --- |
| browser | `lifeos.browser` | `tests/e2e/life_os/test_life_os_skills.py::test_life_os_browser_sheet_opens_a_file_over_the_list` |
| convo-sheet | `lifeos.convo-sheet` | `tests/e2e/life_os/test_life_os_conversations.py::test_life_os_convo_link_opens_the_sheet_and_the_reader` |
| convo-viewer | `lifeos.convo-viewer` | `tests/e2e/life_os/test_life_os_conversations.py::test_life_os_convo_link_opens_the_sheet_and_the_reader` |
| doc | `lifeos.doc` | `tests/e2e/life_os/test_life_os_skills.py::test_life_os_browser_sheet_opens_a_file_over_the_list` |
| model-menu | `lifeos.model-menu` | `tests/e2e/life_os/test_life_os_skills.py::test_life_os_tab_renders_skills_and_its_toolbar_drives_the_launch` |
| tab | `lifeos.tab` | `tests/e2e/life_os/test_life_os_skills.py::test_life_os_tab_renders_skills_and_its_toolbar_drives_the_launch` |

**Drive:** `.\.venv\Scripts\python.exe -m pytest tests/e2e/life_os/test_life_os_skills.py::test_life_os_browser_sheet_opens_a_file_over_the_list tests/e2e/life_os/test_life_os_conversations.py::test_life_os_convo_link_opens_the_sheet_and_the_reader tests/e2e/life_os/test_life_os_skills.py::test_life_os_tab_renders_skills_and_its_toolbar_drives_the_launch -q --browser chromium` (the suite boots its own disposable instance).
<!-- gen:end feature -->

## Gotchas

_None recorded yet._ Add one line per trap: the symptom, then the fix. Regeneration never touches this section.
