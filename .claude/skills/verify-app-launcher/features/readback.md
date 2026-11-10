# readback

<!-- gen:begin feature -->
**Seeded from:** surface()

**How a user reaches it**

- Not declared in `.fleet.toml` or `CLAUDE.md`. Read the driving test: it opens the surface before it calls `surface()`.

| Sub-feature | Surface name | Driving test node(s) |
| --- | --- | --- |
| summary-modal | `readback.summary-modal` | `tests/e2e/compose/test_summarize_readback.py::test_summary_modal_closes_when_reading_ends_and_on_a_tap` |

**Drive:** `.\.venv\Scripts\python.exe -m pytest tests/e2e/compose/test_summarize_readback.py::test_summary_modal_closes_when_reading_ends_and_on_a_tap -q --browser chromium` (the suite boots its own disposable instance).
<!-- gen:end feature -->

## Gotchas

_None recorded yet._ Add one line per trap: the symptom, then the fix. Regeneration never touches this section.
