# Changelog - mngr_ovh

A concise, human-friendly summary of changes for the `mngr_ovh` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: New `mngr_ovh` provider plugin — runs mngr agents in Docker containers on OVH classic VPS instances (e.g. `vps-2025-model1` at ~$7.99/mo). Uses `python-ovh` (OAuth2 / AK-AS-CK / `~/.ovh.conf`), provisions via `/order/cart` + `POST /vps/{s}/rebuild`, discovers via OVH IAM v2 tags, and TOFU-pins the host key into a per-provider `known_hosts`.
- Added: `mngr create --provider ovh` auto-reuses a cancelled-but-still-alive OVH VPS instead of ordering a fresh month (config knobs: `enable_recycle_cancelled`, `recycle_safety_margin_hours`, `recycle_max_candidates_considered`).
- Added: `mngr ovh list [--all]` operator command — table of every mngr-tagged OVH VPS (or every VPS with `--all`) with plan, datacenter, state, expiration, cancellation status, and IAM tags.
- Added: `MNGR_VPS_EXTRA_TAGS=k1=v1,k2=v2` env var honored on OVH VPS creation; strict local IAM-key validation rejects typos before the API call.
- Added: `install_required_outer_packages` helper in `mngr_ovh.bootstrap` installs `rsync` as the final outer step (the OVH `Debian 12 - Docker` image ships docker but not rsync).

### Changed

- Changed: `parse_extra_tags_env(...)` now runs at the top of `_provision_vps` before any OVH API call, so typos in `MNGR_VPS_EXTRA_TAGS` fail before a fresh-month VPS is ordered.
- Changed: `OvhProviderConfig.recycle_safety_margin_hours` default dropped 24 → 2 so same-day destroy + create reclaims the cancelled VPS.
- Changed: `OuterHost.get_name` / `OuterHostInterface.get_name` now return `str` so SSH hostnames/IPs containing dots (e.g. `vps-x.vps.ovh.us`) survive; the `Host` subclass still returns `HostName`.
- Changed: Project now participates in the per-project changelog layout (per-project `changelog/`, `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md`).

### Fixed

- Fixed: `order_and_wait_for_vps` now correlates by `orderId` (walking `/me/order/{orderId}/details/.../operations/{opId}` and matching `extension.order.plan.code`) instead of diffing `/vps` listings — concurrent orders against the same OVH account can no longer swap serviceNames; post-hoc verifies `model.name` and datacenter zone.
- Fixed: `OvhVpsClient.set_renew_at_expiration` retries on the transient OVH 400 `"Unable to synchronize l1::Service, subscription is not active yet"` for up to 5 minutes (configurable), so cleanup of an orphaned fresh order no longer loses the race and leaks a month of billing.
- Fixed: `order_and_wait_for_vps` waits for the background `deliverVm` task to drain before returning, so the immediately-following `/rebuild` no longer fails with "Action not available while there are running tasks on the VPS".
- Fixed: `destroy_instance` now actually cancels via `PUT /serviceInfos` (`renew.deleteAtExpiration=true`) — the legacy `POST /terminate` only emailed a confirmation token, so VPSes auto-renewed indefinitely without a human confirming.
- Fixed: `set_renew_at_expiration(False)` also restores `renew.automatic=true` / `renewalType=automaticV2012` (OVH silently flips these when `deleteAtExpiration` goes true).
- Fixed: Rebuild SSH key now sudo-copied into root's `~/.ssh/authorized_keys` (OVH's `Debian 12 - Docker` installs the key into `/home/debian/.ssh/authorized_keys`); configurable via `bootstrap_ssh_user` (defaults to `debian`).
- Fixed: `mngr-provider` / `mngr-host-id` IAM tags now attached immediately after the VPS appears in `GET /vps`, before rebuild + TOFU + root-bootstrap, so failed provisions leave a discoverable orphan rather than an invisible one.
- Fixed: SSH-as-bootstrap-user / SSH-as-root paramiko sessions now load private keys with a type-agnostic helper (Ed25519, RSA, ECDSA) instead of hardcoded `paramiko.Ed25519Key`.
