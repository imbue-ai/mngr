# Changelog

A concise, human-friendly summary of changes. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: New `Volume.path_exists(path)` method across all providers (local, Docker, Modal) and `ScopedVolume`.
- Added: `gemini` agent type plugin (`imbue-mngr-gemini`) wiring Google's Gemini CLI into mngr.
- Added: `mngr latchkey admin-jwt` and `mngr latchkey gateway-info` subcommands; `LatchkeyForwardInfo.gateway_port` stamped for non-spawning consumers.
- Added: `mngr extras config` subcommand walking user-scope config gaps (today: default agent type for `mngr create`).
- Added: `mngr plugin list --kind {agent-type,provider}` filter projecting to canonical agent-type or provider backend names.
- Added: `use_env_config_dir` option on the `claude` agent type config so local Claude agents share `$CLAUDE_CONFIG_DIR` instead of provisioning a per-agent dir.
- Added: urwid single-select picker for `mngr extras` Install/Skip prompts (completion, claude-plugin) and the default agent type step inside `mngr extras -i`.
- Added: `mngr usage` per-session cost aggregation with separate `subscription_cost` / `api_cost` aggregates, `--since`, `--detail`, and CEL/format-template surfaces; cost tracking now works for direct `ANTHROPIC_API_KEY` users.
- Added: `mngr_ovh` provider plugin running mngr agents in Docker on OVH classic VPS (`vps-2025-model1`), with `mngr ovh list [--all]` operator command, IAM v2 tag-based discovery, cancelled-VPS recycling, and TOFU host-key pinning.
- Added: Workspace-server restart UX — dedicated `/agents/<id>/recovery` page with SSE health stream, sidebar "Restart workspace server" menu, landing-page status badges, and a styled 503 loader; minds tracks per-agent HEALTHY/STUCK/RESTARTING state via `workspace_backend_failure` envelopes.
- Added: `permissions` latchkey gateway catalog endpoints (`GET /permissions/available[/<service>]`) plus a `services.json` data file shipped alongside the extensions; agent baseline permissions broadened to allow self-read and per-service catalog read.
- Added: `minds env activate`/`deactivate` shell-based env switching exporting `MINDS_ROOT_NAME` and friends; `--create` flag for fresh dev envs; `MINDS_ROOT_NAME` validation tightened to `minds(-<env-name>)?`.
- Added: `minds env recover` command plus per-env `.minds-deploy-recover-target-<env>.json` and `flock`-based deploy locks; every deploy snapshots a Neon branch (`pre-deploy-<deploy_id>`) and captures pre-deploy Modal app versions for rollback.
- Added: `minds pool` CLI group (`create`/`list`/`destroy`) shelling into `mngr imbue_cloud admin pool`, auto-injecting `minds_env=<active-env>` tag and the activated tier's OVH AK/AS/CK from Vault.
- Added: Per-dev-env Neon project (`minds-<env>`) with `host_pool` + `litellm_cost` databases, `apps/minds/imbue/minds/envs/migrations.py` schema runner, and `apps/remote_service_connector/migrations/003_vps_address.sql`.
- Added: `--run-name` flag on `mngr tmr`, a `TMR (reintegrate)` GitHub Actions workflow, and S3 mirror of TMR HTML reports to `s3://int8-shared-internal/tmr-reports/<run>.html` (with `report_url` event and `http://go/shared/tmr-reports/<run>.html` URL).
- Added: `tmr-ci` shared `MNGR_USER_ID` namespace and `.github/tmr-authorized-keys` for inbound SSH on TMR CI hosts; `tmr_role` agent label (testing/snapshotter/integrator) and `AgentKind.SNAPSHOTTER`.
- Added: Connector `GET /health/liveness` no-auth route used by `minds env deploy`'s post-deploy probe (replaces the `/docs` probe).
- Added: `[create_templates.ovh]` block in `forever-claude-template` and `MNGR_VPS_EXTRA_TAGS=k=v,...` parsing for attaching extra OVH IAM v2 tags.
- Added: `--start/--no-start` flag on `mngr push`, `mngr pull`, `mngr provision`, `mngr rename`; new `resolve_to_started_host_and_agent` / `resolve_to_started_host_and_running_agent` helpers in `imbue.mngr.api.find`.
- Added: `pre_baked_agent_id` field on `HostInterface` so `mngr create --reuse` recognizes baked pool-host agents without tripping the duplicate-name pre-flight check.
- Added: Shell-level integration tests for `scripts/install.sh` (`test_install_script.py`) covering `uv tool` branches, PATH-not-set, and `mngr dependencies -i` / `mngr extras -i` continue-on-failure.

