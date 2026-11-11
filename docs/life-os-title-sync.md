# Life OS title sync

Renaming a Life OS conversation renames every live session that has it open, and renaming a session renames the Life OS conversation it has open. The mirror is best effort: the rename the owner made always stands, and the reply carries a verdict saying whether the other side followed. The verdict's values are declared once, in `src/life_os/title_sync.py`, as the `life-os-title-sync` state machine in `.fleet.toml`, so the conventions ratchet's state rules report any other module that spells them as raw strings.

## Verdicts

| Verdict | Meaning |
| --- | --- |
| none (the reply has no verdict key) | Nothing to mirror: the title was cleared, or no session (or conversation) is on the other side |
| `synced` | Every target took the name |
| `failed` | At least one target kept its old name, or the targets could not be found; the verdict carries an `error` for the phone |

A partial mirror is `failed`, never `synced`: one stale name is what the owner needs to hear about. `title_sync(synced, failed)` is the one function that turns the counts into a verdict.

## Where it appears

| Reply key | Route | Mirrors |
| --- | --- | --- |
| `session_sync` | `PATCH /api/life-os/skills/<skill>/conversations` | the conversation's new title onto its live sessions (`renamed`, `failed` counts) |
| `life_os_sync` | `POST /api/claude-code/sessions/<id>/rename` | the session's new name onto its Life OS conversation (`conversations` count) |
| `title_sync` | the conversation Resume launch | the conversation's title onto the resumed session |

## The phone

The browser reads the same values from `app/webapp/static/life-os/title-sync.js`: `TitleSync`, `titleSync(verdict)` (`null` outside the set) and `syncFailed(verdict)`. The Life OS rename and the session rename toast the verdict's `error` only when `syncFailed` holds, so a synced, missing or unknown verdict never reports a stale name. It is declared as the `life-os-title-sync-ui` state machine; `tests/unit/life_os/test_title_sync.py` holds the verdict table and runs the Node pin, which fails when the browser's values drift from `TitleSync`.
