# Changelog - mngr_imbue_cloud

A concise, human-friendly summary of changes for the `mngr_imbue_cloud` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Changed

- Changed: `mngr imbue_cloud admin pool create` is now provider-generic — drops `MINDS_ROOT_NAME` env detection, adds required `--region` and repeatable `--tag KEY=VALUE`, lands on `--template ovh` with `@host.ovh`, and installs+configures `ufw` on every leased VPS before the row hits `pool_hosts`.
- Changed: Bake now uses the constant `system-services` agent name (was a per-bake `pool-<hex>` UUID) so the leased workspace's tmux sessions match the user's expected name; the per-bake unique suffix stays on the host name.
- Changed: Bake destroys the FCT-bootstrap-created chat agent and `rm -f`'s `runtime/initial_chat_created` after key-injection so the user's first start fires the bootstrap fresh with the correct host_name and a valid claude config.
- Changed: Bake's `mngr stop` / `mngr exec` calls use the full `system-services@<host>.ovh` address so sequential bakes don't target a stale operator-local agent. `_get_agent_info` now filters by both `name` and `host.name`.
- Changed: Bumped pinned `imbue-mngr` / `imbue-common` / `concurrency-group` versions.
- Changed: `ImbueCloudAuthError` from discovery still raises but minds no longer silently auto-disables the offending provider; the user can disable it explicitly via the providers panel.

### Fixed

- Fixed: `pool_hosts` INSERT in `_create_single_pool_host` now populates the schema's `host_name` column — every successful pool bake had been dying at the last step with a not-null violation, leaving a fully-provisioned VPS leaked. SQL is now extracted into a module-level constant with a regression test asserting every required column.
- Fixed: `ImbueCloudProvider.create_host` SFTPs into the leased container after host-key scan and rewrites `/mngr/data.json`'s `host_name` field to the user-supplied `HostName`, so the FCT bootstrap's first-start chat agent picks up the user's workspace name instead of the bake's placeholder.
- Fixed: Multi-token `mngr exec` commands are packed into a single `shlex.join`'d positional string so the inner `mngr destroy <name> --force` is not misparsed by Click as additional agent names.

## [v0.2.8] - 2026-05-13

### Changed

- Changed: `mngr list` for `imbue_cloud` now drives discovery through outer (VPS-root) SSH instead of inner-container SSH, showing true container state (`RUNNING`/`STOPPED`/`CRASHED`/`PAUSED`/`DESTROYED`) and full details even when inner sshd is unreachable.

## [v0.2.7] - 2026-05-11

### Added

- Added: New `mngr_imbue_cloud` plugin with `mngr imbue_cloud` CLI (`auth`, `hosts`, `keys litellm`, `tunnels`, `admin pool`) that owns SuperTokens auth, pool-host leasing, LiteLLM keys, and Cloudflare tunnels; multi-account modelled as multiple provider instances.

### Changed

- Changed: `mngr imbue_cloud admin pool create` post-create read-back is now scoped to `--provider` (default `vultr`) and uses `--on-error continue`; broken `just create-pool-hosts*` recipes and `apps/remote_service_connector/scripts/create_pool_hosts.py` deleted in favour of the plugin command.