### Changed

- Changed: Bumped bundled Latchkey to 2.11.1.
- Changed: `mngr tmr` testing agents now publish a single `outputs.tar.gz` archive into the per-agent volume API, replacing the rsync + git-pull finalization; SSH provider no longer supported for testing-agent outputs.
- Changed: `mngr list --format json` no longer emits the redundant `address` field; same value remains on parsed `AgentDetails.address` / `HostDetails.address`.
- Changed: Restored Modal compatibility for the standard mngr Dockerfile (single-stage `python:3.12-slim`); source-dependent setup moved to `scripts/post-source-setup.sh` and reused via offload's `post_patch_cmd`. Bumped offload pin from 0.9.2 to 0.9.5.
- Changed: Switched mngr-latchkey + minds permission management to latchkey 2.9.0's `permission_requests` / `permissions` gateway extensions; `LATCHKEY_MIN_VERSION` bumped to 2.9.0 and minds streams pending requests via `GET /permission-requests?follow=true`.
- Changed: `mngr create` no longer hard-codes `claude` as the default agent type — it must come from a positional argument, `--type`, or `[commands.create] type` in user settings; the error lists registered types and points at `mngr config set`.
- Changed: `scripts/install.sh` no longer carries custom shell logic for the default agent type — that prompt now runs inside `mngr extras -i` and is re-runnable via `mngr extras config`.
- Changed: Bumped pinned Claude Code CLI version from `2.1.116` to `2.1.141` in the Dockerfile and CI workflow installs.
- Changed: `mngr schedule add --verify quick|full` now works when the trigger's `mngr create` produces an agent inside the cron-runner's local provider; verification runs inside the container and reports back over a structured sentinel line.
- Changed: CI acceptance wall-clock cut ~62% — `contents: write` granted so offload image-cache git notes push, `max_parallel` lowered 200→50 for better LPT packing, and the `mngr_modal` session-end leak detector reshaped (typed `ModalCleanupOutcome`, `pytest_sessionfinish` hook).
- Changed: `mngr_claude_subagent_proxy` typed `subagent_type` (e.g. `imbue-code-guardian:verify-and-fix`) now preserves Claude Code's system-prompt contract in both PROXY and DENY modes by resolving on-disk agent definitions.
- Changed: minds split the services agent from the initial chat agent — the primary agent runs only bootstrap/services and is hidden; a real chat agent named after the host is created on first boot and every subsequent agent shares its `CLAUDE_CONFIG_DIR`. Existing workspaces must be re-created.
- Changed: minds "Create a Project" Name field now sets the host name (validated via `HostName` regex); the agent is always `system-services`, and `imbue_cloud` `/hosts/lease` and `/hosts` gain a required `host_name`.
- Changed: Regenerated CLI docs for `mngr tmr` and `mngr latchkey`.
- Changed: Minds moved to multi-environment deploys (`dev`, `staging`, `production`) backed by HCP Vault; per-env data root `~/.minds-<env>/` with split `client.toml` + `secrets.toml`; standalone `scripts/deploy_remote_service_connector.sh`, `deploy_litellm.sh`, and `push_modal_secrets.py` folded into `minds env deploy`.
- Changed: `minds env deploy` is now one path for every tier driven by a required `[lifecycle]` block in each tier's `deploy.toml`; Modal app/function names shortened (`remote-service-connector`→`rsc`, `litellm-proxy`→`llm`, `fastapi_app`→`api`, `litellm_app`→`proxy`); `DevEnvName` capped at 40 chars; pool-hosts schema backed by a `schema_migrations` table.
- Changed: Every `minds env deploy` mints `MINDS_DEPLOY_ID` and pushes Modal Secrets under timestamped `<svc>-<tier>-<deploy_id>` names with end-of-deploy GC (last 10 per svc-tier); Modal apps hard-fail at module load if `MINDS_DEPLOY_ID` is missing.
- Changed: `permission-requests` latchkey endpoint switched to `{agent_id, scope, permissions, rationale}` shape (replacing `service_name`); minds desktop client adapted, legacy `service_name` events tolerated on load, and `LatchkeyGatewayClient.get_available_services` returns a typed `dict[str, AvailableServiceEntry]`.
- Changed: Latchkey permission dialog default check state is now the union of existing grants and the agent's requested permissions (no longer pre-checks the catch-all `any`).
- Changed: `mngr rename` now works against offline hosts (writes to the provider's persisted agent data without starting the host); `--start/--no-start` defaults to `--no-start`.
- Changed: `mngr push`/`pull`/`provision` no longer require the agent to be running; `mngr connect` no longer falls back to the most-recently-created agent when run non-interactively without an explicit agent.
- Changed: Renamed `HostedLocation` → `HostLocationAddress` (and `parse_hosted_location` → `parse_host_location_address`, `ResolvedHostedLocation` → `ResolvedHostLocationAddress`, Click param-type display name `hosted_location` → `host_location_address`).
- Changed: `mngr create --type X` fails fast with `UnknownAgentTypeError` when `X` doesn't resolve to a registered type (or a `[agent_types.X]` block with a known `parent_type`); the bare `--type X -- ...` alias for `--type command` is gone.
- Changed: `scripts/release.py` blocks releases when unconsolidated `changelog/` entries exist and prints the exact `mngr schedule` trigger line.
- Changed: Modal provider no longer auto-creates its per-user environment from read flows; only `mngr create` may bootstrap, plumbed via a new `is_for_host_creation: bool = False` parameter on `ProviderBackendInterface.build_provider_instance`.
- Changed: `vps_ip` renamed to `vps_address` end-to-end (API models, Python call sites, and the `pool_hosts.vps_ip` DB column); `min_containers` for the deployed `rsc-<tier>` and `llm-<tier>` apps is now driven by a `[min_containers]` block in each tier's `deploy.toml`.
- Changed: Imbue-cloud pool bake swapped from Vultr to OVH; `mngr imbue_cloud admin pool create` is now provider-generic with required `--region` and repeatable `--tag KEY=VALUE`; tier Vault path is `<tier>/ovh`; `OvhProviderConfig.recycle_safety_margin_hours` default dropped from 24 to 2.
- Changed: Connector auth endpoints converted from `async def` to sync `def` (with `syncio` SuperTokens imports) so SuperTokens' `loop.run_until_complete` no longer hits "event loop is already running" 500s; OAuth callbacks bridge via `supertokens_python.async_to_sync_wrapper.sync`.
- Changed: Latchkey per-directory encryption key is no longer cached on the `Latchkey` model — re-read (and minted) on every spawn; `load_or_create_encryption_key` validates the on-disk key file's permission bits and raises `LatchkeyEncryptionKeyPermissionError` with a `chmod 600` hint when group/other bits are set.
- Changed: `LatchkeyGatewayClient` self-heals from a stale cached gateway URL on connect-level transport failures; supervisor restart and gateway-client pre-warm now serialize on a single thread at minds startup.
- Changed: `mngr_lima` no longer ssh-keyscans — each Lima VM gets a pre-generated ed25519 host keypair injected via the provision script, with per-host `known_hosts` under `<provider-dir>/keys/hosts/<host_id>/`; `merge_lima_yaml` now extends `provision` and `mounts` instead of replacing them.
- Changed: Lima provider emits two `portForwards` ignore rules (`guestIP: 0.0.0.0` with `guestIPMustBeZero: true`, and `guestIP: 127.0.0.1`) to actually suppress guest→host forwarding fallback; `portForwards` is locked against user `--file` overrides.
- Changed: TMR run names compacted to a single `YYYYMMDDHHMMSS` timestamp used consistently across the output directory, `tmr_run_name` label, and all spawned entity names; testing agents are `tmr-<run>-<test_name>`, branches `mngr-tmr/<run>/<test_name>`.
- Changed: `OuterHost.get_name` / `OuterHostInterface.get_name` return `str` (was `HostName`) so dotted hostnames (`vps-x.vps.ovh.us`, IPv4) parse cleanly; the `Host` subclass's `get_name` still returns `HostName`.
- Changed: `ProviderError` now carries `provider_name` on the base class; every subclass requires it as the first constructor argument so handlers can read `e.provider_name` without isinstance narrowing.
- Changed: LiteLLM-proxy deploys now run a Prisma schema push against the proxy's `DATABASE_URL` via a new `migrate_db` Modal Function, removing the manual `prisma db push` step.
- Changed: Faster minds Docker build — Playwright + Chromium install deferred to first boot via a new `deferred-install` service; manifest-first layering preserves the `uv sync` / `npm ci` warm caches across code-only edits; `forever-claude-template` `.dockerignore` symlinked to `.gitignore` with `**/`-prefixed patterns. Warm-rebuild measured at ~25.6s (down from ~1m33s).
- Changed: `mngr config` help text and docs example corrected from `--user` to `--scope user`.

### Removed

- Removed: Unused `libs/flexmux/` project and all references (justfile recipes, `EXCLUDED_RATCHET_PROJECTS` exclusions, `uv.lock` workspace member).

### Fixed

- Fixed: Cloned claude agent now actually resumes the source agent's conversation — `_adopt_cloned_session` renames the project subdir to the destination's realpath-resolved encoding, drops the stale `sessions-index.json`, writes the real `claude_session_id`, and carries forward `claude_session_id_history`.
- Fixed: `tmux send-keys -l` and `tmux rename-session` now use the `--` end-of-options separator, so agent commands/messages and rename targets starting with `-` (e.g. `--model gemma`) are no longer misparsed by tmux.
- Fixed: Streamed permission-request handler dedupes redeliveries by `event_id`, so the in-memory inbox no longer grows unbounded and the desktop log no longer fills with duplicate `Streamed latchkey permission request ...` lines.
- Fixed: `mngr_latchkey` race where a concurrent reader could observe an empty per-directory encryption-key file — the key file is now published atomically via temp file + `fsync` + `os.link`.
- Fixed: Recovery-page flashing on stale `workspace_backend_failure` envelopes after a successful restart (now ignored within a short grace window); workspace-loader spinner no longer visibly jumps on the 1-second auto-refresh.
- Fixed: Lima serial-log tailer switched to `tail -F` for macOS BSD-tail portability; the previous `tail --follow=name --retry` was GNU-only and silently dropped serial-log diagnostics during VM boot on macOS.
- Fixed: OVH `Debian 12 - Docker` outer bootstrap now installs `rsync` (the image ships docker but not rsync, which `mngr_vps_docker`'s build-context upload needs); `rsync` also added to `cloud_init.generate_cloud_init_user_data`'s package list.
- Fixed: `pool_hosts` INSERT in `_create_single_pool_host` now picks up the schema's `host_name NOT NULL` column; the SQL is hoisted to a module-level `_INSERT_POOL_HOST_SQL` with a regression test asserting every required column appears.
- Fixed: `ImbueCloudProvider.create_host` SFTPs into the leased container and rewrites `/mngr/data.json:host_name` to the user-supplied name so the FCT bootstrap's first-start chat agent is named correctly instead of inheriting the bake's placeholder.
- Fixed: `mngr create` duplicate-agent-name pre-flight check in `api/create.py` now honors `host.pre_baked_agent_id`, so the lease-adopt scenario no longer trips the "agent already exists" raise.
- Fixed: `minds env deploy` is now idempotent against Neon — Neon's REST API doesn't 409 on duplicate project names, so `create_neon_project` / `delete_neon_project` look up by name via `_find_projects_by_name`, adopt on a single match, and raise loudly with a cleanup recipe on multiple matches (no silent leak / wrong-project-destroy).
- Fixed: Connector auth endpoints (`/auth/session/revoke`, `/auth/email/is-verified`, `/auth/email/send-verification`) no longer 500 due to nested event-loop errors.
- Fixed: `mngr list` no longer aborts with "Provider 'modal' is not available" for an empty Modal env — new `ProviderEmptyError` distinguishes "backend answered with nothing" from "couldn't ask", and the listing pipeline drops empty providers in every mode.
- Fixed: Deploy-safety ordering — Neon snapshot + recover-target write now happen BEFORE pool-hosts migrations; `verify_neon_token_has_restore_scope` is actually called as a preflight after Neon project resolution; failed recover-target writes best-effort delete the just-created Neon snapshot before re-raising.
- Fixed: OVH provider correctness — `parse_extra_tags_env` runs at the top of `_provision_vps` (before any OVH API call); `set_renew_at_expiration` retries on OVH's transient `"subscription is not active yet"` 400 (5-minute budget, 15s poll); `order_and_wait_for_vps` correlates via `orderId` + operations chain instead of diffing `/vps` listings, so concurrent orders can never swap serviceNames.
- Fixed: OVH provider bootstrap blockers — post-delivery `deliverVm` task drain before `/rebuild`; `destroy_instance` uses `PUT /serviceInfos` with `renew.deleteAtExpiration=true` (instead of the email-confirmation-only `POST /terminate`); restoring `renew.automatic=true` + `renewalType=automaticV2012` when un-cancelling; sudo-copying the rebuild SSH key into root's home (default `bootstrap_ssh_user="debian"`); type-agnostic SSH private-key loader (Ed25519/RSA/ECDSA); IAM tags attached immediately so failures during rebuild/TOFU/bootstrap leave a discoverable orphan VPS.
- Fixed: `_authenticate_supertokens` and `_get_user_id_from_access_token` now pass `override_global_claim_validators=lambda *_: []` to the SuperTokens session getter so the explicit `if not is_verified: raise 401 "Email not verified"` check fires for unverified tokens instead of being shadowed by the SDK's generic `Invalid token` rejection.
- Fixed: `DELETE /tunnels/{name}` and `POST /hosts/{id}/release` are idempotent at the HTTP layer — a second call returns 200 with `{"status": "already_deleted"}` / `{"status": "already_released"}` instead of 404.
- Fixed: `DeployLifecycleConfig` rejects `writes_local_state=true` + `creates_resources=false` at deploy.toml parse time instead of `AssertionError`ing partway through deploy.
- Fixed: `minds env destroy` proceeds with cloud-side cleanup even when the local env root has already been removed by hand; `destroy_env` no longer raises `DevEnvNotFoundError` for missing-root.
- Fixed: `minds env list` resolves reserved-tier (`production` / `staging`) `client.toml` to the committed in-repo file instead of showing `(no client.toml)`; `DevEnvSummary` gains a `client_config_source` field.
- Fixed: `minds env deploy`'s `find_monorepo_root` check now runs BEFORE Vault credential read and `make_deploy_id`, so running from outside the monorepo fails cleanly rather than after logging a misleading `Deploy id: ...` line.
- Fixed: `litellm-connector` Modal Secret is now pushed as a separate code-driven step at the end of the secret-push loop (it was never vault-backed); the `_DERIVED_ONLY_SECRET_SERVICES` carve-out is deleted and `[secrets].services` becomes a truthful vault-backed-only list.
- Fixed: `minds env recover` now `modal app stop`s the deployed app when the captured pre-deploy app version is `None` (a first-ever deploy of the env/tier), instead of leaving an app 500'ing on every request after secret deletion.
- Fixed: Cancelling the interactive agent selector now exits cleanly via `click.Abort` instead of returning silently.

## [v0.2.8] - 2026-05-13

### Added

- Added: New `imbue-mngr-latchkey` package owning the shared latchkey gateway lifecycle, per-agent wiring, and reverse SSH tunnel; ships as a `mngr` plugin with `mngr latchkey forward` / `create-agent-env` / `link-permissions` subcommands plus a `LatchkeyForwardSupervisor` that minds spawns detached.
- Added: New `mngr usage` command reporting Claude Code's rolling 5h / 7d / overage quota usage with `human`/`json`/`jsonl` formats and the standard agent-filter flags.
- Added: New `mngr usage wait --until <CEL>` command that blocks until a usage snapshot matches a predicate, with exit codes mirroring `mngr wait`.
- Added: New experimental `mngr_claude_subagent_proxy` plugin that reroutes Claude Code's `Task` tool through mngr-managed subagents, with `PROXY` (default) and `DENY` modes and a `mngr-subagents` Claude skill teaching the explicit two-command spawn-and-wait protocol.
- Added: New `mngr tmr --additional-authorized-host` repeatable flag for installing SSH public keys on every agent host.
- Added: `mngr_user_id` / `additional_authorized_hosts` `workflow_dispatch` inputs to the TMR GitHub Actions workflow.
- Added: `mngr push` / `mngr pull` now accept the `@HOST[.PROVIDER]:PATH` syntax via the shared `HostedLocation` parser.

### Changed

- Changed: Latchkey state is now keyed per-host instead of per-agent — `finalize_agent_permissions` → `finalize_host_permissions`, `permissions_path_for_agent` → `permissions_path_for_host`, and `mngr latchkey link-permissions` takes `--host-id`.
- Changed: `mngr tmr --use-snapshot` no longer re-uploads the code repo per test agent — agents source from on-host `/code` via `git-worktree`; modal launches additionally skip the per-agent initial filesystem snapshot.
- Changed: Address parsing refactored — typed `HostAddress` / `AgentAddress` / `NewAgentLocation` / `HostedLocation` parsed once at the Click boundary and threaded through the API layer; `HostName` no longer permits dots; many `parse_*` helpers and `api/agent_addr.py` deleted.
- Changed: Background processes started via `ConcurrencyGroup.run_process_in_background()` now default to `is_checked_by_group=True`, so non-zero exits surface as `ProcessError` at group teardown instead of being silently swallowed.
- Changed: `CHANGELOG.md` is now version-organized — `[Unreleased]` accumulates categorized bullets across cron runs and `scripts/release.py` renames it on each release.
- Changed: Changelog consolidator groups entries by PR-landed committer date (America/Los_Angeles) and emits one `## YYYY-MM-DD` section per distinct date in `UNABRIDGED_CHANGELOG.md`.
- Changed: Consolidation cron auto-merges `origin/main` before forking the per-run branch, so each PR's diff is just the consolidation commit.
- Changed: `mngr create` — positional `AGENT_TYPE` now wins over a config-supplied `type`; `--type` defaults to `"claude"` and `--start-on-boot` to `False` directly (rather than resolved at runtime).
- Changed: `mngr list` for `imbue_cloud` now drives discovery through outer (VPS-root) SSH instead of inner-container SSH, showing true container state (`RUNNING`/`STOPPED`/`CRASHED`/`PAUSED`/`DESTROYED`) and full details even when inner sshd is unreachable.
- Changed: `[plugins.claude_subagent_proxy]` is disabled in the project-level `.mngr/settings.toml`; the `mngr-subagents` skill no longer recommends `--reuse` on `mngr create` so slug collisions surface as a hard error.
- Changed: `mngr.toml` unknown-field errors for `[agent_types.<name>]` now include a missing-plugin hint and list currently disabled plugins.
- Changed: TMR GitHub Actions workflow uses the canonical `--format` flag (the previous `--output-format` was not a real option).
- Changed: Internal trace log lines emitted during `mngr create` (`_setup_per_agent_config_dir`, `_write_generated_files`) demoted from INFO to DEBUG.
- Changed: `cel-python` minimum bumped to `>=0.5.0` to match the global `mngr` install and surface strict-typo warnings reliably.

### Removed

- Removed: Latchkey on-disk gateway record (`<plugin_data_dir>/latchkey_gateway.json`) and all cross-process gateway adoption helpers — `LatchkeyForwardSupervisor` guarantees one `mngr latchkey forward` per directory.

### Fixed

- Fixed: `mngr list` / `mngr kanpan` no longer log per-agent CEL warnings for `--include` / `--exclude` filters that reference keys on tolerant schemaless fields (`labels`, `plugin`, `host.tags`, `host.plugin`); typos against strict fields still warn.
- Fixed: `mngr clone <agent> <new-name>@.<provider>` (and `mngr migrate` cross-host moves) now succeeds — plugin-state rsync runs on the source agent's host so the Claude transcript / session history / memory carries over.
- Fixed: `mngr create` defaults — encoded the real defaults for options that previously listed a help-text default but were stored as `None` and resolved at runtime, and corrected `--worktree-base-folder` help.
- Fixed: `mngr latchkey forward` no longer dies with its parent — the parent-death watcher was removed and SIGHUP is wired into the clean coupled-lifetime shutdown path for the interactive case.

## [v0.2.7] - 2026-05-11

### Added

- Added: New `mngr_forward` plugin (`libs/mngr_forward/`) that serves `<agent-id>.localhost:8421/*` subdomain forwarding, signed login cookies, optional reverse SSH tunnels (`--reverse`), and CEL agent/event filters; SIGHUP bounces just the `mngr observe` child.
- Added: New `minds run` command that spawns `mngr forward` as a subprocess and consumes its JSONL envelope stream instead of running forwarding in-process.
- Added: New `mngr_imbue_cloud` plugin with `mngr imbue_cloud` CLI (`auth`, `hosts`, `keys litellm`, `tunnels`, `admin pool`) that owns SuperTokens auth, pool-host leasing, LiteLLM keys, and Cloudflare tunnels; multi-account modelled as multiple provider instances.
- Added: TMR `integrator_branch` event on `mngr tmr`'s structured stdout (`--format jsonl`/`json`) and a manually-dispatched `.github/workflows/tmr.yml` workflow.
- Added: `mngr_kanpan` field-value staleness — each `FieldValue` carries a `created` timestamp, taint propagates through cached inputs, and stale cells render dimmed; new `staleness_threshold_seconds` config.
- Added: `DockerBuildTimeoutError` raised (with config-knob hint) when `docker build` exceeds the per-provider `build_timeout_seconds`.
- Added: `mngr create -vv` emits a `Transferring agent files` log span (with `count=0` for the no-transfer path) around the per-file `write_file` loop.
- Added: minds injects `LATCHKEY_DISABLE_COUNTING=1` into every workspace whenever latchkey is wired so the shared host-side gateway represents one user.
- Added: minds installs a grandparent-death watcher so the Python backend self-terminates ~3 s after Electron crashes, cascading into `mngr observe` / `mngr events` / latchkey children.
- Added: Per-PR changelog entry system in `changelog/` with nightly consolidation into `UNABRIDGED_CHANGELOG.md` and a version-organized `CHANGELOG.md`; idempotent setup at `scripts/setup_changelog_agent.sh`.
- Added: New meta ratchet `test_every_project_excludes_tests_from_wheel` enforcing a uniform wheel-exclude pattern across every package.
- Added: New `MalformedJsonlLineError` (`imbue.mngr.errors`) and `MalformedMngrOutputError` (`imbue.minds.errors`).
- Added: Spec `specs/expose-outer-host/concise.md` for exposing each host's outer machine via a new `OuterHost` base class and `mngr exec --outer` flag.

### Changed

- Changed: Bumped bundled Latchkey to 2.8.0 and switched minds to a single shared `latchkey gateway` subprocess for all agents, with password-protected `LATCHKEY_GATEWAY_LISTEN_PASSWORD`.
- Changed: `mngr tmr` HTML reports gain a dedicated "Failed" section separate from "Blocked" (infrastructure failures vs. agent-reported BLOCKED), and now include rows for launch-failed agents.
- Changed: Reverse-tunnel health check now retries broken tunnels forever (capped at one attempt per 5 minutes via exponential backoff) instead of giving up after 10 consecutive failures.
- Changed: Default `docker build` timeout bumped from 5 to 10 minutes; configurable per provider via `build_timeout_seconds`.
- Changed: Upgraded offload from 0.8.1 → 0.9.2 in CI with history-based test scheduling, thin-diff application fix, and propagation of `GITHUB_HEAD_REF` / `GITHUB_REF_NAME` to sandboxes.
- Changed: `mngr imbue_cloud admin pool create` post-create read-back is now scoped to `--provider` (default `vultr`) and uses `--on-error continue`; broken `just create-pool-hosts*` recipes and `apps/remote_service_connector/scripts/create_pool_hosts.py` deleted in favour of the plugin command.
- Changed: `mngr_modal` — `ModalMode.TESTING` removed from production paths (tests inject `TestingModalInterface` via `make_testing_provider`); `make_testing_provider` collapsed onto the shared `_construct_modal_provider` factory; `enable_output_capture` is now an abstract method on `ModalInterface`.
- Changed: Workspace wheels uniformly exclude `*_test.py`, `test_*.py`, `**/conftest.py`, `**/testing.py` — previously `libs/mngr` was leaking three test helpers.
- Changed: JSONL parsers (`MalformedJsonLineWarner.parse`, `parse_event_line`, `parse_discovery_event_line`, `parse_agents_from_mngr_output`, vps_docker's `_parse_batched_json_files`) now raise on malformed input instead of silently returning `None`.
- Changed: minds "Create a Project" screen — removed the .env checkbox, added an independent "AI provider" choice, renamed "Launch mode" to "Compute provider", added an optional `GH_TOKEN` Advanced field; revoked imbue_cloud sessions auto-disable the matching provider block.
- Changed: minds no longer persists `imbue_cloud` account identity to disk; only the workspace↔account map lives on disk, identity is sourced on demand.
- Changed: `mngr` accepts `host.provider` qualifier consistently anywhere a host identifier is taken (e.g. `mngr create --from @m1.modal:/path`, `mngr limit --host m1.modal`, `mngr snapshot create --host m1.modal`).
- Changed: Default transfer mode for same-host remote git sources is now `git-worktree` instead of `git-mirror`; same-host `--from` short-circuits to a single in-host `git push` / local rsync.
- Changed: `remote_service_connector.add_service` is now idempotent; updating an access list no longer fails Cloudflare 81053 ("DNS record already exists").
- Changed: Connector schema migration replaces `pool_hosts.version` with `attributes JSONB`; legacy `version` callers are folded into `attributes` automatically.
- Changed: `scripts/setup_changelog_agent.sh` redeploys when re-run (removes any existing schedule first) and drops the `CHANGELOG_REPLACE=1` gate; the consolidation cron's commit author is now `bot@imbue.com`.

### Removed

- Removed: `LaunchMode.DEV` from minds — the web create form, `/create`, and `/api/create-agent` now only offer `LOCAL` / `LIMA` / `CLOUD` / `IMBUE_CLOUD`; the DEV-only latchkey helper and `MINDS_ALLOW_HOST_LOOPBACK` env var are gone.
- Removed: `apps/minds_workspace_server/` from the monorepo — migrated to `forever-claude-template`'s `apps/system_interface/` and consumed at runtime; the release Dockerfile's node/npm install step is dropped.

### Fixed

- Fixed: `mngr stop` no longer leaves orphaned grandchildren (e.g. `playwright-mcp`, `node`) alive on Linux when an agent's pane process was killed abruptly; `Host.stop_agents` now also enumerates processes by inherited `MNGR_AGENT_ID` via `/proc/<pid>/environ`.
- Fixed: `mngr message <agent> -m /clear` (and `/compact`) no longer hangs for 90 s — the `mngr-submit-<session>` tmux signal is now also fired from the SessionStart hook for `clear` / `compact` sources.
- Fixed: `mngr gc` no longer crashes on hosts whose SSH host key is missing from `known_hosts`; raises `HostAuthenticationError` (a trust failure) instead of generic `HostConnectionError`.
- Fixed: `mngr forward` subprocess no longer pegs ~130% CPU after agents disconnect — the bidirectional relay (lifted to `mngr_forward/relay.py`) now terminates when the paramiko channel has received EOF.
- Fixed: `minds run` no longer pegs a CPU as agents/hosts come and go — reverse-tunnel bookkeeping is pruned on agent destroy and the 30 s health-check loop applies per-tunnel exponential backoff.
- Fixed: WebSocket broadcaster queue-full flood — stuck WS clients are evicted after 50 consecutive queue-full broadcasts, and the broadcaster cancels wedged handler tasks blocked on `send_text(...)`.
- Fixed: Spurious "Duplicate host name '127.0.0.1' found on provider 'docker'" warning on `mngr list` when multiple Docker containers run against a local daemon — `Host.get_name()` now returns the mngr-assigned host name; use `get_connector_host_name()` for the connector address.
- Fixed: `mngr plugin add` no longer warns about unknown config fields and unknown provider backends when the local config references plugins that haven't been installed yet.
- Fixed: `mngr create --from` against a remote source now works for any provider (the git-author / origin-URL lookups now route through the host interface); same-host `--transfer=git-worktree` is allowed.
- Fixed: `mngr create` now provisions credentials correctly inside nested sandboxes (e.g. Linux lima VM on macOS host) — `get_user_claude_config_dir()` falls back to `$CLAUDE_CONFIG_DIR` when `$ORIGINAL_CLAUDE_CONFIG_DIR` doesn't exist inside the VM.
- Fixed: `mngr tmr` no longer crashes the whole orchestrator when a single agent fails its initial-message send — launching loops also catch `AgentError`, and failed launches render as errored entries in HTML reports.
- Fixed: `mngr tmr` integrator launch (and any local-provider test-agent launch) no longer always fails with "Failed to generate a unique host name after 100 attempts"; TMR now reuses the existing local host when the provider is `local`.
- Fixed: One-event lag in remote (online-host) `events.jsonl` follow-mode tailing — the remote read now wraps `cat` with a sentinel so an absent trailing newline is detectable.
- Fixed: Closing the last tab in a minds workspace no longer leaves a blank screen — the primary agent's chat tab is automatically reopened when the dockview becomes empty.
- Fixed: Local-host shell commands issued from worker threads no longer crash on Linux with `TypeError: child watchers are only available on the default loop` — `Host._run_shell_command` bypasses pyinfra's gevent `LocalConnector` for local hosts.
- Fixed: `apps/minds/scripts/propagate_changes` now protects `.claude/settings.local.json` from `rsync --delete`, preventing per-agent `UserPromptSubmit` hook loss and the resulting 90 s `send_message` hang.
- Fixed: `claude plugin update` SessionStart hook no longer hangs Modal-launched agents at the `ssh` TOFU prompt — `scripts/claude_update_plugin.sh` now uses `GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes'`.
- Fixed: `mngr_modal` post-merge CI failure — restored the per-test reset of `ModalProviderBackend._app_registry` (load-bearing for cross-test isolation after the testing-factory reshape).
- Fixed: `mngr_modal` session env registered with the leak-detection registry so silent CLI cleanup failures are caught at session end; CLI cleanup helpers now surface non-zero exits as warnings.
- Fixed: `resolve_provider_names_for_identifiers` no longer silently returns partial results when an identifier is unknown — returns `None` to signal a full discovery scan.
- Fixed: Changelog consolidation cron commit author email corrected from `dev@imbue.com` to `bot@imbue.com` so GitHub attributes commits to the bot account whose token it uses.
