# Changelog - minds

A concise, human-friendly summary of changes for the `minds` app. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: "Creating your project" page spinner caption now updates per phase ("Starting...", "Cloning repository...", "Checking out branch...", "Provisioning AI access...", "Creating workspace...", "Waiting for workspace to be ready..."); phase state is carried on `AgentCreationStatus`, and `/api/create-agent/{id}/status` returns new enum values (`INITIALIZING`, `CLONING_REPO`, `CHECKING_OUT_BRANCH`, `PROVISIONING_AI`, `CREATING_WORKSPACE`, `WAITING_FOR_READY`, `DONE`, `FAILED`).
- Added: Workspace-server restart and health-recovery UI — per-agent recovery page (`/agents/<id>/recovery`) streams server-status via SSE and auto-reloads when healthy; landing-page status badges; sidebar "Restart workspace server" menu item; per-agent health state machine tracking `system_interface_backend_failure` envelopes from `mngr_forward`.
- Added: `minds env activate` / `minds env deactivate` for explicit shell activation, exporting `MINDS_ROOT_NAME` + derived `MNGR_*` vars + `MINDS_CLIENT_CONFIG_PATH`; `--create` mkdirs the env root for fresh dev envs.
- Added: New top-level `minds pool` CLI group (`create` / `list` / `destroy`) requiring an activated env and auto-injecting `--tag minds_env=<active-env>`; shells out 1:1 to `mngr imbue_cloud admin pool ...`.
- Added: `minds env recover` command + per-env recover-target file capturing pre-deploy Modal app versions and a Neon snapshot branch atomically before touching external state; orchestrator commands refuse while a recover-target file exists.
- Added: Per-tier `[lifecycle]` block in `deploy.toml` (`creates_resources`, `modal_env_strategy`, `writes_local_state`, `tracks_generation`) drives one unified `minds env deploy` path across dev/staging/production.
- Added: Per-dev-env Neon project (`minds-<env>`) with `host_pool` and `litellm_cost` databases — deploy provisions the project and applies the `pool_hosts` schema; destroy deletes the whole project atomically.
- Added: Per-tier generation id minted at deploy time, stored at `secrets/minds/<tier>/generation` and exposed by the connector at `GET /generation`; `minds env activate` auto-wipes the env's `mngr/` / `auth/` / `logs/` on generation mismatch.
- Added: `apps/minds/docs/staging-bringup.md` end-to-end checklist for standing up the `staging` tier from scratch.
- Added: Spec + scaffolding for live integration / acceptance / release testing via a `just minds-test-deployment` orchestrator.
- Added: Vault template `secrets/minds/<tier>/ovh` (AK / AS / CK); manual-provisioning step documented in `apps/minds/docs/vault-setup.md` and `host-pool-setup.md`.

### Changed

