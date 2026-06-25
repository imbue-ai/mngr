Hardened suspicious edge-case handling in the top-level `imbue/minds` modules:

- `_ensure_mngr_settings` now resolves the active settings path through the same guarded `read_active_profile_dir` helper the other readers use, so a corrupt/unreadable `config.toml` degrades gracefully instead of raising a raw `TOMLDecodeError` out of the import-time bootstrap on every `minds` invocation.
- `reconcile_imbue_cloud_providers_from_sessions` now logs a warning (instead of silently skipping) when the plugin's `accounts.json` decodes to an unexpected shape or contains a malformed entry, so a future schema drift surfaces loudly rather than silently leaving the user unable to `mngr create`.
- `cli_entry` asserts that click has resolved a subcommand before logging the command name, replacing a dead `or "unknown"` fallback that could only silently mislabel logs.
