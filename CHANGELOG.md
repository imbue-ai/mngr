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
- Added: `mngr_ovh` provider plugin running mngr agents in Docker on OVH classic VPS instances (OAuth2 / AK-AS-CK / `~/.ovh.conf` creds, IAM v2 tag-based discovery, TOFU host-key pinning, cancelled-VPS recycling, `mngr ovh list` operator command).
- Added: `GET /permissions/available` and `GET /permissions/available/<service_name>` catalog endpoints on the permissions gateway extension, backed by a shipped `services.json`; default agent permissions broadened to allow reading own permissions and the per-service catalog entry.
- Added: New `minds env recover` command with per-env recover-target file and per-env `flock`; every deploy captures pre-deploy Modal versions and creates Neon snapshot branches before touching external state.
- Added: Connector `GET /health/liveness` route; `minds env deploy`'s post-deploy `await_apps_healthy` polls both apps' liveness endpoints for up to 30s each.
- Added: Per-dev-env Neon project (`minds-<env>` with `host_pool` and `litellm_cost` DBs) provisioned by `minds env deploy` and deleted on destroy; `mngr imbue_cloud admin pool create` auto-resolves `--database-url` from the activated minds env.
- Added: Per-tier generation id at `secrets/minds/<tier>/generation` exposed at the connector's `GET /generation`; `minds env activate` auto-wipes stale env subdirs on generation mismatch and exports `MODAL_PROFILE` derived from the tier's `modal_workspace`.
- Added: `secrets/minds/<tier>/ovh` Vault template (AK / AS / CK); `secrets/minds/<tier>/neon-admin` now expects `NEON_ORG_ID` instead of `NEON_PROJECT_ID`.
- Added: `[lifecycle]` and `[min_containers]` blocks in tier `deploy.toml`; `DeployLifecycleConfig` validator rejects `writes_local_state=true` + `creates_resources=false` at parse time.
- Added: `load_or_create_encryption_key` validates the on-disk latchkey key file permission bits and raises `LatchkeyEncryptionKeyPermissionError` on group/other access.
- Added: `mngr push` / `pull` / `provision` / `rename` gain a `--start/--no-start` flag; new `resolve_to_started_host_and_agent` / `resolve_to_started_host_and_running_agent` helpers replace the prior `is_start_desired` / `skip_agent_state_check` flags.
- Added: TMR HTML reports mirrored to `s3://int8-shared-internal/tmr-reports/<run>.html`; public URL emitted as a structured `report_url` event; new `mngr tmr --run-name` flag and `TMR (reintegrate)` workflow.
- Added: Shell-level integration tests for `scripts/install.sh` (`test_install_script.py`).

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
- Changed: `permission-requests` latchkey extension now expects POST bodies with `agent_id` / `scope` / `permissions` / `rationale` (replacing `service_name`); minds desktop client fetches the permission catalog from the gateway instead of bundling `services.toml` and renders the dialog pre-checked with the union of existing grants and the agent's requested set.
- Changed: `LatchkeyGatewayClient.get_available_services` returns a typed `dict[str, AvailableServiceEntry]` with pydantic validation; the latchkey gateway client starts lazily on a background thread so `minds run` no longer blocks on the supervisor binding its port.
- Changed: Latchkey per-directory encryption key is no longer cached on the long-lived `Latchkey` pydantic model — it's read on each subprocess-spawn so the secret only lives in parent-process memory per call.
- Changed: minds now uses multi-environment deploys (`dev` / `staging` / `production`) backed by HCP Vault — each env owns one directory (`~/.minds/` for prod, `~/.minds-<env-name>/` otherwise) with `minds env activate` / `deactivate` for shell activation; `MINDS_ROOT_NAME` validation tightens to `minds(-<env-name>)?`.
- Changed: All tiers share one `minds env deploy` path driven by a `[lifecycle]` block; each deploy mints a fresh `MINDS_DEPLOY_ID` and pushes Modal Secrets under timestamped `<svc>-<tier>-<deploy_id>` names with end-of-deploy GC keeping the last 10; pool-hosts schema migrations move to a real `schema_migrations(version, applied_at)` table.
- Changed: Modal app + function names shortened (`remote-service-connector` → `rsc`, `fastapi_app` → `api`, `litellm-proxy` → `llm`, `litellm_app` → `proxy`) so deployed hostnames stay within the DNS 63-char limit; `DevEnvName` enforces a 40-char max and requires a `dev-` prefix.
- Changed: Modal provider no longer auto-creates an environment from non-`create` commands; new `is_for_host_creation` flag on `build_provider_instance` keeps the bootstrap reserved to `mngr create`.
- Changed: `DELETE /tunnels/{name}` and `POST /hosts/{id}/release` are idempotent at the HTTP layer; `minds env destroy` proceeds with cloud-side cleanup even when the local env root has been removed; `minds env list` resolves reserved-tier `client.toml` to the committed in-repo file.
- Changed: imbue-cloud pool bake swapped from Vultr to OVH — `mngr imbue_cloud admin pool create` is provider-generic with `--region` / `--tag`; new `minds pool create` / `list` / `destroy` CLI group; `minds env destroy` walks OVH via IAM v2 tags.
- Changed: `minds pool create` auto-injects the activated tier's OVH AK/AS/CK from Vault; `--management-public-key-file` derived from Vault by default.
- Changed: OVH bake produces a leasable state aligned with the adopt path — services agent always named `system-services`, bake destroys the FCT-bootstrap chat agent and its sentinel, `mngr exec` uses full agent addresses; lease/adopt rewrites the leased container's `/mngr/data.json` `host_name` via SFTP; `mngr` core's duplicate-agent-name check honors a new `pre_baked_agent_id` field on `HostInterface`.
- Changed: OVH provider — IAM tags attached immediately after `GET /vps` discovery so partial-failure orphans are still discoverable; `OuterHost.get_name` / `OuterHostInterface.get_name` return `str` instead of `HostName` (outer-host names contain dots); SSH paramiko sessions load private keys with a type-agnostic helper (Ed25519 / RSA / ECDSA); `Debian 12 - Docker` SSH key sudo-copied from `/home/debian` to root's home via new `bootstrap_ssh_user` field.
- Changed: `ProviderError` base class now carries `provider_name`; every subclass requires it as the first constructor argument so handlers can read `e.provider_name` without isinstance narrowing.
- Changed: LiteLLM-proxy deploys run a Prisma schema push automatically via a new `migrate_db` Modal Function.
- Changed: Faster `forever-claude-template` Dockerfile builds — Playwright / Chromium install deferred via a new `deferred-install` service; warm-cache rebuilds drop from ~1m33s to ~25.6s.
- Changed: Renamed `vps_ip` → `vps_address` end-to-end across API models, call sites, and the `pool_hosts.vps_ip` DB column (idempotent migration shipped).
- Changed: Renamed address-side `HostedLocation` → `HostLocationAddress` (and `parse_hosted_location` → `parse_host_location_address`, etc.) so it lines up with `HostAddress` / `AgentAddress`.
- Changed: `mngr push` / `pull` / `provision` no longer require the agent to be running; `mngr connect` no longer falls back to "most recently created agent" non-interactively; cancelling the interactive selector exits cleanly via `click.Abort`.
- Changed: `mngr create --type X` fails fast with `UnknownAgentTypeError` when X doesn't resolve to a registered agent class; `--type X -- ...` is no longer a hidden alias for `--type command -- ...`.
- Changed: `mngr_lima` drops `ssh-keyscan` — each Lima VM gets a pre-generated ed25519 host keypair injected via the provision script with per-host `known_hosts` files under `<provider-dir>/keys/hosts/<host_id>/`.
- Changed: TMR GitHub Actions workflow defaults `MNGR_USER_ID` to a shared `tmr-ci` namespace and reads inbound-SSH keys from `.github/tmr-authorized-keys`; run names are now a compact `YYYYMMDDHHMMSS` timestamp (random hex removed) and a new `tmr_role` label drives integrator filtering.
- Changed: `scripts/release.py` refuses to cut a release when there are unconsolidated entries in `changelog/`, printing the one-liner that triggers the consolidation schedule on demand.