- Changed: minds latchkey permission management now uses latchkey 2.9.0's `permission_requests` / `permissions` gateway extensions; permission requests stream via `GET /permission-requests?follow=true` and grants apply through `POST /permissions/rules`.
- Changed: minds split the services agent from the initial chat agent — the primary agent runs only bootstrap/services and is hidden; a real chat agent named after the host is created on first boot and every subsequent agent shares its `CLAUDE_CONFIG_DIR`. Existing workspaces must be re-created.
- Changed: minds "Create a Project" Name field now sets the host name (validated via `HostName` regex); the agent is always `system-services`, and `imbue_cloud` `/hosts/lease` and `/hosts` gain a required `host_name`.
- Changed: Renamed "workspace server" to "system interface" across the desktop client; "Restart workspace server" menu item / recovery page label becomes "Restart system interface".
- Changed: `just minds-start` unsets `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` before launching, so dev-shell credentials no longer leak into agents.
- Changed: Latchkey gateway client starts lazily on a background thread so `minds run` no longer blocks on the supervisor binding its gateway port.
- Changed: `LatchkeyPermissionRequestEvent` now carries `scope` (Detent schema) + `permissions` instead of `service_name`; the desktop client lazily fetches the catalog from the gateway's `/permissions/available` (cached in process), replacing the bundled `services.toml`.
- Changed: Latchkey permission dialog's initial check state is the union of currently-granted permissions and the agent's requested permissions; the catch-all `any` is no longer pre-checked.
- Changed: Multi-environment deploys (`dev` / `staging` / `production`) backed by HCP Vault; each env owns one data root (`~/.minds-<env-name>/`, or `~/.minds/` for production); `MINDS_ROOT_NAME` validation tightened to `minds(-<env-name>)?`.
- Changed: `minds env deploy` and `minds env destroy` no longer take a name argument — they operate on the currently-activated env and refuse loudly when nothing is activated; `minds env destroy` supports staging (gated by `--yes-i-mean-staging`) and hard-refuses production.
- Changed: `minds env deploy` now picks Modal deploy strategy from context (`recreate` for dev / migrations, `rollover` for staging+production without migration) with `--hard` / `--soft` overrides; uses Modal 1.4.x's `--strategy=recreate`.
- Changed: `minds env destroy` walker swapped from Vultr to OVH (matches by `tags["minds_env"] == <env>` and terminates via `OvhVpsClient.destroy_instance`); the dev-tier Vault path is now `<tier>/ovh`.
- Changed: `minds pool create` reads the active tier's OVH AK/AS/CK from Vault and injects them into the inner `mngr imbue_cloud admin pool create`; `--management-public-key-file` is now optional, defaulting to the tier's pool-SSH private key.
- Changed: Modal app + function names shortened (`remote-service-connector` → `rsc`, `litellm-proxy` → `llm`, `fastapi_app` → `api`, `litellm_app` → `proxy`) so deployed hostnames stay under DNS's 63-char limit; `DevEnvName` enforces a 40-char max.
- Changed: Pool-hosts schema migrations now backed by a real `schema_migrations(version, applied_at)` table instead of the old "replay every .sql with IF NOT EXISTS" approach.
- Changed: Every `minds env deploy` mints a fresh `MINDS_DEPLOY_ID` and pushes Modal Secrets under timestamped names; deployed apps pin secrets to the matching deploy id; end-of-deploy GC keeps the last 10 timestamped secrets per `<svc>-<tier>`.
- Changed: Post-deploy health check polls `/health/liveness` instead of `/docs`; per-attempt HTTP timeout bumped 3s → 10s, total budget 30s → 60s for cold-boot tolerance.
- Changed: `minds env deploy` runs `apply_pool_hosts_migrations` for every tier (not just dev) so shared-tier schemas don't diverge from dev's.
- Changed: `minds env destroy` proceeds with cloud-side cleanup even when the local env root has been removed by hand; cleanup is keyed by env name.
- Changed: `minds run` (and `propagate_changes`, and every justfile recipe touching mngr state) refuse without an activated env; no implicit fallback to a hardcoded dev `client.toml`.
- Changed: Electron build dropped `MINDS_BUILD_TIER` in favor of explicit `MINDS_CLIENT_CONFIG_BUNDLE=<path>` + `MINDS_ROOT_NAME_BUNDLE=<minds(-<env-name>)?>`, so beta / staging / production builds no longer collide on disk.
- Changed: Forever-claude-template Dockerfile restructured to defer Playwright + Chromium into a post-boot `deferred-install` service and to copy dependency manifests before source, dropping warm-cache rebuild from ~1m33s to ~25.6s.
- Changed: `[secrets].services` in every tier's `deploy.toml` is now a truthful "vault-backed only" list — `litellm-connector` is pushed as a separate code-driven step; `_DERIVED_ONLY_SECRET_SERVICES` deleted.
- Changed: `DeployLifecycleConfig` has a new pydantic model validator rejecting `writes_local_state=true` + `creates_resources=false` at `deploy.toml` parse time.
- Changed: Per-env recover-target file (`.minds-deploy-recover-target-<env>.json`) and per-env `flock` on `.minds-deploy-lock-<env>.lock` let concurrent deploys against different envs not block each other.
- Changed: Hard-enforced `dev-<user>` naming for dev envs (`DevEnvName` rejects non-`dev-` names); `MINDS_ROOT_NAME_PATTERN` accepts only `minds`, `minds-staging`, or `minds-dev-<rest>`.
- Changed: `minds env activate` exports `MODAL_PROFILE` derived from the active tier's `modal_workspace`, pinning every `modal` CLI shellout to the right workspace regardless of `~/.modal.toml`'s active profile.
- Changed: `min_containers` for `rsc-<tier>` and `llm-<tier>` Modal apps now driven by `deploy.toml`'s new `[min_containers]` block (`connector`, `litellm_proxy`); staging/production ship with `1`.
- Changed: `minds env list` resolves reserved tiers' (`production` / `staging`) `client.toml` to the committed in-repo file (`DevEnvSummary` gains `client_config_source`).
- Changed: All deploys now flow through `minds env deploy`; standalone `scripts/deploy_remote_service_connector.sh`, `deploy_litellm.sh`, and `push_modal_secrets.py` removed; tier deploys require `--yes-i-mean-<tier>` and push Vault secrets straight to Modal.

