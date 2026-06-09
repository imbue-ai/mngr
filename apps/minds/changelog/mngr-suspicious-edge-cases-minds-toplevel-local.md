Hardened suspicious edge-case handling in the top-level `imbue/minds` modules:

- `_ensure_mngr_settings` now resolves the active settings path through the same guarded `read_active_profile_dir` helper the other readers use, so a corrupt/unreadable `config.toml` degrades gracefully instead of raising a raw `TOMLDecodeError` out of the import-time bootstrap on every `minds` invocation.
- `reconcile_imbue_cloud_providers_from_sessions` now logs a warning (instead of silently skipping) when the plugin's `accounts.json` decodes to an unexpected shape or contains a malformed entry, so a future schema drift surfaces loudly rather than silently leaving the user unable to `mngr create`.
- `is_imbue_cloud_provider_enabled_for_account` no longer crashes the desktop chip-rendering path on a corrupt `settings.toml`; a read/parse failure falls back to the documented "enabled" default (with a warning), consistent with its other branches.
- `list_disabled_provider_names` (the other passive providers-panel read) gets the same treatment: a corrupt/unreadable `settings.toml` degrades to an empty list (with a warning) instead of crashing the enumeration.
- `cli_entry` asserts that click has resolved a subcommand before logging the command name, replacing a dead `or "unknown"` fallback that could only silently mislabel logs.
- Documented why `resolve_minds_root_name` deliberately falls back to the production data dir on a set-but-invalid `MINDS_ROOT_NAME` (and which stricter gate destructive callers use instead).
