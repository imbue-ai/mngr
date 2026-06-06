Hardened suspicious edge-case handling in `apps/minds/scripts/` Python helpers:

- `create_telegram_bot.py`: `_try_latchkey_auth_get` no longer swallows `KeyError` -- a `telegramUser` record from latchkey that is missing `dcId`/`authKeyHex` is now reported as malformed credentials instead of silently falling through to "no credentials found". The `subprocess.run`/`json.loads` try blocks were narrowed to single statements.
- `create_telegram_bot.py`: `_fetch_telegram_web_api_credentials` drops the redundant `URLError` from its catch (it subclasses `OSError`) and documents that the caught `ValueError`s are the intended "extraction failed, use public defaults" signal.
- `create_telegram_bot.py`: `create_bot` now raises `BotCreationError` when a successful BotFather response lacks a `t.me/<username>` link, instead of guessing the requested username and reporting it as confirmed.
- `demo_desktop_client.py`: documented that the empty-listing fallback in `_render_directory_listing` (on `os.listdir` `OSError`) is a deliberate, demo-only degradation.
