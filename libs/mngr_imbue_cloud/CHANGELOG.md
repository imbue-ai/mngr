# Changelog - mngr_imbue_cloud

A concise, human-friendly summary of changes for the `mngr_imbue_cloud` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.1.3] - 2026-06-15

## [v0.1.2] - 2026-06-13

### Changed

- Changed: A stopped (offline) host's files are now readable through the same interface as an online host (used e.g. by Claude session preservation when a host is destroyed while offline). The host's volume is resolved lazily on first read, so this adds no per-host probe to host discovery; when no volume is available, reads behave as "nothing there".
- Changed: `_build_delegated_vps_provider` now returns a `MinimalVpsDockerProvider` (moved into `mngr_vps_docker`, since it's a generally useful role for any externally-managed-VPS host-setup path). Its `_parse_build_args` extracts `--git-depth=N` and forwards everything else to docker, which is the correct behavior for the no-provisioning path that pairs with `ExternallyManagedVpsClient`; without this, every slow-path container rebuild raised before any docker work happened (the base `_parse_build_args` is `@abstractmethod` now).
- Changed: `mngr imbue_cloud admin pool create` now passes `--ovh-datacenter=` instead of the retired `--vps-datacenter=` to the inner `mngr create --provider ovh`, keeping pool creation working after the OVH provider's per-provider build-arg prefix rename.
- Changed: Replaced direct ValueError/RuntimeError raises in build-arg parsing and host provisioning with dedicated custom exception types.

## [v0.1.1] - 2026-06-08

### Added

- Added: `--no-recycle` flag on `mngr imbue_cloud admin pool create` that forces a fresh OVH VPS order (sets `MNGR__PROVIDERS__OVH__ENABLE_RECYCLE_CANCELLED=false` on the inner `mngr create`) instead of reclaiming a cancelled (still-billable) VPS, for exercising the fresh-provision path.
- Added: Region-aware leasing — `mngr create` against imbue_cloud accepts a hard `-b region=<datacenter>` build arg (lease fails if no host is available in that datacenter), validated against the known OVH-US datacenters (`US-EAST-VA`, `US-WEST-OR`), and applied on both the fast and slow paths. `mngr imbue_cloud admin pool create` records the bake `--region` so the connector can filter on it.
- Added: Auto-discovered as a publishable package by the release tooling; will be offered for first publication to PyPI on the next release.

### Changed

- Changed: Rebuilt containers now run under the gVisor (`runsc`) runtime with `--workdir=/` and `no-new-privileges` hardening args, configured per account by minds bootstrap.
- Changed: The imbue_cloud slow (rebuild) path now re-applies the full idempotent host setup (pinned Docker version, gVisor `runsc` install/registration, sshd tuning, base packages) on the leased VPS before rebuilding the container, so a workspace created via the slow path — even on a host baked before runsc existed — comes up consistent and runs its agent container under gVisor. A failure is fatal.

### Removed

- Removed: The soft `-b preferred_region=<dc>` lease build arg. A lease is now constrained only by the hard `-b region=<dc>` arg; when unset, the lease is region-agnostic.

## [v0.1.0] - 2026-06-05

### Added

- Added: New `mngr imbue_cloud bucket` command group (`create` / `list` / `info` / `destroy`) for managing per-host R2 buckets (paid accounts only), plus `bucket keys create/list/destroy` for minting and revoking bucket-scoped S3 keys (read-only or read-write). `bucket create` returns S3-compatible credentials as JSON; the secret is shown only once and never stored. `bucket destroy` refuses a non-empty bucket and otherwise cascades to revoke its keys.
- Added: A pure helper exposing the rendered host-wipe shell script so it can be unit-tested without an SSH transport.
- Added: New `mngr imbue_cloud admin paid` subcommands for managing the connector's paid-user lists: `paid domain add|remove|list` and `paid email add|remove|list` (with `--paid-only` on list). These talk to the connector's `/paid/*` admin API using the fixed API key read from `$MINDS_PAID_ADMIN_KEY` (or `--api-key`). Matching client methods and a `PaidListEntry` data type are exposed.
- Added: Robust "slow path" for imbue_cloud host leasing, selected by a new `-b fast_mode=require|prevent` build arg. `require` adopts an exactly-matching pre-baked agent (the original fast path); `prevent` (the new default) leases any adequately-sized host and rebuilds its container from the FCT Dockerfile, releasing the lease if setup fails.

### Changed

- Changed: `mngr destroy <agent>` against an imbue_cloud-leased pool host is now terminal rather than a soft `docker stop`. The new flow stops + removes the workspace container, drops the per-host docker volume and btrfs subvolume, prunes the system, wipes `/root` + `/tmp` (preserving `/root/.ssh/authorized_keys`), releases the lease back to the pool, then cleans up local per-host state. Privacy-first ordering wipes data before flipping the row to `released`. `mngr delete <agent>` runs the same flow and is a safe no-op for an already-released lease. Use `mngr stop <agent>` instead to pause the container without releasing the lease.
- Changed: `mngr imbue_cloud admin pool destroy` (and the `minds pool destroy` wrapper) now do a full teardown: cancel the OVH VPS (strip per-lease tags + `deleteAtExpiration`) before dropping the row, so destruction can no longer strand a still-billing VPS. Pass `--skip-vps-cancel` only when the VPS is already gone. The provider's `destroy_host` now also raises when the connector release fails instead of silently cleaning up local state, so a failed release no longer makes mngr "forget" a host whose lease/VPS is still live.
- Changed: Stopped masking errors in the lease/teardown paths — host-listing and host-release failures now raise instead of being swallowed (the create-rollback path still catches release errors explicitly to stay best-effort).
- Changed: Bumped the `imbue-mngr` pin from `0.2.8` to `0.2.10` to align with main's release commit, so building the `apps/minds` ToDesktop bundle from main no longer fails at `uv lock`.
- Changed: Simplified an exception handler now that the host error types are all `MngrError` subclasses. No behavior change.
- Changed: `mngr imbue_cloud admin pool create` is now provider-generic — adds a required `--region REGION` and repeatable `--tag KEY=VALUE`, defaults to the OVH templates/provider, and installs + configures `ufw` on every leased VPS.
- Changed: A leased host now adopts the user-supplied host name (rewritten into the container) so the FCT bootstrap's initial chat uses the user's chosen name instead of the bake's placeholder.
- Changed: The bake's services agent now uses the constant name `system-services`, and the bake clears the FCT bootstrap's initial-chat state so the user's first start re-fires it cleanly.
- Changed: Agent lookup now filters by both agent name and host name, so an operator's local state accumulating one `system-services` agent per bake no longer routes calls to the wrong VPS.
- Changed: Offline plugin fields are now populated for leased hosts that fall back to offline/lease-only data.
- Changed: Added to the release tooling's publish graph; will be offered for first publication to PyPI on the next release. Previously-unpinned internal deps are now pinned, as a published wheel requires. No runtime change.

### Removed

- Removed: Dead env-injection helpers; the central `MINDS_API_KEY` is now injected on the fly by the latchkey gateway's `minds-api-proxy` extension and no longer needs to be pushed onto leased pool hosts.

### Fixed

- Fixed: `pool_hosts` INSERT now picks up the schema's `host_name` column; every successful pool bake had been dying at the last step with `null value in column "host_name"` and leaking a fully-provisioned VPS.
- Fixed: Multi-token `mngr exec` commands packed into a single `shlex.join`'d positional string so click no longer eats `--force` as a `mngr exec` option.
- Fixed: `mngr imbue_cloud auth oauth` no longer hangs until the 300s timeout after the browser already returned the OAuth code. The local callback listener now only records query params when the request is for `/oauth/callback` with non-empty params, so secondary GETs (favicon, prefetches) can no longer overwrite the captured callback with `{}`.
- Fixed: The slow (rebuild) path no longer trips on `python3: not found`. A rebuilt host was wrongly treated as carrying a pre-baked agent, so provisioning took the minimal "adopt" path; it now runs the standard full create + provision pipeline.
- Fixed: Pool-host bake no longer writes the wrong value into the VPS instance id column, which had broken every connector-side OVH teardown.

## [v0.2.8] - 2026-05-13

### Changed

- Changed: `mngr list` for `imbue_cloud` now drives discovery through outer (VPS-root) SSH instead of inner-container SSH, showing true container state (`RUNNING`/`STOPPED`/`CRASHED`/`PAUSED`/`DESTROYED`) and full details even when inner sshd is unreachable.

## [v0.2.7] - 2026-05-11

### Added

- Added: New `mngr_imbue_cloud` plugin with `mngr imbue_cloud` CLI (`auth`, `hosts`, `keys litellm`, `tunnels`, `admin pool`) that owns SuperTokens auth, pool-host leasing, LiteLLM keys, and Cloudflare tunnels; multi-account modelled as multiple provider instances.

### Changed

- Changed: `mngr imbue_cloud admin pool create` post-create read-back is now scoped to `--provider` (default `vultr`) and uses `--on-error continue`; broken `just create-pool-hosts*` recipes and `apps/remote_service_connector/scripts/create_pool_hosts.py` deleted in favour of the plugin command.
