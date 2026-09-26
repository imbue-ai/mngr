Phase 6 of the slice-fleet cutover: the gen-1 slice fleet is gone, and the minds side follows.

- `pool-ssh` is no longer one of the per-env secret services: it is dropped from `_PER_ENV_SECRET_SERVICES` and from every tier's `deploy.toml` `[secrets].services`, so `minds-admin env deploy` stops pushing a `pool-ssh-<tier>` Modal secret.

- Deploy docs: `gen2-cutover.md` moves to `docs/deploy/history/rollouts/` as the historical record of the migration (the `minds-admin cutover` tooling it drives is deleted); `gen2-management-plane.md` and `gen2-telemetry.md` move to `docs/deploy/reference/management-plane.md` and `reference/box-telemetry.md`; `host-pool-setup.md` moves to `docs/deploy/setup/`. Every runbook drops its gen-1 branches (the pool key, `repair-home-layout`, `backfill-host-keys`, `cutover repave`, slot counting); capacity is now described in default-size machines and the two-budget accounting.

- `next_deploy.md` carries connector migration 048 (the gen-1 column drop), the manual `pool-ssh` secret deletion, the one-time box re-prep for the collector pattern change, and the decision to keep the 70 retired gen-1 rows in production until their owners' archives are dropped.

- Deployment tests: `test_machine_resize.py` no longer skips on a gen-1 tier (there is none); docstrings describe the gen-2 cycle only.

- The desktop bundle lock (`apps/minds/electron/pyproject/uv.lock`) drops `imbue-mngr-lima` from `imbue-mngr-imbue-cloud`'s dependencies, matching the plugin's `pyproject.toml`.
