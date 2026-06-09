# Changelog - mngr_imbue_cloud

A concise, human-friendly summary of changes for the `mngr_imbue_cloud` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.1.1] - 2026-06-08

### Added

- Added: `--no-recycle` flag on `mngr imbue_cloud admin pool create` that forces a fresh OVH VPS order (sets `MNGR__PROVIDERS__OVH__ENABLE_RECYCLE_CANCELLED=false` on the inner `mngr create`) instead of reclaiming a cancelled (still-billable) VPS, for exercising the fresh-provision path.
- Added: Region-aware leasing — `mngr create` against imbue_cloud accepts a hard `-b region=<datacenter>` build arg (lease fails if no host is available in that datacenter), validated against the known OVH-US datacenters (`US-EAST-VA`, `US-WEST-OR`). The region is sent to the connector's lease endpoint as a separate field (not folded into the JSONB attribute filter), applied on both fast (adopt) and slow (rebuild) paths, and preserved through the slow path's attribute relaxation. `mngr imbue_cloud admin pool create` now records the bake `--region` into the new `pool_hosts.region` column so the connector can filter on it.
- Added: Auto-discovered as a publishable package by the release tooling; will be offered for first publication to PyPI on the next release.

### Changed

- Changed: `ImbueCloudProviderConfig` now extends `VpsDockerProviderConfig`, so it carries `docker_runtime` / `install_gvisor_runtime` / `default_start_args`; the delegated vps_docker provider forwards them, so the rebuilt container runs under `--runtime runsc` with the `--workdir=/` and `--security-opt=no-new-privileges` hardening args (values are written into the per-account `[providers.imbue_cloud_<slug>]` block by minds bootstrap).
- Changed: The imbue_cloud slow (rebuild) path now re-applies the full idempotent host setup (pinned Docker version, gVisor `runsc` install/registration, sshd tuning, base packages) on the leased VPS before rebuilding the container, so a workspace created via the slow path — even on a host baked before runsc existed — comes up consistent and runs its agent container under gVisor. A failure is fatal.

### Removed

- Removed: The soft `-b preferred_region=<dc>` lease build arg. A lease is now constrained only by the hard `-b region=<dc>` arg; when unset, the lease is region-agnostic.

## [v0.1.0] - 2026-06-05

### Added

- Added: New `mngr imbue_cloud bucket` command group (`create` / `list` / `info` / `destroy`) for managing per-host R2 buckets (paid accounts only), plus `bucket keys create/list/destroy` for minting and revoking bucket-scoped S3 keys (read-only or read-write). `bucket create` returns S3-compatible credentials as JSON; the secret is shown only once and never stored. `bucket destroy` refuses a non-empty bucket and otherwise cascades to revoke its keys.
- Added: Pure free `build_pool_host_wipe_script` in `mngr_imbue_cloud.instance` exposing the rendered wipe shell so it can be unit-tested without standing up an SSH transport.
- Added: New `mngr imbue_cloud admin paid` subcommands for managing the connector's paid-user lists: `paid domain add|remove|list` and `paid email add|remove|list` (with `--paid-only` on list). These talk to the connector's `/paid/*` admin API using the fixed API key read from `$MINDS_PAID_ADMIN_KEY` (or `--api-key`). Matching client methods and a `PaidListEntry` data type are exposed.
- Added: Robust "slow path" for imbue_cloud host leasing. A new `fast_mode` build arg (`-b fast_mode=require|prevent`) selects how `mngr create` lands on a pool host: `require` adopts an exactly-matching pre-baked agent (the original fast path) and raises a distinct `FastPathUnavailableError` when no exact match exists; `prevent` (the new default) leases any adequately-sized available host, destroys its baked container, and rebuilds it from the FCT Dockerfile via the shared `mngr_vps_docker` setup path. Failed setup releases the lease before re-raising. Unknown `-b` entries (e.g. `--file=Dockerfile`, `.`) are now forwarded verbatim to the delegated build.

### Changed

