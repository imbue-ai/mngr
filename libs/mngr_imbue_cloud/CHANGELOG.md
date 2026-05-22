# Changelog - mngr_imbue_cloud

A concise, human-friendly summary of changes for the `mngr_imbue_cloud` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Changed

- Changed: Swapped the imbue-cloud pool bake walker from Vultr to OVH — `mngr imbue_cloud admin pool create` is now provider-generic, requires `--region` and repeatable `--tag KEY=VALUE`, lands on `--template main --template ovh` with `--provider ovh`, and installs `ufw` on every leased VPS before the row hits `pool_hosts`.
- Changed: Bake produces a leasable state aligned with the adopt path — services agent renamed to constant `system-services`, FCT-bootstrap-created chat agent is destroyed at bake time, and subsequent `mngr stop` / `mngr exec` calls use the full `system-services@<host_name>.ovh` address.
- Changed: `ImbueCloudProvider.create_host` now SFTPs into the leased container and rewrites `/mngr/data.json`'s `host_name` to the user-supplied `HostName` so the FCT bootstrap's freshly-recreated chat agent uses the user's chosen workspace name.
- Changed: Bumped pinned `imbue-mngr` / `imbue-common` / `concurrency-group` versions to match the current monorepo.
- Changed: Adopted per-project changelog layout (`changelog/` dir, `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md` at the project root).

### Fixed

- Fixed: `pool_hosts` INSERT now picks up the schema's `host_name` column — every successful pool bake previously died at the last step with a NOT-NULL violation, leaking a fully-provisioned VPS because the cleanup path doesn't run on psycopg2 errors. SQL is now a module-level constant covered by a regression test.

## [v0.2.8] - 2026-05-13

### Changed

- Changed: `mngr list` for `imbue_cloud` now drives discovery through outer (VPS-root) SSH instead of inner-container SSH, showing true container state (`RUNNING`/`STOPPED`/`CRASHED`/`PAUSED`/`DESTROYED`) and full details even when inner sshd is unreachable.

## [v0.2.7] - 2026-05-11

### Added

- Added: New `mngr_imbue_cloud` plugin with `mngr imbue_cloud` CLI (`auth`, `hosts`, `keys litellm`, `tunnels`, `admin pool`) that owns SuperTokens auth, pool-host leasing, LiteLLM keys, and Cloudflare tunnels; multi-account modelled as multiple provider instances.

### Changed

- Changed: `mngr imbue_cloud admin pool create` post-create read-back is now scoped to `--provider` (default `vultr`) and uses `--on-error continue`; broken `just create-pool-hosts*` recipes and `apps/remote_service_connector/scripts/create_pool_hosts.py` deleted in favour of the plugin command.
