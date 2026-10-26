# app-launcher feature map

One file per view. Seeded from the repo's committed inventories, never hand-written: the e2e suite's `surface()` names first, then `[[design.review.extra_steps]]`, then the `## UX surface` key views in `CLAUDE.md`. Regenerate with project-scaffolding's `scripts/gen_verify_skill.py`; add traps to a view's `## Gotchas`, the only hand-written part.

<!-- gen:begin index -->
| Feature | Sub-features | Driving nodes | Seeded from |
| --- | --- | --- | --- |
| [apps](apps.md) | 2 | 1 | surface() |
| [board](board.md) | 3 | 3 | surface() |
| [chat](chat.md) | 1 | 1 | surface() |
| [code](code.md) | 3 | 3 | surface() |
| [conversation-sheet](conversation-sheet.md) | 1 | 0 | extra_steps |
| [conversations](conversations.md) | 1 | 0 | extra_steps |
| [drawer](drawer.md) | 1 | 0 | extra_steps |
| [filter](filter.md) | 1 | 0 | extra_steps |
| [home](home.md) | 1 | 0 | key views |
| [job-menu](job-menu.md) | 1 | 0 | extra_steps |
| [jobs](jobs.md) | 6 | 2 | surface() |
| [lifeos](lifeos.md) | 6 | 4 | surface() |
| [readback](readback.md) | 1 | 1 | surface() |
| [screen](screen.md) | 2 | 2 | surface() |
| [session-chat](session-chat.md) | 1 | 0 | extra_steps |
| [session-menu](session-menu.md) | 1 | 0 | extra_steps |
| [session-overlay-menu](session-overlay-menu.md) | 1 | 0 | extra_steps |
| [session-terminal](session-terminal.md) | 1 | 0 | extra_steps |
| [settings](settings.md) | 1 | 1 | surface() · extra_steps |
| [skill-menu](skill-menu.md) | 1 | 0 | extra_steps |
| [terminal](terminal.md) | 5 | 3 | surface() |

**Declared e2e surfaces** (`[[e2e.surface]]`: diff-routing groups, context only, not seeds):

- `board`: `tests/e2e/board/test_board_tab.py`, `tests/e2e/board/`, `tests/e2e/board/test_board_nav_badge.py`, `tests/e2e/board/test_board_job_dismiss.py`, `tests/e2e/shell/test_page_headers.py`, `tests/e2e/shell/test_header_cpu_reading.py`, `tests/e2e/shell/test_text_size.py`, `tests/e2e/shell/test_wide_layout.py`, `tests/e2e/code/test_wide_code_detail.py`, `tests/e2e/code/test_coding_chief.py`, `tests/e2e/code/test_channel_sessions_hidden.py`, `tests/e2e/session/test_session_rename.py`, `tests/e2e/session/test_shared_session_title.py`, `tests/e2e/code/test_coding_model_selector.py`, `tests/e2e/terminal/test_terminal_bar_overflow.py`, `tests/e2e/shell/test_primary_nav.py`, `tests/e2e/shell/test_smoke.py`, `tests/e2e/shell/test_row_name_typography.py`, `tests/e2e/shell/test_icon_sizes.py`, `tests/e2e/shell/test_vendored_empty_and_select.py`, `tests/e2e/shell/test_touch_targets.py`, `tests/e2e/session/test_session_row_glance.py`, `tests/e2e/code/test_code_row_status_age.py`, `tests/e2e/settings/test_usage_meter.py`, `tests/e2e/settings/test_settings_sheets.py`, `tests/e2e/shell/test_action_row_variants.py`, `tests/e2e/shell/test_async_list_states.py`
- `lifeos`: `tests/e2e/life_os`, `tests/e2e/life_os/test_life_tab_glance.py`, `tests/e2e/life_os/test_resume_toggle.py`, `tests/e2e/shell/test_page_headers.py`, `tests/e2e/shell/test_header_cpu_reading.py`, `tests/e2e/shell/test_text_size.py`, `tests/e2e/shell/test_action_row_lists.py`, `tests/e2e/shell/test_markdown_link_rendering.py`, `tests/e2e/shell/test_markdown_tables.py`, `tests/e2e/shell/test_collapsible_other_tabs.py`, `tests/e2e/code/test_coding_model_selector.py`, `tests/e2e/terminal/test_terminal_bar_overflow.py`, `tests/e2e/shell/test_primary_nav.py`, `tests/e2e/shell/test_smoke.py`, `tests/e2e/shell/test_row_name_typography.py`, `tests/e2e/shell/test_icon_sizes.py`, `tests/e2e/shell/test_vendored_empty_and_select.py`, `tests/e2e/shell/test_touch_targets.py`, `tests/e2e/settings/test_settings_sheets.py`, `tests/e2e/shell/test_async_list_states.py`, `tests/e2e/shell/test_dialog_title_focus.py`

**Not mapped** (a `surface()` call the generator cannot turn into a driving node):

- `tests/e2e/settings/test_settings_sheets.py::test_every_sheet_opens_and_closes`: a `surface()` name that is not a string literal
<!-- gen:end index -->
