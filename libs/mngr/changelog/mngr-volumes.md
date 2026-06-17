Added a reusable `AutoToggle` enum (`yes` / `auto` / `no`) to `mngr.primitives` for force-on / best-effort / force-off CLI options, with an `auto_toggle_choices()` helper and a shared `AUTO_TOGGLE_HELP_SUFFIX`. The `mngr aws` / `mngr azure` `prepare --use-offline-host-dir` flag uses it.

Regenerated the `mngr aws` / `mngr azure` CLI doc pages to cover the new `prepare --use-offline-host-dir {yes,auto,no}` flag and the state-bucket setup these commands now perform (the providers' state-bucket feature is described in the `mngr_aws` / `mngr_azure` changelogs).

Extracted the `--use-offline-host-dir` click option into a shared `add_use_offline_host_dir_option` decorator in `mngr.cli.common_opts` so the AWS and Azure `prepare` commands share one definition of the flag.

Test-only: raised the per-test timeout on the flaky tmux lifecycle tests `test_start_restart_running_agent` / `test_start_restart_stopped_agent` from the default 10s to 30s (they run several sequential tmux create/stop/restart operations that can exceed 10s on a loaded CI runner); both stay `@pytest.mark.flaky`.
