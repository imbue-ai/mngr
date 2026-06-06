Hardened suspicious edge-case handling in the config-loading subsystem:

- `get_or_create_user_id` now raises a typed `ConfigError` (instead of a bare
  `assert`, which is stripped under `python -O`) when `MNGR_USER_ID` does not
  match the stored user ID file, and treats an empty/corrupt user ID file as
  missing (regenerating it) rather than crashing with an untyped error.
- `get_or_create_profile_dir` now raises a clear `ConfigParseError` when the
  `profile` key in `config.toml` is present but not a non-empty string (e.g. a
  hand-edited `profile = 12345`), instead of failing with an opaque `TypeError`.
- `create_templates` config blocks now honor the same forward-compatibility
  policy as the rest of the loader: under `MNGR_ALLOW_UNKNOWN_CONFIG`, an
  unknown template option is warned-and-dropped rather than fatal, so a config
  written for a newer mngr no longer breaks an older one.
- The lightweight `default_subcommand` pre-reader now skips non-string values
  instead of coercing them (e.g. `true` -> `"True"`) into a bogus dispatch
  target.
