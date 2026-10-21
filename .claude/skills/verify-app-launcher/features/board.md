# board

<!-- gen:begin feature -->
**Seeded from:** surface()

**How a user reaches it**

- Not declared in `.fleet.toml` or `CLAUDE.md`. Read the driving test: it opens the surface before it calls `surface()`.

| Sub-feature | Surface name | Driving test node(s) |
| --- | --- | --- |
| answer-sheet | `board.answer-sheet` | `tests/e2e/test_board_chief.py::test_answer_sheet_renders_questions_and_done_sends_one_message` |
| drawer | `board.drawer` | `tests/e2e/test_board_tab.py::test_board_card_drawer_shows_exchange_and_posts_reply` |
| tab | `board.tab` | `tests/e2e/test_board_tab.py::test_board_renders_columns_counts_and_cards` |

**Drive:** `.\.venv\Scripts\python.exe -m pytest tests/e2e/test_board_chief.py::test_answer_sheet_renders_questions_and_done_sends_one_message tests/e2e/test_board_tab.py::test_board_card_drawer_shows_exchange_and_posts_reply tests/e2e/test_board_tab.py::test_board_renders_columns_counts_and_cards -q --browser chromium` (the suite boots its own disposable instance).
<!-- gen:end feature -->

## Gotchas

_None recorded yet._ Add one line per trap: the symptom, then the fix. Regeneration never touches this section.
