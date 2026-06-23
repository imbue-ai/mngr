Bump the bundled Latchkey version to 2.18.0.

When a mind needs a Google API and its credentials are not valid, Minds now tries the Minds-provided Google OAuth consent screen first (registering its client via `latchkey auth prepare`), and only falls back to the old "create your own Google project" self-setup flow if that fails. Most users no longer see the self-setup step. The flow applies to an explicit list of OAuth Google services (Gmail, Calendar, Drive, Docs, Sheets, People, Analytics); `google-directions` (which uses an API key, not OAuth) and all non-Google services are unchanged.

Fix the fallback so a failed Minds OAuth attempt no longer gets stuck on the Minds client: the registered client is cleared before the self-setup flow runs, so "create your own Google project" actually engages (previously the still-registered Minds client suppressed it, and the failing client was retried indefinitely).
