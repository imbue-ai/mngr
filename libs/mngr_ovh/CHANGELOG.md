# Changelog - mngr_ovh

A concise, human-friendly summary of changes for the `mngr_ovh` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Changed

- Changed: Unauthenticated OVH provider now raises the shared `ProviderNotAuthorizedError` at construction, instead of silently returning an empty listing (exit 0). Reported consistently with the other cloud providers (one consistent error line in `mngr list`, contributing a non-zero exit) rather than vanishing.

## [v0.1.6] - 2026-06-18

## [v0.1.5] - 2026-06-16

### Changed

- Changed: `mngr ovh list` now reads its defaults from the user's `[providers.<name>]` settings.toml block (selected with `--provider`, default `ovh`), matching `mngr aws prepare` / `mngr gcp prepare` / `mngr azure prepare`. Previously it built `OvhProviderConfig()` with class defaults unconditionally, so a user who pinned a non-default `endpoint` / `ovh_subsidiary` (e.g. `ovh-eu`) would inspect a different account than `mngr create --provider <name>` actually used. Credentials still fall back to env / `~/.ovh.conf`; a warning is logged if the named `--provider` block exists but is not an OVH backend.
- Changed: `mngr ovh list` groups OVH-specific options (`--provider`, `--all`) under a "Provider" option group, so `--help` and the generated docs list them ahead of the shared common options.
- Changed: OVH release-test settings now also disable the `azure` provider (`[providers.azure] is_enabled = false`), mirroring the existing gcp/aws/vultr disables, so `mngr list` inside the OVH lifecycle tests no longer exits non-zero when Azure credentials aren't resolvable.

### Removed

- Removed: Dead `create_snapshot`, `delete_snapshot`, `list_snapshots`, and `list_ssh_keys` stubs (and the now-unused `_safe_get_snapshot` and `_snapshot_info_from_payload` helpers) from `OvhVpsClient`, matching the removal of those abstract methods from the shared `VpsClientInterface`.

## [v0.1.4] - 2026-06-16

## [v0.1.3] - 2026-06-15

## [v0.1.2] - 2026-06-13

### Changed

- Changed: **Breaking** -- per-host build args renamed: `--vps-datacenter=` is now `--ovh-datacenter=` (`--ovh-region=` accepted as an alias) and `--vps-plan=` is now `--ovh-plan=`. The old `--vps-*` prefix raises a migration error. `--git-depth=` stays shared.
- Changed: `vps_boot_timeout` config field renamed to `instance_boot_timeout` (matches the shared base-config rename).

## [v0.1.1] - 2026-06-08

### Added

- Added: Auto-discovered as a publishable package by the release tooling (peer of the already-published `mngr_vultr`); will be offered for first publication to PyPI on the next release.

### Changed

- Changed: OVH provisioning now applies the shared `mngr_vps_docker` host-setup steps over SSH (OVH has no cloud-init). With this change OVH installs the pinned Docker version, registers gVisor `runsc` when `install_gvisor_runtime` is set, tunes sshd, installs the required outer packages, and purges qemu — all from the single shared source of truth. This closes a real gap: OVH never installed `runsc` before, so `[providers.ovh] install_gvisor_runtime = true` was silently a no-op and OVH-baked hosts (including the imbue_cloud pool) ran the agent container under the default runtime.

### Removed

- Removed: OVH-specific `install_required_outer_packages` and `purge_qemu_packages` bootstrap helpers — their behavior is now folded into the shared host-setup step list as config-gated steps.

## [v0.1.0] - 2026-06-05

### Fixed

- Fixed: Discovery no longer masks failures as "zero hosts" — `_list_provider_vps_hostnames` previously caught any IAM-listing error and returned an empty list, so a transient OVH outage / expired credentials looked identical to a real empty result and defeated mngr's "mark hosts UNKNOWN when a provider's discovery fails" safeguard. It now lets the error propagate so `mngr list --on-error continue` records the failure instead of silently dropping live hosts.

### Added

