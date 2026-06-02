Fixed three bugs in `minds env deploy` / `recover` surfaced while standing up a
fresh dev environment:

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
