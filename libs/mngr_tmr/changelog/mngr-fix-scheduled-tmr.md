## mngr_tmr

- `mngr tmr --provider modal --use-snapshot` now bootstraps the Modal per-user environment on first run instead of aborting with `ProviderEmptyError`. The pre-snapshot provider lookup passes `is_for_host_creation=True`, matching the create path.
- Exit codes follow a clearer convention: `1` for usage errors (e.g. `--reintegrate` without `--run-name`, or `--run-name` referring to an unknown run), and `2` for everything else. Previously usage errors exited `2` (click's default) and other errors exited `1`.
- Several silent-success failure modes now produce a non-zero exit:
  - `--reintegrate` when `mngr list` fails or the run name matches no agents.
  - Any tmr run where every test agent failed to launch (no successful launches).
