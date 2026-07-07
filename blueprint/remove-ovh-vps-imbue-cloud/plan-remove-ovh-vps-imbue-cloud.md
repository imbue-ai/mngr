# Remove OVH VPS logic from imbue_cloud and the remote connector service

## Refined prompt

let's work through the following cleanup task: remove all OVH VPS logic from imbue_cloud

Note that, in particular, we do NOT want to remove it from mngr_ovh. We simply want to remove the last vestiges from imbue_cloud and our remote connector service (and maybe forever-claude-template, if relevant)

Go gather all of the context for the minds app (per instructions in CLAUDE.md). Also take a look at ~/project/forever-claude-template (the default repo that we deploy)

Also, while we're cleaning up, please delete scripts/remove_old_flat_vault_secrets.py as well (it's no longer needed / used)

* This completes the prior `blueprint/deprecate-ovh-vps` effort, which deliberately kept the `ovh_vps` code paths until migration off them was done; this task removes them.
* Keep all OVH-as-bare-metal-box-supplier logic that lima slices depend on (`bare_metal_servers`, `ovh_order_id`/`ovh_service_name`, `slices/pricing.py` OVH catalog + `OvhCatalogPricingError`, OVH region/datacenter codes, `KNOWN_OVH_US_REGIONS` validation); remove only the legacy `ovh_vps` one-VPS-per-host backend.
* Scope spans `libs/mngr_imbue_cloud`, `apps/remote_service_connector`, `apps/minds` (`cli/pool.py`, env teardown, docs), the root `justfile`, and forever-claude-template; `mngr_vps` and `mngr_ovh` stay untouched.
* Collapse the `backend_kind` concept entirely: `slice` becomes implicit; drop the `--backend` flag, the `BackendKind` type, and the `backend_kind` DB column.
* Assume no live `ovh_vps` pool-host rows remain; remove teardown outright (a forward migration defensively deletes any residual `ovh_vps` rows before dropping the column).
* Remove the connector's OVH release/cleanup logic and `scripts/cleanup_released_hosts.py`; keep the `cleanup_removing_pool_hosts` cron for its `reconcile_slice_boxes` slice audit; release becomes slice-only.
* Drop the `ovh` Modal secret from the connector deployment (`deploy.toml` x3 + `per_env_deploy.py`) and the `env.py` read that built it; keep the `<tier>/ovh` Vault entry and a trimmed `.minds/template/ovh.sh` for operator-sourced bare-metal box ordering.
* Also strip the `minds env deploy`/`env destroy` OVH-tag VPS teardown (delete `envs/providers/ovh_tags.py` and its wiring).
* Keep command names (`mngr imbue_cloud admin pool create` / `minds pool create`) as slice-only (`--server-id` required); keep the `vps_address`/`ssh_port` columns and the `region` validation, updating only OVH-VPS-specific wording.
* Remove now-unused dependencies (`ovh` from the connector, `mngr_ovh` from minds / mngr_imbue_cloud where it becomes unused).
* In forever-claude-template, keep the `[providers.ovh]` block (it drives the kept `mngr_ovh` direct provider) and only fix the stale "imbue-cloud pool-bake default" comments, working in a `.external_worktrees/forever-claude-template` worktree on the same branch with its own changelog entry.
* Full scrub of `ovh_vps`/legacy-VPS-teardown references from the minds docs, leaving only bare-metal-box-supplier framing.
* Delete specs/blueprints that were only about the removed behavior: `specs/swap-pool-to-ovh/`, `blueprint/deprecate-ovh-vps/`, and `blueprint/disable-ovh-qemu-backups/`; keep `specs/ovh-vps-provider/spec.md` (it documents the kept `mngr_ovh` plugin).
* Delete the `ovh_vps`-specific tests (including the deprecation-error tests) and add/update tests asserting the slice-only behavior.
* Add one per-PR changelog entry per touched project (`libs/mngr_imbue_cloud`, `apps/remote_service_connector`, `apps/minds`, `dev`) plus forever-claude-template's own.

## Overview