- Added: New `mngr_ovh` provider plugin that runs mngr agents in Docker containers on OVH classic VPS instances (e.g. `vps-2025-model1` / "VPS-1" at ~$7.99/mo). Uses `python-ovh`, supports OAuth2 / AK-AS-CK / `~/.ovh.conf` credentials, provisions via `/order/cart` + `POST /vps/{s}/rebuild`, discovers via OVH IAM v2 tags, and TOFU-pins the host key on first SSH.
- Added: `mngr create --provider ovh` automatically reuses a cancelled-but-still-alive OVH VPS (the leftover from a prior `mngr destroy` that OVH won't actually decommission until end of month) instead of ordering a fresh one; gated by `enable_recycle_cancelled`, `recycle_safety_margin_hours`, and `recycle_max_candidates_considered`.
- Added: `mngr ovh list [--all]` operator command — shows every mngr-tagged OVH VPS in the account with plan, datacenter, state, expiration, cancellation status, and IAM tags.
- Added: `MNGR_VPS_EXTRA_TAGS=k1=v1,k2=v2` support that attaches each entry as an OVH IAM v2 tag alongside `mngr-provider` / `mngr-host-id`; strict parsing with local key validation so typos fail before the API call.
- Added: `install_required_outer_packages` helper that runs as the final outer step before `VpsDockerProvider.create_host` takes over, installing `rsync` on the OVH `Debian 12 - Docker` image which doesn't ship it.

### Changed

- Changed: `OvhProviderConfig.recycle_safety_margin_hours` default drops 24 → 2 so same-day destroy + create reclaims the cancelled VPS instead of ordering a fresh month.
- Changed: VPS ordering no longer diffs `/vps` listings to find the new service — it tracks the specific order, so two concurrent orders against the same OVH account can never swap services.
- Changed: `OvhVpsClient.set_renew_at_expiration` now retries on the OVH transient 400 `"Unable to synchronize l1::Service, subscription is not active yet"` (5-minute default budget, 15 s poll interval, both injectable).
- Changed: `parse_extra_tags_env(MNGR_VPS_EXTRA_TAGS)` now runs at the top of `_provision_vps`, before any OVH API call, so a typo fails before we pay for a VPS.
- Changed: `OuterHost.get_name` / `OuterHostInterface.get_name` now return `str` instead of `HostName` (the outer host's name is an SSH hostname / IP address that routinely contains dots).
- Changed: **Breaking** — OVH hosts created by `mngr create --provider ovh` now back their per-host unified docker volume with a btrfs subvolume on a loop-mounted btrfs filesystem on the VPS (`/mngr-btrfs/<host_id_hex>` on `/var/lib/mngr-btrfs.img`), enabling consistent `btrfs subvolume snapshot -r` of agent data. See `mngr_vps_docker`'s changelog for the full mechanism. Existing OVH hosts created on the prior layout cannot be discovered or managed after upgrade — destroy and recreate them.
- Changed: Added `inotify-tools` and `jq` to `_REQUIRED_OUTER_PACKAGES` so the new `snapshot_helper.service` (provisioned by `mngr_vps_docker`) has the tools it needs on OVH-leased outers.
- Changed: OVH-provisioned hosts now have OVH automated backups disabled. As the final bootstrap step the OVH provider purges all `qemu*` packages over SSH on each freshly-ordered or recycled VPS, removing the `qemu-guest-agent` that OVH backups use to freeze the guest filesystem (which caused serious runtime problems on the agent). A failure aborts provisioning so no host is left running with backups enabled. Existing already-running OVH hosts are not swept; they pick up the purge when next recycled.
- Changed: `OvhVpsClient.set_renew_at_expiration` also retries on transient transport failures (dropped connection / timeout), not just the "subscription is not active yet" billing-propagation case, hardening the failure-cleanup cancel path against leaking a freshly-ordered month of billing. Non-transient API errors still surface immediately.
- Changed: Added to the release tooling's publish graph (`scripts/utils.py`); will be offered for first publication to PyPI on the next release. No runtime change.

### Fixed

- Fixed: Post-delivery race — `order_and_wait_for_vps` no longer returns until the background `deliverVm` task drains, so the immediately-following `/rebuild` no longer fails with "Action not available while there are running tasks on the VPS". `rebuild_vps_with_public_key` performs the same drain as a pre-flight.
- Fixed: `destroy_instance` now actually cancels the VPS via `PUT /serviceInfos` (`renew.deleteAtExpiration=true`) instead of `POST /terminate` (which only emailed a confirmation token).
- Fixed: `set_renew_at_expiration(False)` now also restores `renew.automatic=true` and `renewalType=automaticV2012`, which OVH silently auto-flips when `deleteAtExpiration` goes to `true`.
- Fixed: OVH `Debian 12 - Docker` image installs the rebuild SSH key into `/home/debian/.ssh/authorized_keys` rather than `/root/.ssh/authorized_keys`; the provider now sudo-copies the key into root's home during provisioning (configurable via new `bootstrap_ssh_user`, default `debian`).
- Fixed: OVH IAM tags are now attached immediately after the VPS appears in `GET /vps` so a failure during rebuild / TOFU / bootstrap leaves a discoverable orphan instead of an invisible VPS.
- Fixed: SSH-as-bootstrap-user / SSH-as-root paramiko sessions now load the private key with a type-agnostic helper that tries Ed25519, RSA, and ECDSA in turn (was hardcoded to `Ed25519Key`).
- Fixed: Fresh-order pool bakes no longer fail intermittently with "Action not available while there are running tasks on the VPS". OVH's task listing is eventually consistent, so the pre-`/rebuild` drain could report no active tasks while OVH still rejected the rebuild because the post-delivery `deliverVm` task was in flight. The rebuild POST is now retried (re-draining each round, up to 5 minutes) until OVH accepts it.
- Fixed: Recycled OVH VPSes now receive the new bake's extra IAM tags (e.g. `minds_env=<env>`), overwriting any stale value left by the previous owner. Previously the recycle path only swapped `mngr-host-id` and skipped extra tags entirely, so a pool host provisioned by recycling a cancelled VPS carried no `minds_env` tag (or a stale one), making it invisible to env-scoped discovery / teardown.
