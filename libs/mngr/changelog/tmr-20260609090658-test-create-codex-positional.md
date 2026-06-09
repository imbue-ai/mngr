Fixed the e2e tutorial test fixture and the `test_create_codex_positional` release test.

- The e2e fixture's `settings.local.toml` was emitting a duplicate `type = "claude"` key under
  `[commands.create]`, producing malformed TOML. Any `mngr config set` that re-parsed the merged
  config then failed with "Cannot overwrite a value". Removed the duplicate line.
- `test_create_codex_positional` now scopes its verification `mngr list` to `--provider local`
  (the agent is created locally) and raises the per-test timeout to 120s, matching the sibling
  `test_create_codex_explicit_type`. The previous unscoped `mngr list` fanned out to every
  provider (including Modal) and exceeded the default 10s timeout. The test also now asserts the
  created agent reached a RUNNING/WAITING state, not just that it has the codex type.
