## libs/mngr

- E2E `conftest.py` now writes `[commands.create] type = "claude"` into the test project's `settings.local.toml`, mirroring what `mngr extras config` writes during install. This unblocks `test_create_modal_*` (and any other e2e tests whose tutorial blocks invoke `mngr create` without an explicit `--type`), which had been failing with "No agent type provided" since the source-coded `claude` default for `--type` was removed.
- `test_create_modal_no_start_on_boot` now also runs `mngr list --format json` and asserts that the created agent has `start_on_boot=False`, so it actually exercises the flag instead of only checking the create command's exit code.
