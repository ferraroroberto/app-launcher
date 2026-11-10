# settings

<!-- gen:begin feature -->
**Seeded from:** surface() · extra_steps

**How a user reaches it**

- From the `claude` tab, click `#settingsOpen`.

| Sub-feature | Surface name | Driving test node(s) |
| --- | --- | --- |
| pane | `settings.pane` | `tests/e2e/settings/test_settings_sheets.py::test_every_sheet_opens_and_closes` |

**Drive:** `.\.venv\Scripts\python.exe -m pytest tests/e2e/settings/test_settings_sheets.py::test_every_sheet_opens_and_closes -q --browser chromium` (the suite boots its own disposable instance).
<!-- gen:end feature -->

## Gotchas

_None recorded yet._ Add one line per trap: the symptom, then the fix. Regeneration never touches this section.
