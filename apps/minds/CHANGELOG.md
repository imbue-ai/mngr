# Changelog - minds

A concise, human-friendly summary of changes for the `minds` app. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: Providers panel on the landing page lists every configured provider (except `local`) with status badge, last error, and Enable/Disable button; two freshness counters show time since last discovery / full discovery event.
- Added: New WebDAV file-server mount at `/api/v1/files` (backed by `wsgidav` + `a2wsgi`) exposing the user's home and `/tmp` to authenticated agents; reachable from agents through the bundled `minds-api-proxy` Latchkey extension.
- Added: File-sharing permission-request flow with READ/WRITE access modes; the desktop client renders a yes/no dialog per absolute path with an inline read-only or read & write badge.
- Added: `LatchkeyFileSharingPermissionRequestEvent` and `FileSharingGrantHandler` for the file-sharing path; `LatchkeyGatewayClient.approve_permission_request` for the new `/permission-requests/approve/<id>` endpoint.
- Added: `test_create_local_docker_workspace_via_electron` acceptance test driving the real Electron minds app via Playwright over CDP.
- Added: New `ci` env tier (alongside `dev`/`staging`/`production`); deployment-tests orchestrator now mints ephemeral `ci-<timestamp>-<uuid>` envs.
- Added: `minds env activate --deploy <name>` opt-in mode that exports `MODAL_PROFILE` and pre-validates `~/.modal.toml`; default activation is now use-only (no `MODAL_PROFILE`).
- Added: Multi-environment deploys (`dev`, `staging`, `production`) backed by HCP Vault, with per-env data roots, `minds env activate/deactivate/list/deploy/destroy/recover`, and per-tier generation id.
- Added: `minds pool` CLI group (`create` / `list` / `destroy`) auto-injecting `--tag minds_env=<active-env>` and tier OVH credentials from Vault.
- Added: `minds env recover` command + recover-target file with idempotent rollback (Modal `app rollback`, Neon branch-restore, orphan secret deletion).
- Added: `minds env deploy --hard` / `--soft` overrides for Modal deploy strategy (rollover vs. recreate); default policy recreates on migrations or dev tier.
- Added: `apps/minds/docs/staging-bringup.md` end-to-end checklist for standing up the staging tier from scratch.
- Added: `MINDS_MNGR_FORWARD_PORT` env var on `minds run` for test harnesses that need to dodge port 8421.

### Changed

