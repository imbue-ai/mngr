# Changelog - mngr_ovh

A concise, human-friendly summary of changes for the `mngr_ovh` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: New `mngr_ovh` provider plugin that runs mngr agents in Docker containers on OVH classic VPS instances (e.g. `vps-2025-model1` at ~$7.99/mo). Supports OAuth2, AK/AS/CK, and `~/.ovh.conf` credentials; provisions via `/order/cart` and bootstraps via `POST /vps/{s}/rebuild`; discovers via OVH IAM v2 tags.
- Added: `mngr ovh list [--all]` operator command showing every mngr-tagged OVH VPS (or every VPS with `--all`) with plan, datacenter, state, expiration, cancellation status, and IAM tags.
- Added: `mngr create --provider ovh` automatically reuses cancelled-but-still-alive OVH VPSes (controlled by `enable_recycle_cancelled`, `recycle_safety_margin_hours`, and `recycle_max_candidates_considered`).
- Added: `install_required_outer_packages` helper in `mngr_ovh.bootstrap` that installs `rsync` (and any other required packages) on the OVH outer host as the final outer step before `VpsDockerProvider.create_host` takes over.

### Changed

- Changed: `OvhProvider` now honours `MNGR_VPS_EXTRA_TAGS=k1=v1,k2=v2`, attaching each entry as an OVH IAM v2 tag; parse is strict and runs at the top of `_provision_vps` so typos fail before any OVH API call.
- Changed: `OvhProviderConfig.recycle_safety_margin_hours` default lowered from 24 to 2 so same-day destroy + create reclaims the cancelled VPS instead of ordering a fresh month.
- Changed: `OuterHost.get_name` and `OuterHostInterface.get_name` now return `str` instead of `HostName` (outer host names routinely contain dots like `vps-x.vps.ovh.us`); `Host.get_name` continues to return `HostName`.
- Changed: Adopted per-project changelog layout (`changelog/` dir, `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md` at the project root).

### Fixed

- Fixed: `order_and_wait_for_vps` no longer diffs `/vps` listings — it walks the `/me/order/{orderId}/details/{detailId}/...` chain matching on `extension.order.plan.code` so two concurrent orders against the same OVH account can't swap serviceNames; the function additionally `GET /vps/{serviceName}` to verify model and datacenter.
- Fixed: `OvhVpsClient.set_renew_at_expiration` now retries on OVH's transient "Unable to synchronize l1::Service, subscription is not active yet" 400 message so cleanup of an orphaned fresh order doesn't silently leak a month of billing.
- Fixed: Post-delivery race resolved — `order_and_wait_for_vps` no longer returns until the background `deliverVm` task drains, so the immediately-following `/rebuild` no longer fails with "Action not available while there are running tasks on the VPS".
- Fixed: `destroy_instance` now actually cancels the VPS via `PUT /serviceInfos` (`renew.deleteAtExpiration=true`) instead of the legacy `POST /terminate` (which only emails a confirmation token).
- Fixed: `set_renew_at_expiration(False)` now also restores `renew.automatic=true` and `renewalType=automaticV2012`, which OVH silently auto-flips when `deleteAtExpiration` goes to `true`.
- Fixed: OVH's `Debian 12 - Docker` image installs the rebuild SSH key into `/home/debian/.ssh/authorized_keys`; the provider now sudo-copies the key into root's home during provisioning (configurable via the new `bootstrap_ssh_user` field).
- Fixed: `mngr-provider` / `mngr-host-id` IAM tags are now attached immediately after the VPS appears in `GET /vps`, before rebuild + TOFU + root-bootstrap, so failures during those later steps leave an orphan VPS that's still discoverable.
- Fixed: SSH paramiko sessions load the private key with a type-agnostic helper trying Ed25519, RSA, and ECDSA in turn (was hardcoded to `Ed25519Key`).
- Fixed: `parse_extra_tags_env` for `MNGR_VPS_EXTRA_TAGS` now runs at the top of `_provision_vps` before any OVH API call, so a typo no longer aborts only after we've already ordered a fresh-month VPS.
