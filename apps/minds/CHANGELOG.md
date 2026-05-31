# Changelog - minds

A concise, human-friendly summary of changes for the `minds` app. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `mngr latchkey register-agent --host-id ID --agent-id ID` CLI as the operator-facing equivalent of the per-agent registration the desktop client now does directly.
- Added: Lima 2.1.1 is bundled into the desktop app — `scripts/build.js` downloads and extracts the official release tarball into `resources/lima/`, and the packaged backend prepends `resources/lima/bin` to `PATH` so `limactl` is found without a separate `brew install lima`. macOS Apple Silicon is fully self-contained via Lima's `vz` backend; macOS Intel and Linux still require host QEMU.
- Added: New WebDAV file-server mount at `/api/v1/files` (backed by `wsgidav` + `a2wsgi`) exposing the user's home directory and `/tmp` to agents through the `minds-api-proxy` Latchkey extension, with per-agent Bearer-token auth.
- Added: Latchkey gateway ships a new bundled `minds-api-proxy` extension that reverse-proxies `/minds-api-proxy` to the minds desktop client's bare-origin Minds API, with the upstream URL re-published on every `minds run` startup.
- Added: File-sharing permission requests carry a required `access` field (`READ` / `WRITE`); the minds approval dialog renders a green "read-only" / amber "read & write" badge per file path.
- Added: New providers panel on the landing page lists every configured provider with its status, last error, and an Enable/Disable button; the panel also shows time since the last discovery event.
- Added: New `ci` env tier alongside `dev` / `staging` / `production`, mirroring dev's lifecycle and reading Vault secrets from `secrets/minds/ci/*`.
- Added: "Creating your project" page now updates its spinner caption as setup progresses; `AgentCreationStatus` gains `INITIALIZING` / `CLONING_REPO` / `CHECKING_OUT_BRANCH` / `PROVISIONING_AI` / `CREATING_WORKSPACE` / `WAITING_FOR_READY` / `DONE` / `FAILED`.
- Added: Workspace-server restart and health-recovery UI on the `mngr_forward` plugin — when a workspace server stops responding, the chrome auto-navigates to a recovery page with SSE-streamed status updates and a Restart button; landing page rows show status badges.
- Added: `minds env recover` command + per-env recover-target file; every deploy captures pre-deploy Modal app versions, takes a Neon snapshot, and writes the recover file atomically before touching external state.
- Added: New top-level `minds pool` CLI group (`create` / `list` / `destroy`) that auto-injects `--tag minds_env=<active-env>` and shells out to `mngr imbue_cloud admin pool`.
- Added: New `minds env` shell-activation model with `activate` (use-only) / `activate --deploy` / `deactivate`; `deploy` / `destroy` / `recover` refuse without deploy-activation.
- Added: Multi-environment deploys (`dev` / `staging` / `production`) backed by HCP Vault, each owning a per-env data root (`~/.minds-<env>/`).
- Added: Per-dev-env Neon project named `minds-<env>` containing `host_pool` and `litellm_cost` DBs, provisioned and torn down atomically by `minds env deploy` / `destroy`.
- Added: Per-tier generation id minted at deploy and exposed at `GET /generation`; `minds env activate` wipes stale `mngr/` / `auth/` / `logs/` when the tier was redeployed since the dev last activated.
- Added: `apps/minds/docs/staging-bringup.md` — end-to-end checklist for standing up the `staging` tier from scratch.
- Added: 14-day dependency-cooldown supply-chain gate for the packaged toolchain — `minimumReleaseAge: 20160` in `apps/minds/pnpm-workspace.yaml` (pnpm) and `exclude-newer = "14 days"` under `[tool.uv]` in `apps/minds/electron/pyproject/pyproject.toml` (uv). Resolution refuses any distribution (including transitive) published within the window; frozen-lockfile installs are unaffected.
- Added: Packaged macOS desktop now bundles the real `git` binary plus its `libexec/git-core` helpers (resolved via `xcrun --find git`) instead of the `/usr/bin/git` shim, so `git clone` in a packaged app no longer SIGKILLs on Macs without Xcode Command Line Tools at the expected path; ToDesktop `uploadSizeLimit` bumped 300 → 600 to fit the larger bundle.
- Added: "Backup provider" control on the workspace create form mirroring the existing "AI provider" toggle, with three options — `imbue_cloud` (creates a per-workspace R2 bucket named after the new host id and a scoped key, then injects a `runtime/secrets/restic.env` pointing the FCT `host_backup` service at that bucket; gated on a selected account and the default when an account is present), `manual` (a free-form `KEY=VALUE` block written verbatim to `restic.env`), and `configure_later` (injects nothing now; default when no account is selected). When a real backup provider is chosen, a "Backup encryption method" row appears (`master_password` or `no_password`). minds now requires `restic` installed on the host running it; it initializes each workspace's restic repository itself and gives the workspace its own random repository password so the master password never enters the workspace.
- Added: `apps/minds/scripts/mac-runner-reset.sh` that cleans `~/.minds`, removes the installed `.app`, kills leftover Minds processes, and stops/deletes any Lima VM instances; optionally re-downloads and installs a fresh `.app` from a ToDesktop `.zip` URL passed as the first argument. Intended to run at the start of every verification job on the dedicated self-hosted mac-runner, preserving only the Lima base-image cache (~1.5 GB).

