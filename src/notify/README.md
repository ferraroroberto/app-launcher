# `src/notify/` — vendored Telegram notifier

A tiny, **stdlib-only** notifier primitive shared across fleet apps: deliver a short plain-text message to a Telegram chat (e.g. "🚨 alarm triggered", "⚠️ mains power lost"). This is the **Python** counterpart to the tray's vendored primitive (`app/tray/single_instance.py`) and the shared `tray_lifecycle.ps1` helper — the same "copy byte-for-byte, never fork per-app" channel, for a non-UI library module. (The web-UI `_vendored/` channel under `app/webapp/static/` is HTML/CSS/JS only.)

## Files

| File | What |
| --- | --- |
| `base.py` | `NotifierError` + the universal `Notifier` Protocol (one method: `send_text`). |
| `telegram.py` | `TelegramNotifier(bot_token, chat_id)` — one Bot-API `sendMessage` HTTPS POST via `urllib`. |
| `config.py` | `TelegramConfig(bot_token, chat_id)` — a resolved-values dataclass, no env names baked in. |
| `factory.py` | `build_notifier(name, telegram)` over `'none' | 'telegram'`. |
| `__init__.py` | Re-exports the public surface. |

## Vendoring recipe

1. Copy this whole `src/notify/` folder **verbatim** into the consuming app's `src/notify/`. Don't edit the shipped files.
2. In your app's **own** config layer, resolve the bot token + chat id from your secrets (`.env` / `config/*.json`, under whatever env names you like — `WR_TELEGRAM_*`, `TELEGRAM_*`, …) and build a `TelegramConfig(bot_token, chat_id)`. **Env-name resolution and file paths stay in your app — never in this vendored copy.**
3. Build the notifier and send:

   ```python
   from src.notify import build_notifier, NotifierError, TelegramConfig

   notifier = build_notifier("telegram", TelegramConfig(bot_token, chat_id))
   if notifier is not None:                 # 'none' / unconfigured → None → silent no-op
       try:
           notifier.send_text("🚨 Home alarm triggered")
       except NotifierError as exc:
           logger.warning("notify failed: %s", exc)   # best-effort: never crash the caller
   ```

4. Keep secrets out of git (gitignored `config/*.json` + a committed `*.sample.json`, mirroring the rest of the app's config).

## Rules

- **Vendor verbatim.** To change behaviour, change it *here* and re-vendor downstream — don't fork per-app. Per-app variation is *config resolution* and *which events map to which text*, both of which live in the consuming app, not in these files.
- **Stdlib only.** One HTTPS POST — no third-party dependency, nothing added to `requirements.txt`.
- **Best-effort at the call site.** `send_text` raises `NotifierError`; an app firing alerts on an already-failing path should catch and log, never let a delivery failure worsen the situation it reports.
- **Digests / persistence / retry-queues are app-specific.** They layer on top of this primitive; they don't belong in the vendored copy.

## Adoption status

- Source of the pattern: `whatsapp-radar/src/notify/` (digest + alert delivery). Its refactor onto this vendored copy, and the first consumer (`home-automation` — alarm/UPS alerts), are tracked separately (project-scaffolding#100 and its linked issues), not bundled into the vendoring.
