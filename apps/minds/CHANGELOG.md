# Changelog - minds

A concise, human-friendly summary of changes for the `minds` app. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: Multi-environment deploy support (`dev`, `staging`, `production`, plus an opt-in `ci-<...>` ephemeral env tier) backed by HCP Vault — `minds env activate` / `deactivate` / `deploy` / `destroy` / `recover`, per-env data root at `~/.minds-<env>/`, per-tier generation id, and a single unified `deploy_env` path driven by a required `[lifecycle]` block in each tier's `deploy.toml`.
- Added: `minds env recover` command with atomic per-env recover-target file, Neon snapshot branches, `modal app rollback`, timestamped Modal Secrets gated by `MINDS_DEPLOY_ID`, and end-of-deploy GC of stale secrets.
- Added: Per-dev-env Neon project (named `minds-<env>`) under the dev-tier Neon org with `host_pool` and `litellm_cost` databases; `minds env deploy` provisions and `minds env destroy` deletes the project outright.
- Added: Workspace-server restart and health-recovery UI on the `mngr_forward` plugin architecture — per-agent recovery page that streams server status via SSE, sidebar "Restart workspace server" menu item, and landing-page status badges for stuck/restarting workspaces.
- Added: "Creating your project" page now updates its spinner caption progressively as setup progresses; the `AgentCreationStatus` enum is the single source of truth (`INITIALIZING`, `CLONING_REPO`, `CHECKING_OUT_BRANCH`, `PROVISIONING_AI`, `CREATING_WORKSPACE`, `WAITING_FOR_READY`, `DONE`, `FAILED`).
- Added: New top-level `minds pool` CLI group (`create` / `list` / `destroy`) that auto-injects `--tag minds_env=<active-env>` and shells out to `mngr imbue_cloud admin pool ...`.
- Added: `MINDS_MNGR_FORWARD_PORT` env var on `minds run` so concurrent invocations and test harnesses can dodge the default port 8421 collision.
- Added: `test_create_local_docker_workspace_via_electron` acceptance test that drives the real Electron app via Playwright over CDP and resolves the forever-claude-template source in three steps (local worktree → branch on FCT remote → `main`).
- Added: `apps/minds/docs/staging-bringup.md`, an end-to-end checklist for standing up the `staging` tier from scratch.
- Added: Spec `specs/minds-deployment-tests.md` plus the `just minds-test-deployment` orchestrator scaffolding for live integration/acceptance/release testing.

### Changed

- Changed: minds latchkey permission management now uses latchkey 2.9.0's `permission_requests` / `permissions` gateway extensions; permission requests stream via `GET /permission-requests?follow=true` and grants apply through `POST /permissions/rules`.
- Changed: minds split the services agent from the initial chat agent — the primary agent runs only bootstrap/services and is hidden; a real chat agent named after the host is created on first boot and every subsequent agent shares its `CLAUDE_CONFIG_DIR`. Existing workspaces must be re-created.
- Changed: minds "Create a Project" Name field now sets the host name (validated via `HostName` regex); the agent is always `system-services`, and `imbue_cloud` `/hosts/lease` and `/hosts` gain a required `host_name`.
- Changed: Bumped bundled Latchkey to 2.11.3.
- Changed: `minds env activate` is split into a default use-only mode (no `MODAL_PROFILE` exported) and an opt-in `--deploy` mode; `minds env deploy` / `destroy` / `recover` refuse to run unless deploy-activated.
- Changed: Renamed "workspace server" to "system interface" across the desktop client UI, menus, and wire format.
- Changed: Desktop client adapted to the new latchkey permission-request shape (`scope` + `permissions` instead of `service_name`); the bundled `services.toml` is gone, the catalog is lazy-fetched from the gateway's `/permissions/available`, and legacy `service_name` fields on disk are tolerated.
- Changed: Streamed-permission-request handler dedupes redeliveries by `event_id` so the in-memory inbox no longer grows unbounded across reconnects.
- Changed: Latchkey permission dialog no longer pre-checks the catch-all `any` permission as an implicit default; initial check state is the union of existing grants and requested permissions.
- Changed: `minds env destroy` walker swapped from Vultr to OVH (matches by `tags["minds_env"] == <env>`); existing Vultr-backed `pool_hosts` rows are not migrated automatically.
- Changed: Shorter Modal app/function names (`remote-service-connector` → `rsc`, `litellm-proxy` → `llm`, etc.) so deployed hostnames stay under DNS's 63-char limit; `DevEnvName` enforces a 40-char max; hard-enforces `dev-<your-user>` naming for dev envs.
- Changed: Pool-hosts schema migrations are now backed by a real `schema_migrations(version, applied_at)` table instead of replaying `.sql` files with `IF NOT EXISTS` guards; `apply_pool_hosts_migrations` runs for every tier (was dev-only).
- Changed: `minds env deploy` picks Modal deploy strategy (rollover vs recreate) from context, with `--hard` / `--soft` overrides; post-deploy health check polls the connector's new `/health/liveness` route (per-attempt timeout bumped from 3s to 10s, total budget from 30s to 60s).
- Changed: Local minds workspace creation sped up by restructuring the `forever-claude-template` Dockerfile and deferring Playwright into a post-boot install (warm rebuild ~1m33s → ~25.6s).
- Changed: `just minds-start` now unsets `ANTHROPIC_API_KEY` and `ANTHROPIC_BASE_URL` before launching the desktop client.
- Changed: minds starts the latchkey gateway client lazily on a background thread so `minds run` no longer blocks on the supervisor binding its gateway port.
- Changed: `minds env list` resolves reserved tiers' (`production` / `staging`) `client.toml` to the committed in-repo file instead of showing `(no client.toml)`; `DevEnvSummary` gains a `client_config_source` field.

### Removed

- Removed: Stale skipped `test_create_agent_e2e` (replaced by the new Electron acceptance test).

### Fixed

- Fixed: Startup race that could cache a stale latchkey gateway port and then fail every subsequent call — `LatchkeyGatewayClient` now self-heals on connect-level transport failures, and the supervisor restart + gateway-client pre-warm run sequentially on a single background thread.
- Fixed: `minds env deploy` is now actually idempotent against Neon — `create_neon_project` looks up by name first and refuses on multiple matches rather than silently creating duplicate projects on every retry.
- Fixed: `minds env destroy` proceeds with cloud-side cleanup even when the local env root has already been removed by hand (resources are keyed off env name, not the local directory).
- Fixed: Stale failure envelopes arriving immediately after a successful workspace-server restart no longer cause a brief recovery-page flash; the health tracker ignores failures within a short grace window after recovery.
- Fixed: "Workspace server starting" loader spinner no longer visibly jumps on each refresh — animation duration now matches the page's 1-second auto-refresh interval.

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
