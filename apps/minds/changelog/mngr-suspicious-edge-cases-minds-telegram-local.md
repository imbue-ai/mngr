Hardened suspicious edge-case handling in the Telegram setup module:

- `fetch_telegram_web_api_credentials` no longer wraps its whole scraping flow in one broad `try` that caught `ValueError`; network calls are isolated and "pattern not found" is explicit control flow, so a real parsing bug now crashes instead of silently falling back to the public default API credentials.
- Telegram setup no longer reports an agent as DONE when its bot-credentials file exists but is corrupt; it now rebuilds the file instead.
- The background Telegram setup thread now always records a terminal status (DONE/FAILED) even if an unexpected telethon/Playwright error escapes, so the UI no longer hangs polling a dead thread.
- Documented the intentional corrupt-treated-as-absent credential-load fallback, the cosmetic `first_name` fallback, and the bot-username fallback; switched `auth_data` reads from `.get` to direct indexing.
