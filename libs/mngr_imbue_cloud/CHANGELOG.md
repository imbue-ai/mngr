# Changelog - mngr_imbue_cloud

A concise, human-friendly summary of changes for the `mngr_imbue_cloud` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: New `mngr imbue_cloud bucket` command group (`create` / `list` / `info` / `destroy`) for managing per-host R2 buckets (paid accounts only), plus `bucket keys create/list/destroy` for minting and revoking bucket-scoped S3 keys (read-only or read-write). `bucket create` returns S3-compatible credentials as JSON; the secret is shown only once and never stored. `bucket destroy` refuses a non-empty bucket and otherwise cascades to revoke its keys.
- Added: Pure free `build_pool_host_wipe_script` in `mngr_imbue_cloud.instance` exposing the rendered wipe shell so it can be unit-tested without standing up an SSH transport.

### Changed

- Changed: `mngr destroy <agent>` against an imbue_cloud-leased pool host is now terminal rather than a soft `docker stop`. The new flow stops + removes the workspace container, drops the per-host docker volume and btrfs subvolume, prunes the system, wipes `/root` + `/tmp` (preserving `/root/.ssh/authorized_keys`), releases the lease back to the pool, then cleans up local per-host state. Privacy-first ordering wipes data before flipping the row to `released`. `mngr delete <agent>` runs the same flow and is a safe no-op for an already-released lease. Use `mngr stop <agent>` instead to pause the container without releasing the lease.
- Changed: `mngr imbue_cloud admin pool create` is now provider-generic — drops `MINDS_ROOT_NAME` env detection, adds a required `--region REGION` and repeatable `--tag KEY=VALUE`, defaults to `--template main --template ovh` with `@host.ovh` + `--provider ovh`, and installs + configures `ufw` on every leased VPS before the row hits `pool_hosts`.
- Changed: `ImbueCloudProvider.create_host` now SFTPs into the leased container after the host-key scan and rewrites `/mngr/data.json`'s `host_name` field to the user-supplied `HostName`, so the FCT bootstrap's `_maybe_create_initial_chat` uses the user's chosen name instead of the bake's placeholder.
- Changed: The bake's services agent now uses the constant name `system-services` (was per-bake `pool-<hex>` UUID); the bake also destroys the FCT-bootstrap-created chat agent and `rm -f`'s `/code/runtime/initial_chat_created` so the user's first start re-fires the bootstrap cleanly.
- Changed: `_get_agent_info` now takes `host_name` as a keyword arg and filters by both `name` and `host.name`, so the operator's local mngr state accumulating one `system-services` agent per bake no longer routes subsequent calls to the wrong VPS.
- Changed: Provider's `get_host_and_agent_details` override (and its lease-only `_build_offline_details_from_lease` fallback) now accepts and forwards the new `offline_field_generators` parameter, so offline plugin fields are populated for leased hosts that fall back to offline/lease-only data.

### Removed

- Removed: Dead `build_combined_inject_command`, `normalize_inject_args`, `_sed_replace_env_line`, and `_ensure_no_quote_chars` helpers on `ImbueCloudHost` (and the now-empty `host_test.py`); the central `MINDS_API_KEY` is injected on the fly by the latchkey gateway's `minds-api-proxy` extension and no longer needs to be pushed onto leased pool hosts.

### Fixed

- Fixed: `pool_hosts` INSERT now picks up the schema's `host_name` column; every successful pool bake had been dying at the last step with `null value in column "host_name"` and leaking a fully-provisioned VPS.
- Fixed: Multi-token `mngr exec` commands packed into a single `shlex.join`'d positional string so click no longer eats `--force` as a `mngr exec` option.
- Fixed: `mngr imbue_cloud auth oauth` no longer hangs until the 300s timeout after the browser already returned the OAuth code. The local callback listener now only records query params when the request is for `/oauth/callback` with non-empty params, so secondary GETs (favicon, prefetches) can no longer overwrite the captured callback with `{}`.

## [v0.2.8] - 2026-05-13

### Changed

- Changed: `mngr list` for `imbue_cloud` now drives discovery through outer (VPS-root) SSH instead of inner-container SSH, showing true container state (`RUNNING`/`STOPPED`/`CRASHED`/`PAUSED`/`DESTROYED`) and full details even when inner sshd is unreachable.

## [v0.2.7] - 2026-05-11

### Added

- Added: New `mngr_imbue_cloud` plugin with `mngr imbue_cloud` CLI (`auth`, `hosts`, `keys litellm`, `tunnels`, `admin pool`) that owns SuperTokens auth, pool-host leasing, LiteLLM keys, and Cloudflare tunnels; multi-account modelled as multiple provider instances.

### Changed

- Changed: `mngr imbue_cloud admin pool create` post-create read-back is now scoped to `--provider` (default `vultr`) and uses `--on-error continue`; broken `just create-pool-hosts*` recipes and `apps/remote_service_connector/scripts/create_pool_hosts.py` deleted in favour of the plugin command.
