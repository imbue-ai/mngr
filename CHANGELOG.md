# Changelog

A concise, human-friendly summary of changes. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: New `Volume.path_exists(path)` method across all providers (local, Docker, Modal) and `ScopedVolume`.
- Added: `gemini` agent type plugin (`imbue-mngr-gemini`) wiring Google's Gemini CLI into mngr.
- Added: `mngr extras config` subcommand walking through user-scope config gaps (currently the default agent type for `mngr create`).
- Added: `mngr plugin list --kind agent-type|provider` filter projecting plugins to canonical agent-type or provider names.
- Added: `use_env_config_dir` option on the `claude` agent type so local Claude agents share the user's `$CLAUDE_CONFIG_DIR` instead of provisioning a per-agent dir.
- Added: `mngr latchkey admin-jwt` and `mngr latchkey gateway-info` subcommands for shell-driven access to the shared gateway.

### Changed

- Changed: Bumped bundled Latchkey to 2.11.1.
- Changed: `mngr tmr` testing agents now publish a single `outputs.tar.gz` archive into the per-agent volume API, replacing the rsync + git-pull finalization; SSH provider no longer supported for testing-agent outputs.
- Changed: `mngr list --format json` no longer emits the redundant `address` field; same value remains on parsed `AgentDetails.address` / `HostDetails.address`.
- Changed: Restored Modal compatibility for the standard mngr Dockerfile — single-stage `python:3.12-slim`, source-dependent setup moved to `scripts/post-source-setup.sh` and wired as offload's `post_patch_cmd`.
- Changed: Bumped offload pin from 0.9.2 to 0.9.5.
- Changed: Switched latchkey permission management to the latchkey 2.9.0 gateway extensions (`permission_requests.mjs`, `permissions.mjs`); `LATCHKEY_MIN_VERSION` bumped 2.8.0 → 2.9.0. Agents still posting `LATCHKEY_PERMISSION` via the old `events.jsonl` channel will no longer reach the minds inbox.
- Changed: `mngr create` no longer hard-codes `claude` as the default agent type; must come from a positional argument, `--type`, or `[commands.create] type` in user settings.
- Changed: `mngr extras -i` and `scripts/install.sh` no longer contain custom shell logic for the default agent type; `mngr extras -i` walks through it as a step, and `mngr extras completion` / `mngr extras claude-plugin` now use the same urwid picker.
- Changed: Bumped pinned Claude Code CLI from `2.1.116` to `2.1.141`.
- Changed: `mngr schedule add --verify quick|full` now works when the trigger's `mngr create` produces an agent inside the cron-runner's local provider; verification runs inside the container and reports back over a structured sentinel line.
- Changed: `mngr_claude_subagent_proxy` typed `subagent_type` (e.g. `imbue-code-guardian:verify-and-fix`) now preserves Claude Code's system-prompt contract — PROXY mode prepends the resolved agent definition body; DENY mode appends a one-line pointer.
- Changed: minds split the services agent from the chat agent — the primary `system-services` agent runs only bootstrap/background services and is hidden from the UI; chat agents are created post-boot, share `CLAUDE_CONFIG_DIR`, and can be destroyed without tearing down services. Existing pre-change workspaces are not migrated.
- Changed: `mngr usage` now aggregates cost per-session within a `--since` window (default 24h), keeps subscription vs `ANTHROPIC_API_KEY` spend in separate aggregates, and exposes `subscription_cost` / `api_cost` on the `mngr usage wait --until` CEL surface. The Claude statusline writer also now emits `cost_snapshot` events when only `cost` is present (direct API-key users).
- Changed: minds "Name" field on the create-project form now sets the *host* name (validated via `HostName`); the agent is always called `system-services`. The imbue_cloud connector grows a required `host_name` on `/hosts/lease` and `/hosts`.
- Changed: Regenerated CLI docs for `mngr tmr` and `mngr latchkey`.
- Changed: CI acceptance speedups — granted `contents: write` to `test-offload`/`test-offload-acceptance` so offload's image-cache `git push` succeeds; lowered `max_parallel` from 200 → 50 in `offload-modal-acceptance.toml` for better LPT batching.

### Removed

- Removed: Unused `libs/flexmux/` project and all references (justfile recipes, `EXCLUDED_RATCHET_PROJECTS` exclusions, `uv.lock` workspace member).

### Fixed

- Fixed: Cloned claude agents now actually resume the source agent's conversation — `_adopt_cloned_session` renames the project subdir to the destination's realpath-resolved encoding, drops the stale `sessions-index.json`, writes the JSONL filename stem as `claude_session_id`, and carries `claude_session_id_history` forward; `--adopt-session` shares the same finalize step.
- Fixed: tmux argv-parsing footguns — `tmux send-keys -l` and `tmux rename-session` now use `--` so agent commands/messages and session prefixes starting with `-` are no longer misparsed as flags.
- Fixed: `mngr_modal` session-end leak detector no longer false-positives during Modal's eventually-consistent post-delete listing — cleanup deregisters only on synchronous DELETE/NOT_FOUND, and the check runs in `pytest_sessionfinish` after all session-scoped teardowns.

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