- Changed: `mngr destroy <agent>` against an imbue_cloud-leased pool host is now terminal rather than a soft `docker stop`. The new flow stops + removes the workspace container, drops the per-host docker volume and btrfs subvolume, prunes the system, wipes `/root` + `/tmp` (preserving `/root/.ssh/authorized_keys`), releases the lease back to the pool, then cleans up local per-host state. Privacy-first ordering wipes data before flipping the row to `released`. `mngr delete <agent>` runs the same flow and is a safe no-op for an already-released lease. Use `mngr stop <agent>` instead to pause the container without releasing the lease.
- Changed: `mngr imbue_cloud admin pool destroy` (and the `minds pool destroy` wrapper) now do a full teardown: cancel the OVH VPS (strip per-lease tags + `deleteAtExpiration`) before dropping the row, so destruction can no longer strand a still-billing VPS. Pass `--skip-vps-cancel` only when the VPS is already gone. The provider's `destroy_host` now also raises when the connector release fails instead of silently cleaning up local state, so a failed release no longer makes mngr "forget" a host whose lease/VPS is still live.
- Changed: Stopped masking errors in the lease/teardown paths — `_list_leased_hosts_cached` no longer swallows a `list_hosts` failure to an empty list, and `client.release_host` now raises `ImbueCloudConnectorError` on a transport error or non-2xx instead of returning a quiet `False` (the create-rollback path `_release_lease_quietly` still catches it explicitly to stay best-effort). The leased-host TOFU host-key scan now logs (debug) the cause when it can't read a remote key.
- Changed: Bumped the `imbue-mngr` pin in `pyproject.toml` from `0.2.8` to `0.2.10` to align with main's release commit, so building the `apps/minds` ToDesktop bundle from main no longer fails at `uv lock`.
- Changed: Simplified an exception handler now that `HostError` / `HostConnectionError` / `HostNotFoundError` are all `MngrError` subclasses (the redundant `except (HostConnectionError, HostNotFoundError, MngrError)` guard is now just `except MngrError`). No behavior change.
- Changed: `mngr imbue_cloud admin pool create` is now provider-generic — drops `MINDS_ROOT_NAME` env detection, adds a required `--region REGION` and repeatable `--tag KEY=VALUE`, defaults to `--template main --template ovh` with `@host.ovh` + `--provider ovh`, and installs + configures `ufw` on every leased VPS before the row hits `pool_hosts`.
- Changed: `ImbueCloudProvider.create_host` now SFTPs into the leased container after the host-key scan and rewrites `/mngr/data.json`'s `host_name` field to the user-supplied `HostName`, so the FCT bootstrap's `_maybe_create_initial_chat` uses the user's chosen name instead of the bake's placeholder.
- Changed: The bake's services agent now uses the constant name `system-services` (was per-bake `pool-<hex>` UUID); the bake also destroys the FCT-bootstrap-created chat agent and `rm -f`'s `/code/runtime/initial_chat_created` so the user's first start re-fires the bootstrap cleanly.
- Changed: `_get_agent_info` now takes `host_name` as a keyword arg and filters by both `name` and `host.name`, so the operator's local mngr state accumulating one `system-services` agent per bake no longer routes subsequent calls to the wrong VPS.
- Changed: Provider's `get_host_and_agent_details` override (and its lease-only `_build_offline_details_from_lease` fallback) now accepts and forwards the new `offline_field_generators` parameter, so offline plugin fields are populated for leased hosts that fall back to offline/lease-only data.
- Changed: Added to the release tooling's publish graph (`scripts/utils.py`); will be offered for first publication to PyPI on the next release. Previously-unpinned internal deps (`imbue-mngr-vps-docker`, `imbue-common`) are now pinned with `==` to their current workspace versions, as a published wheel requires. No runtime change.

### Removed

- Removed: Dead `build_combined_inject_command`, `normalize_inject_args`, `_sed_replace_env_line`, and `_ensure_no_quote_chars` helpers on `ImbueCloudHost` (and the now-empty `host_test.py`); the central `MINDS_API_KEY` is injected on the fly by the latchkey gateway's `minds-api-proxy` extension and no longer needs to be pushed onto leased pool hosts.

### Fixed

- Fixed: `pool_hosts` INSERT now picks up the schema's `host_name` column; every successful pool bake had been dying at the last step with `null value in column "host_name"` and leaking a fully-provisioned VPS.
- Fixed: Multi-token `mngr exec` commands packed into a single `shlex.join`'d positional string so click no longer eats `--force` as a `mngr exec` option.
- Fixed: `mngr imbue_cloud auth oauth` no longer hangs until the 300s timeout after the browser already returned the OAuth code. The local callback listener now only records query params when the request is for `/oauth/callback` with non-empty params, so secondary GETs (favicon, prefetches) can no longer overwrite the captured callback with `{}`.
- Fixed: Imbue_cloud slow (rebuild) path no longer trips on `python3: not found`. When `fast_mode=prevent` leased a host and rebuilt its container, the rebuilt host was still marked as carrying a pre-baked agent, so `provision_agent` took the minimal "adopt" path against the freshly-rebuilt container. The slow path now builds the host object with `adopt_pre_baked_agent=False`, so `pre_baked_agent_id` is unset and mngr runs its standard full create + provision pipeline.
- Fixed: Pool-host bake no longer writes the wrong value into `pool_hosts.vps_instance_id` — the INSERT was passing the mngr `host_id` where the OVH service name belongs, which broke every connector-side OVH teardown. The bake now writes `vps_address` (the service name) via a new pure `build_pool_host_insert_values()`, pinned by a regression test using the real `host-`/`vps-` shapes.

## [v0.2.8] - 2026-05-13

### Changed

- Changed: `mngr list` for `imbue_cloud` now drives discovery through outer (VPS-root) SSH instead of inner-container SSH, showing true container state (`RUNNING`/`STOPPED`/`CRASHED`/`PAUSED`/`DESTROYED`) and full details even when inner sshd is unreachable.

## [v0.2.7] - 2026-05-11

### Added

- Added: New `mngr_imbue_cloud` plugin with `mngr imbue_cloud` CLI (`auth`, `hosts`, `keys litellm`, `tunnels`, `admin pool`) that owns SuperTokens auth, pool-host leasing, LiteLLM keys, and Cloudflare tunnels; multi-account modelled as multiple provider instances.

### Changed

- Changed: `mngr imbue_cloud admin pool create` post-create read-back is now scoped to `--provider` (default `vultr`) and uses `--on-error continue`; broken `just create-pool-hosts*` recipes and `apps/remote_service_connector/scripts/create_pool_hosts.py` deleted in favour of the plugin command.
