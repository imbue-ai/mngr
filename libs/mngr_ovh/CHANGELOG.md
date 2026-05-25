# Changelog - mngr_ovh

A concise, human-friendly summary of changes for the `mngr_ovh` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: New `mngr_ovh` provider plugin — runs mngr agents in Docker containers on OVH classic VPS instances (e.g. `vps-2025-model1`). Uses the `python-ovh` SDK; supports OAuth2, AK/AS/CK, and `~/.ovh.conf` credentials. Provisions via `/order/cart`, bootstraps via `POST /vps/{s}/rebuild`, and discovers via OVH IAM v2 tags.
- Added: `mngr ovh list [--all]` operator command — shows every mngr-tagged OVH VPS (or every VPS with `--all`) with plan, datacenter, state, expiration, cancellation status, and IAM tags.
- Added: Cancelled-but-still-alive OVH VPS auto-recycle on `mngr create --provider ovh` (controlled by `enable_recycle_cancelled`, `recycle_safety_margin_hours`, `recycle_max_candidates_considered`).
- Added: `MNGR_VPS_EXTRA_TAGS=k1=v1,k2=v2` env var support; entries attach as OVH IAM v2 tags alongside `mngr-provider` / `mngr-host-id`. Strict local IAM-key validation so typos fail before the API call.
- Added: `install_required_outer_packages` helper in `mngr_ovh.bootstrap` installs `rsync` as the final outer step (OVH's Debian 12 Docker image ships docker but not rsync, which `mngr_vps_docker`'s build-context upload needs).

### Changed

- Changed: `OvhProviderConfig.recycle_safety_margin_hours` default drops 24 → 2 so same-day destroy + create reclaims the cancelled VPS instead of ordering a fresh month.
- Changed: `OuterHost.get_name` / `OuterHostInterface.get_name` now return `str` instead of `HostName` (outer host names routinely contain dots that `HostName` rejects).

### Fixed

- Fixed: `MNGR_VPS_EXTRA_TAGS` parse now runs at the top of `_provision_vps` (before `_maybe_claim_recycled_vps` and any OVH API call) so a typo no longer raises only after a fresh-month VPS has been ordered.
- Fixed: `OvhVpsClient.set_renew_at_expiration` retries on OVH's transient `"Unable to synchronize l1::Service, subscription is not active yet"` 400 so the cleanup path doesn't lose the race and leak a freshly-ordered month of billing.
- Fixed: `order_and_wait_for_vps` walks the `/me/order/{orderId}/details/...` operations chain to find the assigned serviceName (strongly correlated to OUR `orderId`), instead of diffing `/vps` listings — two concurrent orders against the same OVH account can no longer swap serviceNames.
- Fixed: Post-delivery race — `order_and_wait_for_vps` no longer returns until the background `deliverVm` task drains; `rebuild_vps_with_public_key` performs the same drain as a pre-flight.
- Fixed: `destroy_instance` now actually cancels the VPS via `PUT /serviceInfos` (`renew.deleteAtExpiration=true`); the legacy `/terminate` only emailed a confirmation token.
- Fixed: `set_renew_at_expiration(False)` also restores `renew.automatic=true` and `renewalType=automaticV2012` so a recycled VPS auto-renews at the next anniversary.
- Fixed: OVH's `Debian 12 - Docker` image installs the rebuild SSH key into `/home/debian/.ssh/authorized_keys` rather than root — provider now sudo-copies the key into root's home during provisioning (configurable via `bootstrap_ssh_user`).
- Fixed: OVH IAM `mngr-provider` / `mngr-host-id` tags now attach immediately after the VPS appears in `GET /vps`, before rebuild + TOFU + root-bootstrap — failures during later steps now leave a discoverable orphan instead of an invisible VPS.
- Fixed: SSH-as-bootstrap-user / SSH-as-root paramiko sessions load private keys with a type-agnostic helper (tries Ed25519, RSA, ECDSA in turn), unmasking the RSA-key crash uncovered after fixing the post-delivery race.
