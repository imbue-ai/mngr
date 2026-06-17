Regenerated the `mngr aws` / `mngr azure` CLI doc pages to cover the state-bucket setup these commands now perform (the providers' state-bucket feature is described in the `mngr_aws` / `mngr_azure` changelogs).

Test-only: raised the per-test timeout on the tmux lifecycle tests `test_start_restart_running_agent` / `test_start_restart_stopped_agent` from the default 10s to 30s (they run several sequential tmux create/stop/restart operations that can exceed 10s on a loaded CI runner).