### Removed

- Removed: Unused `libs/flexmux/` project and all references (justfile recipes, `EXCLUDED_RATCHET_PROJECTS` exclusions, `uv.lock` workspace member).
- Removed: `litellm-connector` Modal Secret from `[secrets].services` (it was never vault-backed); deploy now pushes it as a separate code-driven step and the `_DERIVED_ONLY_SECRET_SERVICES` carve-out is deleted.
- Removed: Standalone deploy scripts (`scripts/deploy_remote_service_connector.sh`, `scripts/deploy_litellm.sh`, `scripts/push_modal_secrets.py`); their work folds into the unified `minds env deploy`.

### Fixed

- Fixed: Cloned claude agent now actually resumes the source agent's conversation — `_adopt_cloned_session` renames the project subdir to the destination's realpath-resolved encoding, drops the stale `sessions-index.json`, writes the real `claude_session_id`, and carries forward `claude_session_id_history`.
- Fixed: `tmux send-keys -l` and `tmux rename-session` now use the `--` end-of-options separator, so agent commands/messages and rename targets starting with `-` (e.g. `--model gemma`) are no longer misparsed by tmux.
- Fixed: minds desktop client tolerates legacy `service_name` field on `RequestResponseEvent` records on disk by dropping it before pydantic validation, eliminating per-startup warning floods and unresolved pending requests.
- Fixed: Streamed permission-request handler dedupes redeliveries by `event_id`, preventing unbounded inbox growth and duplicate `Streamed latchkey permission request ...` log spam.
- Fixed: minds startup race where a stale latchkey gateway port was cached and every subsequent call failed with `Connection refused` — `LatchkeyGatewayClient` self-heals on connect-level errors, and supervisor restart + pre-warm now run sequentially on one background thread.
- Fixed: Race in `mngr_latchkey`'s per-directory encryption-key resolution where a concurrent reader could observe an empty key file — the file is now published atomically via temp file + `fsync` + `os.link`.
- Fixed: `mngr_lima` serial-log tailer now uses portable `tail -F` instead of GNU-only `tail --follow=name --retry`, restoring macOS Lima boot diagnostics.
- Fixed: Connector `_authenticate_supertokens` passes `override_global_claim_validators=lambda *_: []` so the explicit "Email not verified" 401 surfaces; matching `_get_user_id_from_access_token` skips claim validation so `/auth/session/revoke` works for unverified users.
- Fixed: Connector auth endpoints no longer 500 on `/auth/session/revoke`, `/auth/email/is-verified`, `/auth/email/send-verification` — every async endpoint converted to sync `def` with SuperTokens `syncio` modules so the live FastAPI / uvicorn loop is no longer trapped by `loop.run_until_complete`.
- Fixed: `minds env recover` now `modal app stop`s the deployed app when the captured pre-deploy version is `None` (first-ever deploy), so the app doesn't 500 against just-deleted Modal Secrets.
- Fixed: `minds env deploy` is now actually idempotent against Neon — `create_neon_project` / `delete_neon_project` look up by name first, preventing silent multi-project leaks and wrong-project destroys under name collisions.
- Fixed: `mngr list` no longer aborts with "Provider 'modal' is not available" when the Modal per-user env hasn't been created — Modal backend raises new `ProviderEmptyError` and the listing pipeline silently skips empty providers in every mode.
- Fixed: `mngr config` help text and docs example corrected from `--user` to `--scope user`.
- Fixed: OVH outer-bootstrap installs `rsync` (the OVH `Debian 12 - Docker` image lacks it); rsync also added to cloud-init backends for symmetry.
- Fixed: `pool_hosts` INSERT now writes `host_name` (the schema migration that added the NOT NULL column was never reflected in the bake's INSERT, leaking a fully-provisioned VPS on every successful bake).
- Fixed: Three deploy-safety correctness bugs in `_deploy_env_locked` — Neon snapshot + recover-target write moved before pool-hosts migrations; `verify_neon_token_has_restore_scope` actually called as preflight; `write_recover_target_atomic` wrapped with snapshot-cleanup on failure.
- Fixed: OVH provider — `MNGR_VPS_EXTRA_TAGS` parsed at the top of `_provision_vps` before any order; `set_renew_at_expiration` retries on the transient `"subscription is not active yet"` 400; `order_and_wait_for_vps` matches the assigned serviceName via the order's operations chain instead of diffing `/vps`, eliminating cross-order swaps.
- Fixed: OVH provider blockers surfaced on first end-to-end `mngr create --provider ovh` — post-delivery race in `order_and_wait_for_vps` (drains `deliverVm`); `destroy_instance` actually cancels via `PUT /serviceInfos` instead of email-only `/terminate`; `set_renew_at_expiration(False)` restores `renew.automatic=true` so cancelled VPSes don't silently lose auto-renewal.
- Fixed: Lima provider's guest → host port forwarding is now actually disabled — emits two ignore rules for `guestIP: 0.0.0.0` and `127.0.0.1`; `merge_lima_yaml` locks `portForwards` against user overrides.

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