### Fixed

- Fixed: Startup race where the desktop client could cache a stale latchkey gateway port and fail every subsequent call with `[Errno 111] Connection refused`; `LatchkeyGatewayClient` self-heals from a stale cached URL on connect-level transport failures, and supervisor restart + gateway-client pre-warm run sequentially.
- Fixed: Desktop client now tolerates legacy `service_name` field on response events stored on disk — the loader drops `service_name` before validating, so historical events.jsonl loads cleanly.
- Fixed: Streamed-permission-request handler dedupes redeliveries by `event_id`; previously the in-memory request inbox grew unbounded and the desktop log filled with duplicates.
- Fixed: Stale failure envelopes arriving immediately after a successful restart no longer cause a brief recovery-page flash; the health tracker ignores failures within a short grace window.
- Fixed: "Workspace server starting" loader spinner no longer visibly jumps on each refresh — animation duration matches the 1-second auto-refresh.
- Fixed: Recovery page's "Restart workspace server" button (and the sidebar menu item) now await the restart API response before navigating to the workspace URL, avoiding the race against the in-flight kill.
- Fixed: `minds env deploy` idempotent against Neon — `create_neon_project` looks up by name first and adopts on single match, raises with a copy-paste cleanup recipe on multi-match (the Neon API does not 409 on duplicate names; previous behavior leaked entire Neon projects per re-deploy).
- Fixed: Neon snapshot + recover-target file write now happen BEFORE pool-hosts migrations, so `minds env recover` can roll back a bad migration; `verify_neon_token_has_restore_scope` is now called as a preflight before snapshot; `write_recover_target_atomic` best-effort deletes the just-created snapshot branch on write failure.

### Removed

- Removed: Bundled `apps/minds/imbue/minds/desktop_client/latchkey/services.toml` (desktop client now fetches the catalog from the gateway).
- Removed: `apps/minds/imbue/minds/cli/pool.py` duplicate (pre-`mngr_imbue_cloud`) and `apps/minds/imbue/minds/envs/providers/vultr_tags.py`.
- Removed: `MINDS_BUILD_TIER` from the packaged Electron build; `just devminds-start` and `forward-{minds,devminds}-system-interface` recipes (replaced by env-agnostic `just minds-start` and `forward-system-interface`).
- Removed: Inline best-effort rollback machinery (`_best_effort_rollback`, `_ROLLBACK_TABLE`, `_rollback_*`) — replaced by `minds env recover`.

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
