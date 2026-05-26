## mngr_tmr

- `mngr tmr --provider modal --use-snapshot` now bootstraps the Modal per-user environment on first run instead of aborting with `ProviderEmptyError`. The pre-snapshot provider lookup passes `is_for_host_creation=True`, matching the create path.
- Several silent-success failure modes now produce a non-zero exit (click's default exit code):
  - `--reintegrate` when `mngr list` fails or the run name matches no agents.
  - Any tmr run where every test agent failed to launch (no successful launches).