- The `ovh_vps` pool backend (one ordered OVH classic VPS per pool host) is the last vestige of a path that was already deprecated; bare-metal slices (lima VMs on OVH bare-metal boxes) are the sole live backend, so the `ovh_vps` machinery can be deleted outright.
- The central distinction driving every change: OVH appears in two unrelated roles. The legacy `ovh_vps` *backend* is removed; OVH as the *bare-metal box supplier* that slices are carved on (ordering, pricing, regions, credentials) stays, as do the generic `mngr_vps` foundation and the `mngr_ovh` provider library.
- Because slices are the only remaining backend, the `backend_kind` discriminator collapses everywhere: CLI flag, primitive type, and DB column all go, simplifying the lease/release/reconcile paths to a single shape.
- The cleanup removes runtime OVH coupling from the deployed connector entirely (no more signed OVH API calls, no `ovh` SDK dependency, no `ovh` Modal secret), while preserving the `<tier>/ovh` Vault credentials operators still need to order bare-metal boxes.
- This is a removal, not a behavior change for slices: leasing, slice release, and the hourly slice reconcile keep working; only the dead OVH-VPS branches and their now-stale docs/specs/blueprints disappear.

## Expected behavior

- `mngr imbue_cloud admin pool create` and `minds pool create` are slice-only: no `--backend` flag, `--server-id` required, and the `ovh_vps`-only flags (`--management-public-key-file`, `--no-recycle`) are gone. There is no `ovh_vps` value to pass anymore (not even to get a deprecation error).
- Leasing is unchanged from a user's perspective: the connector hands out available slice rows and `mngr create @...imbue_cloud` works as before.
- Releasing a pool host tears down the slice (lima VM / container) only; the connector makes no OVH API calls during release and never returns an OVH-cleanup error.
- The connector's hourly `cleanup_removing_pool_hosts` cron still runs and still performs the slice reconcile audit; it no longer performs any OVH VPS tag-strip/cancel sweep.
- `minds env destroy` no longer attempts to find or terminate OVH VPSes tagged for the env (that step is removed); all other env teardown steps are unchanged.
- Operators still order bare-metal boxes using OVH credentials sourced from the unchanged `secrets/minds/<tier>/ovh` Vault entry; the connector deployment no longer carries the `ovh` Modal secret.
- The deployed connector no longer depends on the `ovh` Python package; minds and mngr_imbue_cloud no longer depend on `mngr_ovh` (which itself remains a fully functional, separately-usable provider).
- `mngr create @host.ovh` against forever-claude-template still works (the `[providers.ovh]` block and `mngr_ovh` are untouched); only stale "pool-bake default" wording is corrected.
- The connector DB no longer has a `backend_kind` column; any residual `ovh_vps` rows are deleted by the migration.
- Known residual gap (out of scope here, flagged as future work): with the OVH sweep gone, a slice row left in `removing` by a crashed release is no longer auto-mopped; inline slice teardown plus the alert-only reconcile remain the only cleanup.

## Changes

### `libs/mngr_imbue_cloud`

- Remove the `ovh_vps` ordering/hardening path from `cli/admin.py` (the on-demand OVH order + ufw/management-key hardening) and its `mngr_ovh` imports; keep the slice/bare-metal (`cli/server.py` `allocate_slices`) and `admin server` box-lifecycle paths.
- In `primitives.py`, remove `BACKEND_KIND_OVH_VPS`, the `BackendKind` type, `InvalidBackendKind`, and the `_BACKEND_KINDS` set (slice is now implicit).
- Strip OVH-VPS-specific wording from `data_types.py` field descriptions (e.g. "OVH-backed rows are DNS hostnames `vps-xxxx.vps.ovh.us`"); keep `vps_address`/`ssh_port`/`vps_instance_id` fields and the `region` knob + `KNOWN_OVH_US_REGIONS` validation.
- Keep `errors.py` `OvhCatalogPricingError`, `slices/pricing.py`, and all bare-metal server data types/DB code (these serve the kept bare-metal-box supplier path); remove only `backend_kind` reads/writes and `ovh_vps` framing where present.
- Update `README.md` so slices are the only documented backend and `ovh_vps` is gone (not merely "legacy").

### `apps/remote_service_connector`

