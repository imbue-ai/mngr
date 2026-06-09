Test-only changes (no user-visible behavior change).

- Fixed the e2e test fixture: the generated `settings.local.toml` defined `type = "claude"` twice
  under `[commands.create]`, which is an invalid duplicate TOML key and caused every `mngr` command
  in the e2e suite to fail with a config parse error. Removed the duplicate.
- Strengthened `test_list_filter_by_state` to also assert that the `--stopped` flag returns exactly
  the same set of agents as its documented CEL alias `--include 'state == "STOPPED"'`.