- Changed: minds latchkey permission management now uses latchkey 2.9.0's `permission_requests` / `permissions` gateway extensions; permission requests stream via `GET /permission-requests?follow=true` and grants apply through `POST /permissions/rules`.
- Changed: minds split the services agent from the initial chat agent — the primary agent runs only bootstrap/services and is hidden; a real chat agent named after the host is created on first boot and every subsequent agent shares its `CLAUDE_CONFIG_DIR`. Existing workspaces must be re-created.
- Changed: minds "Create a Project" Name field now sets the host name (validated via `HostName` regex); the agent is always `system-services`, and `imbue_cloud` `/hosts/lease` and `/hosts` gain a required `host_name`.
- Changed: `minds run` no longer dictates the `mngr forward` plugin's port; the plugin picks its own port and reports it via a `listening` envelope (5s startup timeout). `--mngr-forward-port` flag and `MINDS_MNGR_FORWARD_PORT` env var are removed.
- Changed: Latchkey gateway `permission-requests` extension takes typed `{agent_id, rationale, type, payload}` requests (`type` is `predefined` or `file-sharing`); pending requests persist under `permission_requests/v2/`.
- Changed: Bumped bundled Latchkey to 2.11.3.
- Changed: `MINDS_API_KEY` now lives in the workspace host's env file via `--host-env` so every agent on the host (system-services, chat, worktree, worker) inherits the same key.
- Changed: minds desktop client's latchkey-permission handler code reorganised into `imbue.minds.desktop_client.latchkey.handlers` with `.predefined` and `.file_sharing` siblings; `LatchkeyPermissionRequestEvent` renamed to `LatchkeyPredefinedPermissionRequestEvent`.
- Changed: Landing page agents whose provider just failed render in `UNKNOWN` state; minds no longer silently auto-disables `imbue_cloud_<slug>` providers on `ImbueCloudAuthError`. The user disables them explicitly via the providers panel.
- Changed: Internal `disable_imbue_cloud_provider_for_account` renamed to `set_provider_is_enabled(provider_name, is_enabled)` and generalized across providers.
- Changed: "Creating your project" page now updates spinner captions through the setup phases (Cloning repository, Checking out branch, Provisioning AI access, Creating workspace, Waiting for workspace to be ready); `AgentCreationStatus` enum carries phase as the single source of truth.
- Changed: Renamed "workspace server" to "system interface" across desktop-client UI, menu labels, and the workspace-restart endpoint (`/api/agents/<id>/restart-system-interface`).
- Changed: Latchkey gateway client now starts lazily on a background thread so `minds run` no longer blocks on the gateway port binding.
- Changed: Shorter Modal app + function names so deployed hostnames stay under 63 chars (`remote-service-connector` → `rsc`, `litellm-proxy` → `llm`, etc.); workspaces renamed to `minds-{dev,staging,production}`.
- Changed: One `minds env deploy` path for every tier driven by `[lifecycle]` block in `deploy.toml`; inline rollback machinery deleted in favour of `minds env recover`.
- Changed: Pool-hosts schema migrations now backed by a real `schema_migrations(version, applied_at)` table; `minds env deploy` runs migrations for every tier (not just dev).
- Changed: Every `minds env deploy` mints a fresh `MINDS_DEPLOY_ID` and pushes every Modal Secret under a new timestamped name; end-of-deploy GC keeps the last 10 per `<svc>-<tier>`.
- Changed: `minds env destroy` proceeds with cloud-side cleanup even when the local env root has been removed by hand; refuses to run unless deploy-activated.
- Changed: Speed up local minds workspace creation: Dockerfile no longer pre-installs Playwright (deferred to first-boot `services.toml` install); manifest layers are cached so warm-cache rebuilds collapse from ~1m33s to ~25.6s.
- Changed: Per-dev-env Neon project (not just a database) named `minds-<env>` with `host_pool` + `litellm_cost`; staging/production keep the tier-shared model unchanged.
- Changed: `minds env deploy` now picks the Modal deploy strategy (rollover vs. recreate) from context.
- Changed: `minds env destroy` walker swapped from Vultr to OVH; `mngr_imbue_cloud admin pool` now provider-generic with `--region` / `--tag`.

### Removed

- Removed: `_ImbueCloudAuthErrorDisabler` and the provider-error callback plumbing on `EnvelopeStreamConsumer` — silent auto-disable on auth errors is gone.
- Removed: Standalone deploy scripts (`scripts/deploy_remote_service_connector.sh`, `scripts/deploy_litellm.sh`, `scripts/push_modal_secrets.py`); work folds into the unified `minds env deploy` CLI.
- Removed: `apps/minds/imbue/minds/cli/pool.py` and `apps/minds/imbue/minds/envs/providers/vultr_tags.py` (pool now flows through `mngr_imbue_cloud`).

### Fixed

- Fixed: `just minds-start` now unsets `ANTHROPIC_API_KEY` and `ANTHROPIC_BASE_URL` before launching the desktop client so dev-shell credentials no longer leak into agents.
- Fixed: `/api/agents/<id>/restart-workspace-server` now returns 200 as soon as the `mngr exec` kill dispatch completes; restart UI awaits the response before navigating, so users reliably land on the loader page.
- Fixed: Stale failure envelopes arriving immediately after a successful restart no longer flash the recovery page (short post-recovery grace window).
- Fixed: `LatchkeyGatewayClient` self-heals from a stale cached gateway URL on connect-level transport failures; supervisor restart and gateway-client pre-warm run sequentially so the startup race is eliminated.
- Fixed: Streamed permission-request handler dedupes redeliveries by `event_id` so the in-memory inbox no longer grows unbounded and the log no longer fills with duplicate lines.
- Fixed: Latchkey permission dialog no longer pre-checks the catch-all `any` permission as an implicit default; pre-check is now the union of currently-granted and newly-requested permissions.
- Fixed: minds desktop client tolerates legacy `service_name` fields on persisted `RequestResponseEvent` records; historical responses load cleanly and their requests are correctly filtered out of the pending list.
- Fixed: `minds env deploy` is now idempotent against Neon — duplicate project names raise `NeonProviderError` with a copy-pasteable cleanup recipe instead of silently leaking projects.
- Fixed: Deploy-safety overhaul: Neon snapshot + recover-target write happen BEFORE pool-hosts migrations; `verify_neon_token_has_restore_scope` runs as a preflight; `write_recover_target_atomic` cleans up the snapshot on write failure.
- Fixed: Connector health-check polls `/health/liveness` (was `/docs`) with 10s per-attempt / 60s total budgets so cold-booting Modal containers actually pass.
- Fixed: `DeployLifecycleConfig` rejects `writes_local_state=true` + `creates_resources=false` at `deploy.toml` parse time instead of asserting partway through deploy.
- Fixed: OVH pool flow end-to-end — `pool_hosts` INSERT carries `host_name`, bake uses the constant `system-services` agent name, lease/adopt SFTPs into the container to rewrite `/mngr/data.json`'s `host_name`, and bake destroys the FCT-bootstrap chat agent so the user's first start fires fresh.