- Remove the OVH release-teardown branch and all OVH helpers from `app.py`: `OvhVpsResource`, `OvhOps`/`HttpOvhOps`/`OvhClientCaller`, `OVH_PROVIDER_TAG_KEY`, `vps_urn_for`, `ovh_region_code_for_endpoint`, `_get_ovh_ops`/`_get_ovh_endpoint`, `PoolHostCleanupError`, `run_pool_host_cleanup_sweep`, and the `import ovh` lines.
- Make the release path slice-only (drop the `backend_kind` branch); keep `cleanup_removing_pool_hosts` but remove its OVH sweep call, retaining the `reconcile_slice_boxes` audit.
- Drop `backend_kind` from all SQL (`bare_metal_db.py` insert/select, `reconcile_slice_boxes` query) and from the `testing.py` `PoolHostRow` harness.
- Delete `scripts/cleanup_released_hosts.py` and its test.
- Add a forward SQL migration (next sequence number after `011`) that `DELETE`s any `pool_hosts` rows with `backend_kind = 'ovh_vps'`, then `ALTER TABLE pool_hosts DROP COLUMN backend_kind`.
- Remove the `ovh` dependency from `pyproject.toml`.

### `apps/minds`

- In `cli/pool.py`, remove `_run_ovh_vps_pool_create`, `_BACKEND_OVH_VPS`, the `--backend` option, and the `ovh_vps`-only flags/branches; `pool create` becomes slice-only with `--server-id` required.
- Delete `envs/providers/ovh_tags.py`; remove `OvhCredentials`, the `list_ovh_instances`/`delete_ovh_instances` wiring, and the env-destroy "Step 2: OVH VPSes" teardown from `cli/env.py` and `envs/provisioning.py`.
- Drop the `ovh` Modal secret from the connector secret set in `per_env_deploy.py` and from `config/envs/{dev,staging,production}/deploy.toml`; remove the `env.py` read of `<tier>/ovh` that built that secret (and the related stale comments).
- Trim `.minds/template/ovh.sh` to describe only bare-metal-box ordering (drop the removed pool-create / connector-runtime framing); keep the file and the `secrets/minds/<tier>/ovh` Vault entry it documents.
- Full scrub of `ovh_vps`/legacy-VPS-teardown references from the minds docs (`host-pool-setup.md`, `vault-setup.md`, `staging-bringup.md`, `environments.md`, and any others), leaving bare-metal-box-supplier framing only.
- Remove the now-unused `mngr_ovh` dependency from `pyproject.toml` (after confirming no remaining imports).

### `dev` (root `justfile`)

- Remove `ovh_vps`/legacy-VPS-teardown wording from the pool recipe comments (the `bake-pool-host-*` recipes were already removed; `destroy-pool-host`'s "cancel the OVH VPS for an `ovh_vps` row" branch note goes).

### forever-claude-template (separate repo)

- Work in `.external_worktrees/forever-claude-template` on the same branch; commit there with its own changelog entry.
- Keep the `[providers.ovh]` block in `.mngr/settings.toml` (it drives `mngr create @host.ovh` via the kept `mngr_ovh`); fix only the stale "imbue-cloud pool-bake's default" comments/framing.

### Specs, blueprints, and the one-off script

- Delete `scripts/remove_old_flat_vault_secrets.py` (unused; counts as a `dev` change).
- Delete `specs/swap-pool-to-ovh/`, `blueprint/deprecate-ovh-vps/`, and `blueprint/disable-ovh-qemu-backups/`.
- Keep `specs/ovh-vps-provider/spec.md` (documents the kept `mngr_ovh` plugin) and all `vps-docker-*` / `bare-providers` / `ovh-baremetal-slices` / `runsc-everywhere-lima-vps` docs (generic / current slice path).

### Tests

- Delete the `ovh_vps`-bake and `--backend ovh_vps` deprecation-error tests across `libs/mngr_imbue_cloud` and `apps/minds`.
- Delete `ovh_tags` / connector-OVH-sweep / `cleanup_released_hosts` tests.
- Add/update tests asserting: `pool create` is slice-only and requires `--server-id`; the connector release/reconcile paths work without `backend_kind`; `env destroy` runs without the OVH step.
- Re-trim any `test_ratchets.py` counts that drop as a result of deleted code.

### Changelog

- Add one per-PR changelog entry per touched project: `libs/mngr_imbue_cloud/changelog/`, `apps/remote_service_connector/changelog/`, `apps/minds/changelog/`, and `dev/changelog/` (justfile + root script/spec/blueprint deletions), each named `<branch-with-slashes-as-dashes>.md`; plus forever-claude-template's own changelog entry in its repo.