### Changed

- Changed: Renamed `LaunchMode.LOCAL` compute provider to `LaunchMode.DOCKER` everywhere (Python code, `/create` form HTML, `/api/create-agent` JSON payloads, docs); the old name collided with mngr's own `local` provider. Submitting `launch_mode=LOCAL` is no longer recognized.
- Changed: `MINDS_API_KEY` is no longer minted per-agent — `minds run` generates a single in-memory key on startup, the latchkey gateway's `minds-api-proxy` extension injects it on forwarded requests, and workspaces no longer carry the env var. Rotating per-startup removes a long-lived secret from the filesystem.
- Changed: Notifications endpoint moved from `POST /api/v1/notifications` to `POST /api/v1/agents/<agent_id>/notifications`; every `/api/v1` route is now per-agent. The bearer-auth gate now compares against the single in-memory key with a constant-time check.
- Changed: Every agent created by minds gets added to the host's `minds-api-proxy-allowed-agent` enum at finalize-host-permissions time, so an agent on host A cannot reach the Minds API on behalf of an agent on host B.
- Changed: Desktop client now registers each agent via `imbue.mngr_latchkey.agent_setup.register_agent_for_host(...)` (a single atomic file edit) instead of the previous gateway-extension dance that POSTed two schemas + one rule per agent.
- Changed: `workspace_ready_timeout_seconds` bumped from 60s to 300s in `agent_creator.py` so first-boot provisioning (uv sync, npm ci + run build for the system_interface frontend) no longer bounces users to the recovery page while the agent is still finishing provisioning.
- Changed: `minds run` no longer dictates the `mngr forward` plugin's port — the `--mngr-forward-port` flag and `MINDS_MNGR_FORWARD_PORT` env var are removed; the plugin picks its own port and reports it back via its `listening` envelope.
- Changed: Bumped bundled Latchkey version to 2.11.3.
- Changed: Latchkey gateway's `permission-requests` extension grows a typed request schema (`{agent_id, rationale, type, payload}`) and a new `POST /permission-requests/approve/<id>` endpoint; pending requests live under `permission_requests/v2/`.
- Changed: minds desktop client's latchkey-permission handlers now live in `imbue.minds.desktop_client.latchkey.handlers` as `.predefined` and `.file_sharing` siblings sharing a Jinja template + Tailwind base.
- Changed: `MINDS_API_KEY` is now written to the workspace host's env file via `--host-env` (not the per-agent `--env`) so every agent on the host can authenticate against `/api/v1/...`.
- Changed: `LatchkeyPermissionRequestEvent` renamed to `LatchkeyPredefinedPermissionRequestEvent` to mirror the wire `type=predefined`.
- Changed: minds latchkey permission management now uses latchkey 2.9.0's `permission_requests` / `permissions` gateway extensions; permission requests stream via `GET /permission-requests?follow=true` and grants apply through `POST /permissions/rules`.
- Changed: minds split the services agent from the initial chat agent — the primary agent runs only bootstrap/services and is hidden; a real chat agent named after the host is created on first boot and every subsequent agent shares its `CLAUDE_CONFIG_DIR`. Existing workspaces must be re-created.
- Changed: minds "Create a Project" Name field now sets the host name (validated via `HostName` regex); the agent is always `system-services`, and `imbue_cloud` `/hosts/lease` and `/hosts` gain a required `host_name`.
- Changed: Renamed the "workspace server" feature to "system interface" across the desktop client menu items and recovery page labels.
- Changed: Started the latchkey gateway client lazily on a background thread so `minds run` no longer blocks on the `mngr latchkey forward` supervisor binding its gateway port.
- Changed: `just minds-start` unsets `ANTHROPIC_API_KEY` and `ANTHROPIC_BASE_URL` before launching the desktop client.
- Changed: `minds env deploy` now picks rollover vs recreate from context (recreate on migration or dev tier) and supports `--hard` / `--soft` overrides.
- Changed: Swapped `minds env destroy`'s instance walker from Vultr to OVH; the dev-tier Vault path is now `<tier>/ovh` with AK/AS/CK.
- Changed: Shorter Modal app + function names so deployed hostnames fit under DNS's 63-char limit (`remote-service-connector` → `rsc`, `fastapi_app` → `api`, `litellm-proxy` → `llm`, `litellm_app` → `proxy`).
- Changed: One `minds env deploy` code path for every tier, driven by a `[lifecycle]` block in each tier's `deploy.toml`.
- Changed: Pool-hosts schema migrations now backed by a `schema_migrations(version, applied_at)` table instead of replaying every `.sql` with `IF NOT EXISTS`.
- Changed: Every `minds env deploy` mints a fresh `MINDS_DEPLOY_ID` and pushes every Modal Secret under a timestamped name (`<svc>-<tier>-<deploy_id>`).
- Changed: `minds env deploy`'s post-deploy health check now polls the connector's `/health/liveness` (10 s per-attempt timeout, 60 s total budget) instead of `/docs`.
- Changed: Speed up local minds workspace creation by restructuring the `forever-claude-template` Dockerfile and deferring Playwright into a post-boot install (warm rebuild 1m33s → 30s → ~25.6s).
- Changed: Latchkey permission dialog no longer pre-checks the catch-all `any` permission as an implicit default; initial check state is now the union of existing grants and the agent's requested permissions.
- Changed: Streamed-permission-request handler now dedupes redeliveries by `event_id` so the requests inbox no longer grows unbounded on every gateway reconnect.
- Changed: Pinned the desktop client's JS toolchain to exact versions (pnpm `10.33.4` + Node.js `24.15.0`) with `engine-strict=true` so mismatched installs fail fast; added an `.nvmrc` so nvm/fnm users pick up the pinned Node automatically. Pinned the packaged end-user Python to `==3.12.13` in `apps/minds/electron/pyproject/pyproject.toml`.
- Changed: Bumped bundled Electron from `35.7.5` to `40.10.1` so the runtime shipped to end users bundles Node.js `24.15.0`, matching the development pin (the prior bundled runtime was Node 22.x).
- Changed: Bumped the bundled `UV_VERSION` in `apps/minds/scripts/build.js` from `0.7.12` to `0.11.15` so the shipped uv can parse the relative `exclude-newer` cooldown (0.7.12 failed to parse it, silently discarded the lockfile, and re-resolved unpinned at first launch).
- Changed: Bumped bundled Latchkey to `2.12.2` — first-time Google Cloud users now see the ToS dialog, and Google Projects are reused when possible to work around the low default project-count limit.
- Changed: Simplified the latchkey predefined-permission approval dialog for non-technical users — read-only informative permission list (no checkboxes) by default, with only Approve and Deny actions; a small "Adjust" link reveals the full per-permission checkbox editor. The dialog was also visually streamlined: the standalone "Workspace:" line was removed, the agent's reason is now attributed as `"<workspace> says:"`, the request summary is a single sentence, and the service name in the header no longer renders inside a grey box. The file-sharing permission dialog was updated to match this chrome (same header treatment, `"<workspace> says:"` attribution, single summary sentence naming the workspace, access level, and host-wide scope). The page reserves the scrollbar gutter so expanding the editor no longer shifts the layout sideways.
- Changed: Excluded the Latchkey dependency from the minimum-age check (Latchkey is co-developed with Minds).
- Changed: ToDesktop build now activates the `beforeInstall` hook so the build server re-downloads/re-resolves `uv` and `git` for its target platform rather than using bytes uploaded from the developer's machine. `package.json`'s `todesktop:beforeInstall` is wired to `./scripts/download-binaries.js`, and the `downloadUv()` orchestrator is restored in that file.
- Changed: pnpm and node are now pinned via ToDesktop's first-class `pnpmVersion` / `nodeVersion` config fields, sourced from `package.json`'s `engines` block (already pinned to `pnpm 10.33.4` and `node 24.15.0`). `todesktop.json` is replaced with a `todesktop.js` that `require('./package.json')` and reads `engines.pnpm` / `engines.node` into the ToDesktop config; `package.json` is now the single source of truth for pnpm + node versions across dev laptops, imbue CI, and ToDesktop. `apps/minds/scripts/build_test.py` now shells out to `node -e "..."` to read the ToDesktop config (module-level-skipped when no `node` is on PATH).
- Changed: Consolidated `downloadUv` into a single definition in `scripts/download-binaries.js` and imported it into `scripts/build.js`, mirroring how `downloadGit` and `download` are already shared. Removes the duplicated `UV_VERSION` constant, `getUvDownloadUrl`, and `downloadUv` from `build.js`; both call sites (local `pnpm build` and ToDesktop's `beforeInstall` hook) now run the same implementation.

### Removed

- Removed: Per-agent reverse SSH tunnel that exposed `/api/v1/...` to workspaces, along with `MindsApiUrlWriter`, `LocalAgentDiscoveryHandler`, and the `$MNGR_AGENT_STATE_DIR/minds_api_url` write; agents now reach the Minds API exclusively through the latchkey gateway's `minds-api-proxy` extension.
- Removed: Per-agent `MINDS_API_KEY` generation in `agent_creator.py` (no more `--host-env MINDS_API_KEY=...` to `mngr create`, no more per-agent `api_key_hash` file).
- Removed: `gateway_client` field on `AgentCreator` and the low-level schema-altering methods on `LatchkeyGatewayClient` (`set_permission_schema`, `delete_permission_schema`, `delete_permission_rule`); the user-grant API (`set_permission_rule`, etc.) stays.
- Removed: Silent auto-disable on `imbue_cloud` auth errors — `_ImbueCloudAuthErrorDisabler` and the provider-error callback plumbing on `EnvelopeStreamConsumer` are gone; the user now drives the Disable action explicitly via the providers panel.

### Fixed

- Fixed: Hardened the workspace-restart shell command in `desktop_client/app.py` to use `tmux kill-window -t "=${MNGR_PREFIX}system-services:svc-system_interface"` (with the `=` exact-match prefix) so the kill no longer silently lands on a sibling-prefix session's window.
- Fixed: Startup race where the minds desktop client could cache a stale latchkey gateway port and then fail every call with `[Errno 111] Connection refused`; the gateway client now self-heals on `httpx.ConnectError`/`ConnectTimeout`, and supervisor restart + pre-warm now run sequentially on a single background thread.
- Fixed: `minds env deploy` is now actually idempotent against Neon — `create_neon_project` / `delete_neon_project` look up by name first via `_find_projects_by_name` and raise on ambiguous matches instead of silently leaking duplicate projects.
- Fixed: Desktop client tolerates legacy `service_name` fields on disk by dropping them before validating `RequestResponseEvent`, eliminating the per-startup pydantic-extras warning and unresolved-request bug.
- Fixed: `minds env destroy` proceeds with cloud-side cleanup even when the local env root has already been removed by hand.
- Fixed: `minds env deploy` runs `apply_pool_hosts_migrations` for every tier (not just dev), so shared-tier schema no longer diverges.
- Fixed: `find_monorepo_root` check runs before Vault credential read and `make_deploy_id` so running from outside the monorepo fails cleanly.
- Fixed: `FileAuthStore.get_signing_key` race on a fresh data directory that intermittently logged users out — the prior lazy non-atomic generation could let one thread read a momentarily-empty key file (raising `SigningKeyError`, returning 500 from `/authenticate`) or two threads each generate a different key and race to write it (silently invalidating the just-signed cookie). Generation is now serialized behind a per-store lock with a double-checked re-read and `atomic_write`. This was the dominant cause of `test-docker-electron` CI flake.
- Fixed: Deny button on the latchkey permission-request dialog now works even when the requested scope is not in the gateway's services catalog (e.g. a typo or stale catalog); the deny flow falls back to the raw scope string so the pending request is always torn down and the agent is always notified.
- Fixed: Resetting or destroying an env no longer leaves its mngr Docker state container (`<MNGR_PREFIX>docker-state-<user_id>`) running forever — `minds env destroy` and the activate-time generation-mismatch auto-wipe now remove that env's exact state container and its backing volume. The auto-wipe also destroys the env's mngr agents (via a single batched `mngr destroy`) before wiping local state so their Docker host containers and build images are cleaned up too; env-teardown agent destruction now uses one batched `mngr destroy` call instead of one per agent.
- Fixed: The "destroy workspace" UI action now releases the underlying imbue_cloud-leased host's lease immediately rather than waiting the 7-day destroyed-host grace period for mngr's GC to run `delete_host`. The implementation lives in `mngr destroy`; minds' destroy command's previous intentional non-chaining of lease release is no longer correct, and the stale "Lease release is intentionally NOT chained here" comment in `destroying.py` is updated.

## [v0.2.8] - 2026-05-13

### Changed

- Changed: minds switched to per-host latchkey state — `LatchkeyForwardInfo.gateway_port` stamped for non-spawning consumers; minds now stores per-host `latchkey_permissions.json` under `<latchkey-dir>/mngr_latchkey/hosts/<host_id>/`.

## [v0.2.7] - 2026-05-11

### Added

- Added: New `minds run` command that spawns `mngr forward` as a subprocess and consumes its JSONL envelope stream instead of running forwarding in-process.
- Added: minds injects `LATCHKEY_DISABLE_COUNTING=1` into every workspace whenever latchkey is wired so the shared host-side gateway represents one user.
- Added: minds installs a grandparent-death watcher so the Python backend self-terminates ~3 s after Electron crashes, cascading into `mngr observe` / `mngr events` / latchkey children.

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
