Hardened suspicious edge-case handling in the kanpan plugin:

- `compute_section` now proves PR-state exhaustiveness with `assert_never` instead of a trailing `raise AssertionError`.
- Field-cache save/load failures and unexpectedly-failed board refreshes now log at `warning` (with a traceback for the refresh case) so silently-discarded caches and fetch-pipeline bugs are visible; `save_field_cache` now catches only `OSError` so a serialization bug surfaces instead of being swallowed.
- Malformed `[plugins.kanpan.commands.*]` entries are now logged instead of being silently dropped.
- A degenerate GitHub remote that does not yield an `owner/repo` no longer produces a malformed/empty repo column.
