# Changelog - mngr_imbue_cloud

A concise, human-friendly summary of changes for the `mngr_imbue_cloud` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Changed

- Changed: Bake produces a leasable state aligned with the adopt path — services agent uses the constant name `system-services`; bake destroys the FCT-bootstrap chat agent and clears the `initial_chat_created` sentinel so the user's lease bootstraps fresh.
- Changed: `mngr exec` calls in the bake use the full address `system-services@<host_name>.ovh` and `_get_agent_info` filters on both `name` and `host.name` so sequential bakes don't SSH the wrong VPS.
- Changed: Multi-token `mngr exec` commands are packed into a single `shlex.join`'d positional string so the inner command isn't mis-split by click's `AGENTS... COMMAND` parser.
- Changed: `ImbueCloudProvider.create_host` now SFTPs into the leased container after host-key scan and rewrites `/mngr/data.json`'s `host_name` to the user-supplied `HostName` so the FCT bootstrap names the chat agent correctly.
- Changed: Swap imbue-cloud pool bake walker from Vultr to OVH; `mngr imbue_cloud admin pool create` is provider-generic with required `--region`, repeatable `--tag KEY=VALUE`, and lands on `--template main --template ovh --provider ovh`; UFW is installed + configured on every leased VPS before the row hits `pool_hosts`.
- Changed: Project now participates in the per-project changelog layout (per-project `changelog/`, `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md`).

### Fixed

- Fixed: `pool_hosts` INSERT in `_create_single_pool_host` now includes the schema's `host_name NOT NULL` column; SQL extracted to a module-level `_INSERT_POOL_HOST_SQL` constant with a regression test asserting all required columns are present. Previous failures leaked fully-provisioned VPSes because cleanup didn't run on psycopg2 errors.

## [v0.2.8] - 2026-05-13

### Changed

- Changed: `mngr list` for `imbue_cloud` now drives discovery through outer (VPS-root) SSH instead of inner-container SSH, showing true container state (`RUNNING`/`STOPPED`/`CRASHED`/`PAUSED`/`DESTROYED`) and full details even when inner sshd is unreachable.

## [v0.2.7] - 2026-05-11

### Added

- Added: New `mngr_imbue_cloud` plugin with `mngr imbue_cloud` CLI (`auth`, `hosts`, `keys litellm`, `tunnels`, `admin pool`) that owns SuperTokens auth, pool-host leasing, LiteLLM keys, and Cloudflare tunnels; multi-account modelled as multiple provider instances.

### Changed

- Changed: `mngr imbue_cloud admin pool create` post-create read-back is now scoped to `--provider` (default `vultr`) and uses `--on-error continue`; broken `just create-pool-hosts*` recipes and `apps/remote_service_connector/scripts/create_pool_hosts.py` deleted in favour of the plugin command.
