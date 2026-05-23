# Changelog - mngr_imbue_cloud

A concise, human-friendly summary of changes for the `mngr_imbue_cloud` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Changed

- Changed: `mngr imbue_cloud admin pool create` is now provider-generic — drops `MINDS_ROOT_NAME` env detection, adds a required `--region` and repeatable `--tag`, lands on `--template main --template ovh` with `@host.ovh`, and installs/configures `ufw` on every leased VPS before the row hits `pool_hosts`.
- Changed: `mngr_imbue_cloud` no longer triggers a silent auto-disable on `ImbueCloudAuthError`; the error now propagates to the minds providers panel for explicit user action.
- Changed: Bumped pinned `imbue-mngr` / `imbue-common` / `concurrency-group` versions to match the current monorepo.

### Fixed

- Fixed: `pool_hosts` INSERT now writes the schema's required `host_name` column — every successful pool bake was previously dying at the very last step with `null value in column "host_name"`, leaking a fully-provisioned VPS each time. SQL extracted into a `_INSERT_POOL_HOST_SQL` constant with a regression test.
- Fixed: Bake's services agent now uses the constant `system-services` name (matching the user's adopt-time `mngr create` invocation) and tears down the FCT-bootstrap-created chat agent + `initial_chat_created` sentinel so the user's first start hydrates fresh chat agents under the correct host name.
- Fixed: `_get_agent_info` now filters by both agent name and `host.name` so sequential bakes on different VPSes can't return a prior bake's stale agent.
- Fixed: `ImbueCloudProvider.create_host` SFTPs into the leased container and rewrites `/mngr/data.json`'s `host_name` to the user-supplied value so the FCT bootstrap's first-start chat-agent naming uses the user's workspace name.
- Fixed: Multi-token `mngr exec` commands are packed into a single `shlex.join`'d positional string (avoids Click's `AGENTS... COMMAND` parser munging the inner `mngr destroy <name> --force`).

## [v0.2.8] - 2026-05-13

### Changed

- Changed: `mngr list` for `imbue_cloud` now drives discovery through outer (VPS-root) SSH instead of inner-container SSH, showing true container state (`RUNNING`/`STOPPED`/`CRASHED`/`PAUSED`/`DESTROYED`) and full details even when inner sshd is unreachable.

## [v0.2.7] - 2026-05-11

### Added

- Added: New `mngr_imbue_cloud` plugin with `mngr imbue_cloud` CLI (`auth`, `hosts`, `keys litellm`, `tunnels`, `admin pool`) that owns SuperTokens auth, pool-host leasing, LiteLLM keys, and Cloudflare tunnels; multi-account modelled as multiple provider instances.

### Changed

- Changed: `mngr imbue_cloud admin pool create` post-create read-back is now scoped to `--provider` (default `vultr`) and uses `--on-error continue`; broken `just create-pool-hosts*` recipes and `apps/remote_service_connector/scripts/create_pool_hosts.py` deleted in favour of the plugin command.
