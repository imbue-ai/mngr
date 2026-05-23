# Changelog - minds

A concise, human-friendly summary of changes for the `minds` app. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: New providers panel on the landing page listing each configured provider with backend type, OK/Error/Disabled status, last error verbatim, Enable/Disable buttons, and two freshness counters for last discovery / last full discovery event.
- Added: Workspace-server restart and health-recovery UI on the `mngr_forward` plugin — landing-page status badges, a `/agents/<id>/recovery` page that streams server-status via SSE, sidebar "Restart workspace server" context menu, and per-agent HEALTHY/STUCK/RESTARTING tracking driven by `workspace_backend_failure` envelopes.
- Added: Latchkey gateway bundled `minds-api-proxy` extension that reverse-proxies `/minds-api-proxy` to the desktop client's bare-origin Minds API; upstream URL published to the supervisor via the new `LatchkeyForwardSupervisor.extra_env` on every `minds run` startup.
- Added: WebDAV file-server mount at `/api/v1/files` (`wsgidav` + `a2wsgi`) exposing the user's home directory and `/tmp` with per-agent Bearer-token auth; HTML directory browser disabled.
- Added: New file-sharing permission flow — `POST /permission-requests` with `{type: "file-sharing", payload: {path, access: READ|WRITE}}`, `LatchkeyFileSharingPermissionRequestEvent` + `FileSharingGrantHandler` rendering single yes/no dialogs per absolute path, per-file Detent permission schema on the `latchkey-self` scope.
- Added: `minds env recover` command + per-env recover-target file at the monorepo root for failed-deploy rollback (Modal `app rollback`, Neon branch-restore from a pre-deploy snapshot, orphan-secret cleanup).
- Added: New `ci` env tier (alongside `dev`/`staging`/`production`) with mirrored Vault secrets and ephemeral `ci-<...>` env names.
- Added: `minds env activate --deploy` mode that exports `MODAL_PROFILE` (default activate is now use-only).
- Added: `minds pool` CLI group (`create`/`list`/`destroy`) shelling out to `mngr imbue_cloud admin pool`, auto-injecting `--tag minds_env=<active-env>`.
- Added: Multi-environment deploys backed by HCP Vault (`dev`/`staging`/`production`) with per-env data roots (`~/.minds-<env-name>/`) holding the env's own mngr profile, agents, auth, logs, and (for dev) split `client.toml`/`secrets.toml`.
- Added: Per-tier generation id minted at deploy, served by the connector at `GET /generation`; `minds env activate` auto-wipes stale per-env state on generation mismatch.
- Added: `test_create_local_docker_workspace_via_electron` acceptance test driving the real Electron app via Playwright over CDP.
- Added: Spec `specs/minds-deployment-tests.md` and a `just minds-test-deployment` orchestrator for live integration/acceptance/release testing.
- Added: `MINDS_API_KEY` written to the workspace host's env file via `--host-env` so every agent on the host inherits it for `/api/v1/...` auth.

### Changed

