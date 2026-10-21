# jobs

<!-- gen:begin feature -->
**Seeded from:** surface()

**How a user reaches it**

- Not declared in `.fleet.toml` or `CLAUDE.md`. Read the driving test: it opens the surface before it calls `surface()`.

| Sub-feature | Surface name | Driving test node(s) |
| --- | --- | --- |
| agenda-sheet | `jobs.agenda-sheet` | `tests/e2e/test_jobs_tab_glance.py::test_header_next_up_and_the_agenda_sheet` |
| confirm-run | `jobs.confirm-run` | `tests/e2e/test_jobs_tab_glance.py::test_kebab_runs_and_removes_with_no_edit_mode` |
| job-menu | `jobs.job-menu` | `tests/e2e/test_jobs_tab_glance.py::test_kebab_runs_and_removes_with_no_edit_mode` |
| job-sheet | `jobs.job-sheet` | `tests/e2e/test_jobs_tab_glance.py::test_header_next_up_and_the_agenda_sheet` |
| run-dialog | `jobs.run-dialog` | `tests/e2e/test_jobs_tab_glance.py::test_kebab_runs_and_removes_with_no_edit_mode` |
| tab | `jobs.tab` | `tests/e2e/test_jobs_tab_glance.py::test_header_next_up_and_the_agenda_sheet` |

**Drive:** `.\.venv\Scripts\python.exe -m pytest tests/e2e/test_jobs_tab_glance.py::test_header_next_up_and_the_agenda_sheet tests/e2e/test_jobs_tab_glance.py::test_kebab_runs_and_removes_with_no_edit_mode -q --browser chromium` (the suite boots its own disposable instance).
<!-- gen:end feature -->

## Gotchas

_None recorded yet._ Add one line per trap: the symptom, then the fix. Regeneration never touches this section.
