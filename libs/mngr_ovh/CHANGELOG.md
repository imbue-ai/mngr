# Changelog - mngr_ovh

A concise, human-friendly summary of changes for the `mngr_ovh` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: New `mngr_ovh` provider plugin that runs mngr agents in Docker containers on OVH classic VPS instances (e.g. `vps-2025-model1` at ~$7.99/mo). Uses the official `python-ovh` SDK; supports OAuth2, AK/AS/CK, and `~/.ovh.conf` credentials; provisions via the OVH `/order/cart` flow and bootstraps via `POST /vps/{s}/rebuild`.
- Added: Cancelled-but-still-alive OVH VPS reuse — `mngr create --provider ovh` automatically reclaims a leftover destroyed VPS instead of ordering a fresh month. Controlled by `enable_recycle_cancelled` (default on), `recycle_safety_margin_hours` (default 2), and `recycle_max_candidates_considered`.
- Added: `mngr ovh list [--all]` operator command showing every mngr-tagged OVH VPS in the account (or every VPS with `--all`) with plan, datacenter, state, expiration, cancellation status, and IAM tags.
- Added: `MNGR_VPS_EXTRA_TAGS=k1=v1,k2=v2` env var honored as OVH IAM v2 tags alongside `mngr-provider` / `mngr-host-id`; strict pre-order validation.
- Added: `install_required_outer_packages` helper that installs `rsync` as the final outer-bootstrap step on the OVH Debian 12 Docker image (no cloud-init available).

### Changed

- Changed: Project adopted the per-project changelog layout (`changelog/`, `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md` at the project root).
- Changed: `OuterHost.get_name` / `OuterHostInterface.get_name` now return `str` (not `HostName`) — the outer host name is the connector target (SSH hostname or IP), which routinely contains dots and was rejected by the `HostName` validator. `Host.get_name` still returns `HostName`.

### Fixed

- Fixed: `parse_extra_tags_env` now runs at the very top of `_provision_vps`, before any OVH API call — a typo in `MNGR_VPS_EXTRA_TAGS` no longer raises only after we'd already ordered + paid for a fresh-month VPS. Pinned by a source-position test.
- Fixed: `OvhVpsClient.set_renew_at_expiration` retries on the OVH transient 400 message `"Unable to synchronize l1::Service, subscription is not active yet"` (default 5-minute budget, 15s poll); other 400s/404s/5xxs propagate immediately. Prevents the cleanup race after fresh-order failures from leaking a freshly-ordered month of billing.
- Fixed: `order_and_wait_for_vps` now correlates via `orderId` + operations chain instead of diffing `/vps` listings — two concurrent orders against the same OVH account can no longer swap serviceNames. Belt-and-suspenders verify post-fetch (model + datacenter must match).
- Fixed: Post-delivery race — `order_and_wait_for_vps` waits for the background `deliverVm` task to drain so the following `/rebuild` no longer fails with "Action not available while there are running tasks on the VPS"; `rebuild_vps_with_public_key` performs the same drain as a pre-flight.
- Fixed: `destroy_instance` now actually cancels the VPS via `PUT /serviceInfos` (`renew.deleteAtExpiration=true`) instead of `POST /terminate` (which only emails a confirmation token).
- Fixed: `set_renew_at_expiration(False)` now also restores `renew.automatic=true` / `renewalType=automaticV2012` so a recycled VPS auto-renews at the next anniversary.
- Fixed: OVH `Debian 12 - Docker` image — provider sudo-copies the rebuild SSH key from `/home/debian/.ssh/authorized_keys` into `/root/.ssh/authorized_keys` during provisioning (configurable via new `bootstrap_ssh_user`), so the rest of the provider runs as root.
- Fixed: OVH `mngr-provider` / `mngr-host-id` IAM tags are now attached immediately after the VPS appears in `GET /vps` (before rebuild + TOFU + root-bootstrap), so failures during later steps leave a discoverable orphan instead of an invisible VPS.
- Fixed: SSH key loading uses a type-agnostic helper that tries Ed25519, RSA, and ECDSA in turn (was hardcoded to `Ed25519Key.from_private_key_file`).