- Changed: minds latchkey permission management now uses latchkey 2.9.0's `permission_requests` / `permissions` gateway extensions; permission requests stream via `GET /permission-requests?follow=true` and grants apply through `POST /permissions/rules`.
- Changed: minds split the services agent from the initial chat agent — the primary agent runs only bootstrap/services and is hidden; a real chat agent named after the host is created on first boot and every subsequent agent shares its `CLAUDE_CONFIG_DIR`. Existing workspaces must be re-created.
- Changed: minds "Create a Project" Name field now sets the host name (validated via `HostName` regex); the agent is always `system-services`, and `imbue_cloud` `/hosts/lease` and `/hosts` gain a required `host_name`.
- Changed: Bumped bundled Latchkey to 2.11.3.
- Changed: `minds run` no longer dictates the `mngr forward` plugin's port — `--mngr-forward-port` and `MINDS_MNGR_FORWARD_PORT` are removed; the plugin reports its bound port via a `listening` envelope.
- Changed: Permission request wire shape is typed (`{agent_id, rationale, type, payload}` with `predefined` or `file-sharing`); pending requests live under `permission_requests/v2/`; `POST /permission-requests/approve/<id>` merges the precomputed effect into the target permissions file.
- Changed: "Creating your project" page updates its spinner caption as setup progresses (`INITIALIZING`/`CLONING_REPO`/`CHECKING_OUT_BRANCH`/`PROVISIONING_AI`/`CREATING_WORKSPACE`/`WAITING_FOR_READY`/`DONE`/`FAILED`); the `/api/create-agent/{id}/status` JSON API returns the new enum values.
- Changed: Renamed the desktop client "workspace server" feature to "system interface" in menu labels, recovery page text, and wire format.
- Changed: `just minds-start` unsets `ANTHROPIC_API_KEY` and `ANTHROPIC_BASE_URL` before launching so shell-exported credentials no longer leak into dev-app agents.
- Changed: Streamed-permission-request handler dedupes redeliveries by `event_id`; the permission dialog initial state is now the union of existing grants and newly-requested permissions (no implicit `any` pre-check).
- Changed: Modal app + function names shortened (`remote-service-connector`→`rsc`, `fastapi_app`→`api`, `litellm-proxy`→`llm`, `litellm_app`→`proxy`); Modal workspaces renamed to `minds-<tier>`; `DevEnvName` enforces a 40-char max.
- Changed: One `minds env deploy` code path for every tier driven by a new required `[lifecycle]` block in each tier's `deploy.toml`; `deploy_dev_env` + `deploy_tier_env` collapsed into `deploy_env`.
- Changed: Each `minds env deploy` mints a fresh `MINDS_DEPLOY_ID` and pushes per-deploy Modal Secrets under `<svc>-<tier>-<deploy_id>`; end-of-deploy GC keeps the last 10 per `<svc>-<tier>`.
- Changed: Pool-hosts schema migrations now backed by a real `schema_migrations(version, applied_at)` table; legacy files keep `IF NOT EXISTS` guards, new migrations land without them.
- Changed: `minds env destroy` walker swapped from Vultr to OVH (matches `tags["minds_env"] == <env>` and terminates via `OvhVpsClient.destroy_instance`).
- Changed: Deploy strategy auto-selects rollover vs recreate (`--hard`/`--soft` overrides); default is recreate for dev/CI, rollover for staging/production with no migration.
- Changed: Faster `forever-claude-template` Dockerfile — Playwright + Chromium install deferred to first container boot via a `deferred-install` service; manifest layers separated so code-only edits reuse the cached `uv sync`/`npm ci`. Measured warm-rebuild: 1m33s → ~25.6s.
- Changed: `forever-claude-template` `.dockerignore` is now a symlink to `.gitignore`; ratchets enforce the convention.
- Changed: Per-dev-env Neon project (with `host_pool` + `litellm_cost` DBs) provisioned/destroyed atomically; Vault `secrets/minds/<tier>/neon-admin` now expects `NEON_ORG_ID`.
- Changed: `minds env activate` is now use-only by default (exports the four use-side env vars and emits `unset MODAL_PROFILE`); pass `--deploy` to also export `MODAL_PROFILE` and pre-validate the matching profile in `~/.modal.toml`.
- Changed: `minds env deploy/destroy/recover` refuse to run unless the shell is deploy-activated.
- Changed: Latchkey gateway client starts lazily on a background thread so `minds run` no longer blocks on the supervisor binding its gateway port.
- Changed: Post-deploy health check polls the connector's new `GET /health/liveness` instead of `/docs` (10s per-attempt, 60s total).
- Changed: `DeployLifecycleConfig` rejects `writes_local_state=true` + `creates_resources=false` at `deploy.toml` parse time.
- Changed: `minds env destroy` proceeds with cloud-side cleanup even when the local env root has been removed by hand.
- Changed: `minds pool create` auto-injects the activated tier's OVH AK/AS/CK from Vault and derives the management public key from `pool-ssh.POOL_SSH_PRIVATE_KEY`; `--management-public-key-file` is now optional.
- Changed: `LatchkeyPermissionRequestEvent` renamed to `LatchkeyPredefinedPermissionRequestEvent` to distinguish from the new file-sharing event type.

### Removed

- Removed: Silent auto-disable on `ImbueCloudAuthError` in discovery — replaced by the providers panel (the user explicitly disables errored providers).
- Removed: `LATCHKEY_PERMISSION` events from the `events.jsonl` channel; the gateway extension owns that flow.
- Removed: `apps/minds/imbue/minds/desktop_client/latchkey/services.toml`; the catalog is now fetched from the gateway's `/permissions/available` and cached in-process.
- Removed: `_LatchkeyDiscoveryAdapter` and supporting `SSHTunnelManager`-related types from `cli/run.py` after the `mngr-latchkey` plugin extraction.

### Fixed

- Fixed: Restart-workspace-server endpoint returns 200 as soon as the kill-dispatch completes (no longer blocks up to 15s polling); restart buttons now await the response before navigating so the user reliably lands on the plugin's loader page.
- Fixed: Stale failure envelopes arriving immediately after a successful restart no longer flash the recovery page; the "Workspace server starting" loader spinner's animation duration matches the page's 1-second auto-refresh.
- Fixed: Race where the desktop client could cache a stale latchkey gateway port and then fail with `[Errno 111] Connection refused` — `LatchkeyGatewayClient` self-heals on connect-level transport failures, and the supervisor restart + gateway pre-warm run sequentially on a single background thread.
- Fixed: `minds env deploy` Neon idempotency — `create_neon_project` / `delete_neon_project` look up by name first and refuse loud on duplicate-name collisions instead of leaking a second Neon project per re-deploy.
- Fixed: OVH-backed imbue_cloud pool flow end-to-end — bake's services agent uses the constant `system-services` name; FCT-bootstrap-created chat agent + sentinel are cleaned up before lease; `ImbueCloudProvider.create_host` rewrites the leased container's `/mngr/data.json` `host_name` to the user-supplied value.
- Fixed: Deploy-safety ordering — Neon snapshot + recover-target file write now happen BEFORE pool-hosts migrations (F1); `verify_neon_token_has_restore_scope` is now called as a preflight (F2); `write_recover_target_atomic` best-effort-cleans up the snapshot branch on file-write failure (F4).
- Fixed: `minds env deploy` runs `apply_pool_hosts_migrations` for every tier (not only dev), so new `.sql` migrations apply to staging/production as well.
- Fixed: `find_monorepo_root` check happens before Vault read and `make_deploy_id` so running from outside the monorepo fails immediately with a clean error.
- Fixed: `litellm-connector` Modal Secret no longer claims to be vault-backed; pushed as a code-driven step at the end of the secret-push loop.

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
