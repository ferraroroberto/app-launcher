# screen

<!-- gen:begin feature -->
**Seeded from:** surface()

**How a user reaches it**

- Not declared in `.fleet.toml` or `CLAUDE.md`. Read the driving test: it opens the surface before it calls `surface()`.

| Sub-feature | Surface name | Driving test node(s) |
| --- | --- | --- |
| quality-sheet | `screen.quality-sheet` | `tests/e2e/test_screen_quality.py::test_a_preset_rides_the_connect_query_is_remembered_and_survives_a_reconnect` |
| readout | `screen.readout` | `tests/e2e/test_screen_quality.py::test_the_readout_numbers_are_the_bytes_and_frames_the_page_received` |

**Drive:** `.\.venv\Scripts\python.exe -m pytest tests/e2e/test_screen_quality.py::test_a_preset_rides_the_connect_query_is_remembered_and_survives_a_reconnect tests/e2e/test_screen_quality.py::test_the_readout_numbers_are_the_bytes_and_frames_the_page_received -q --browser chromium` (the suite boots its own disposable instance).
<!-- gen:end feature -->

## Gotchas

_None recorded yet._ Add one line per trap: the symptom, then the fix. Regeneration never touches this section.
