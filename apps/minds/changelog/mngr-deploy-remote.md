Fixed several bugs in `minds env deploy` / `recover` and the workspace-create
flow, surfaced while standing up a fresh dev environment:

- Deploy now pushes the `ovh` per-env Modal Secret. The remote-service-connector
  app references `ovh-<tier>-<deploy_id>` via `Secret.from_name` (its release
  route + cleanup cron sign OVH API calls at runtime), but the `ovh` entry was
  missing from every tier's `deploy.toml` `[secrets].services` list, so
  `modal deploy rsc-<tier>` failed with "Secret ... not found in environment".
  Added `ovh` to the dev/staging/production/ci lists and added a regression test
  asserting each tier's `secrets.services` matches `per_env_secret_services()`.
- `minds env recover` now runs non-interactively. The Modal app-stop step ran
  `modal app stop` without `-y`, which aborts with "no interactive terminal
  detected" whenever recover runs without a TTY (auto-rollback after a failed
  deploy, CI, background runs). Added `-y`.
- `minds env recover` is now re-runnable. The Neon instant-restore step was not
  idempotent: a recover that failed a later step left the pre-restore preserve
  branch behind, so re-running returned 409 ("branch with that name already
  exists") and could never delete its recover-target file. The restore now
  treats that 409 as "already restored" and proceeds.
- `minds env deploy` now exits non-zero when a failed deploy rolls back. The
  failure path execs into `minds env recover`, which inherits the exit code; a
  successful rollback therefore reported the *failed* deploy as success (exit 0),
  masking it from callers / CI. `recover` gained a hidden `--from-failed-deploy`
  flag (passed only by that auto-rollback exec) that forces a non-zero exit even
  when the rollback itself succeeds.
- `minds env activate` no longer dead-locks the recover flow. The blanket
  "refuse activation while ANY recover-target file exists" guard created a
  catch-22: `minds env recover` requires an activated env, but activation was
  blocked by the failed env's own recover-target -- so you could never activate
  the env to recover it. Activation now allows activating an env that has its
  own pending recover-target (surfacing any *other* envs' targets as a warning),
  and only hard-refuses when the pending target(s) belong solely to other envs.
- Fixed a `ty` error / runtime breakage in workspace creation from a bad merge:
  `_MngrCreateAttemptParams` still carried a `gh_token` field (and passed it to
  `run_mngr_create`) after `GH_TOKEN` had been removed end-to-end as unused, so
  the param no longer matched `run_mngr_create`'s signature and the field was
  never supplied at the construction site. Removed the leftover `gh_token`.
