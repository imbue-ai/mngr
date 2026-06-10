Fixed the e2e tutorial test fixture and broadened coverage of the `mngr create --pass-env`
tutorial block.

- Removed a duplicate `type = "claude"` key the `e2e` fixture wrote into the generated
  `settings.local.toml`. The duplicate produced invalid TOML, so every `mngr` command in the
  e2e/tutorial release suite failed with `Cannot overwrite a value`.
- Added `test_create_with_pass_env_skips_unset_var`, an unhappy-path test for the same
  `--pass-env` tutorial block: forwarding a variable that is not set in the current shell does
  not fail `create`; the variable is simply absent from the agent's environment.