## [v0.2.8] - 2026-05-13

### Changed

- Changed: minds switched to per-host latchkey state — `LatchkeyForwardInfo.gateway_port` stamped for non-spawning consumers; minds now stores per-host `latchkey_permissions.json` under `<latchkey-dir>/mngr_latchkey/hosts/<host_id>/`.

## [v0.2.7] - 2026-05-11

### Added

- Added: New `minds run` command that spawns `mngr forward` as a subprocess and consumes its JSONL envelope stream instead of running forwarding in-process.
- Added: minds injects `LATCHKEY_DISABLE_COUNTING=1` into every workspace whenever latchkey is wired so the shared host-side gateway represents one user.
- Added: minds installs a grandparent-death watcher so the Python backend self-terminates ~3 s after Electron crashes, cascading into `mngr observe` / `mngr events` / latchkey children.
- Added: New `MalformedMngrOutputError` (`imbue.minds.errors`).

### Changed

- Changed: Bumped bundled Latchkey to 2.8.0 and switched minds to a single shared `latchkey gateway` subprocess for all agents, with password-protected `LATCHKEY_GATEWAY_LISTEN_PASSWORD`.
- Changed: Reverse-tunnel health check now retries broken tunnels forever (capped at one attempt per 5 minutes via exponential backoff) instead of giving up after 10 consecutive failures.
- Changed: minds "Create a Project" screen — removed the .env checkbox, added an independent "AI provider" choice, renamed "Launch mode" to "Compute provider", added an optional `GH_TOKEN` Advanced field; revoked imbue_cloud sessions auto-disable the matching provider block.
- Changed: minds no longer persists `imbue_cloud` account identity to disk; only the workspace↔account map lives on disk, identity is sourced on demand.

### Removed

- Removed: `LaunchMode.DEV` from minds — the web create form, `/create`, and `/api/create-agent` now only offer `LOCAL` / `LIMA` / `CLOUD` / `IMBUE_CLOUD`; the DEV-only latchkey helper and `MINDS_ALLOW_HOST_LOOPBACK` env var are gone.
- Removed: `apps/minds_workspace_server/` from the monorepo — migrated to `forever-claude-template`'s `apps/system_interface/` and consumed at runtime; the release Dockerfile's node/npm install step is dropped.

### Fixed

- Fixed: `minds run` no longer pegs a CPU as agents/hosts come and go — reverse-tunnel bookkeeping is pruned on agent destroy and the 30 s health-check loop applies per-tunnel exponential backoff.
- Fixed: WebSocket broadcaster queue-full flood — stuck WS clients are evicted after 50 consecutive queue-full broadcasts, and the broadcaster cancels wedged handler tasks blocked on `send_text(...)`.
- Fixed: Closing the last tab in a minds workspace no longer leaves a blank screen — the primary agent's chat tab is automatically reopened when the dockview becomes empty.
- Fixed: `apps/minds/scripts/propagate_changes` now protects `.claude/settings.local.json` from `rsync --delete`, preventing per-agent `UserPromptSubmit` hook loss and the resulting 90 s `send_message` hang.
