# Changelog - mngr

A concise, human-friendly summary of changes for the `mngr` core library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: New `Volume.path_exists(path)` method across all providers (local, Docker, Modal) and `ScopedVolume`.
- Added: `mngr extras config` subcommand walking user-scope config gaps (today: default agent type for `mngr create`).
- Added: `mngr plugin list --kind {agent-type,provider}` filter projecting to canonical agent-type or provider backend names.
- Added: urwid single-select picker for `mngr extras` Install/Skip prompts (completion, claude-plugin) and the default agent type step inside `mngr extras -i`.
- Added: `HasTranscriptMixin` on `AgentInterface` formalises the raw-capture contract used by `mngr transcript`; `HasCommonTranscriptMixin` extends it with the gated common converter so future agent types get `mngr transcript` support by implementing two methods.
- Added: `--restart` and `--no-resume` flags on `mngr start` (restart-fresh and skip the resume message).
- Added: `read_shared_modal_env_name` helper plus `MNGR_TEST_SHARED_MODAL_ENV_NAME` opt-in for sharing a single Modal env across an offload-acceptance / offload-release run.
- Added: New `ProviderEmptyError` (distinct from `ProviderUnavailableError`) — providers raise it when the backend answered that there's nothing to list; the listing pipeline silently skips empty providers in every mode.
- Added: `pre_baked_agent_id` field on `HostInterface` (default `None`) so the duplicate-agent-name check in `api/create.py` skips the raise when the existing agent matches the host's pre-baked agent (lease-adopt scenario).
- Added: `is_for_host_creation: bool = False` parameter on `ProviderBackendInterface.build_provider_instance` so only `mngr create` can bootstrap host-creation state; read flows leave the default.

### Changed

- Changed: `mngr list --format json` no longer emits the redundant `address` field; same value remains on parsed `AgentDetails.address` / `HostDetails.address`.
- Changed: Restored Modal compatibility for the standard mngr Dockerfile (single-stage `python:3.12-slim`); source-dependent setup moved to `scripts/post-source-setup.sh` and reused via offload's `post_patch_cmd`. Bumped offload pin from 0.9.2 to 0.9.5.
- Changed: `mngr create` no longer hard-codes `claude` as the default agent type — it must come from a positional argument, `--type`, or `[commands.create] type` in user settings; the error lists registered types and points at `mngr config set`.
- Changed: `scripts/install.sh` no longer carries custom shell logic for the default agent type — that prompt now runs inside `mngr extras -i` and is re-runnable via `mngr extras config`.
- Changed: Bumped pinned Claude Code CLI version from `2.1.116` to `2.1.141` in the Dockerfile.
- Changed: Removed `mngr provision` (aka `mngr prov`) subcommand and its docs; provisioning still runs automatically during `mngr create`.
- Changed: Single-agent address resolution refactored — the "find" stage is strictly separate from the "ensure live" stage, with new `resolve_to_started_host_and_agent` / `resolve_to_started_host_and_running_agent` helpers and a unified `--start/--no-start` flag; `push`, `pull`, `provision`, and `rename` no longer require the agent to be running.
- Changed: Renamed the address-side `HostedLocation` type to `HostLocationAddress` to match its peers (`HostAddress`, `AgentAddress`); cascading internal renames across parsers and Click param types. No behavior change.
- Changed: `mngr rename` now works against offline hosts by writing the rename and labels into the provider's persisted agent data without starting the host; default flipped from `--start` to `--no-start`.
- Changed: `mngr create --type X` now fails fast with `UnknownAgentTypeError` when `X` does not resolve to a registered agent class (instead of silently falling back to `BaseAgent`); `--type X -- ...` is no longer a hidden alias for `--type command -- ...`.
- Changed: Removed the unused `Permission` primitive, `AgentPermissionsOptions`, `NoPermissionsAgentMixin`, host/agent `get_permissions`/`set_permissions`, the `--grant`/`--revoke` flags on `mngr limit`, and the `--grant` flag on `mngr create`. Higher-level libraries (latchkey, minds) keep their own permissions concepts.
- Changed: `ProviderError` and all subclasses now require `provider_name` as the first constructor argument; handlers can read `e.provider_name` without `isinstance` narrowing.
- Changed: Renamed mngr's "workspace server" feature to "system interface" — `/api/agents/{id}/restart-workspace-server` → `/api/agents/{id}/restart-system-interface`, SSE event `workspace_server_status` → `system_interface_status`.
- Changed: `mngr connect` no longer falls back to "most recently created agent" when run non-interactively without an explicit agent; cancelling the interactive selector now exits cleanly via `click.Abort`.
- Changed: `mngr create` for IMBUE_CLOUD adopts the lease scenario — `--reuse` signals the lease's pre-baked agent isn't a duplicate-name collision; the bake's services agent is now named `system-services` to match the user's expected name.
- Changed: mngr's generated tmux config (`~/.mngr/tmux.conf`) sets `status-left-length` to 20 so a full `mngr-...` session name shows in the status bar, written before the user's `~/.tmux.conf` is sourced.
- Changed: `mngr create --provider lima` help text shows `--memory=N` / `--disk=N` (plain integers, no `GiB` suffix).
- Changed: Adopted per-project changelog layout (`changelog/` dir, `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md` at the project root).

### Fixed

- Fixed: `tmux send-keys -l` and `tmux rename-session` now use the `--` end-of-options separator, so agent commands/messages and rename targets starting with `-` (e.g. `--model gemma`) are no longer misparsed by tmux.
- Fixed: `mngr list` no longer aborts with "Provider 'modal' is not available" when the Modal per-user environment hasn't been created yet — the Modal backend raises `ProviderEmptyError` and the listing pipeline silently skips it.
- Fixed: `Host._get_all_descendant_pids` now tracks a `visited` set so a PID-reuse cycle in the process tree can no longer drive the walker past Python's recursion limit; `host.stop_agents` no longer crashes with `RecursionError` on long-lived agents.
- Fixed: Two flaky destroy tests (`test_destroy_via_stdin`, `test_destroy_multiple_agents`) now use `@pytest.mark.timeout(60)` to accommodate modal-offload contention.
- Fixed: `mngr config` help text and the docs example now show the correct `--scope user` (was `--user`).

## [v0.2.8] - 2026-05-13

### Added

- Added: `mngr push` / `mngr pull` now accept the `@HOST[.PROVIDER]:PATH` syntax via the shared `HostedLocation` parser.

### Changed

- Changed: Address parsing refactored — typed `HostAddress` / `AgentAddress` / `NewAgentLocation` / `HostedLocation` parsed once at the Click boundary and threaded through the API layer; `HostName` no longer permits dots; many `parse_*` helpers and `api/agent_addr.py` deleted.
- Changed: `mngr create` — positional `AGENT_TYPE` now wins over a config-supplied `type`; `--type` defaults to `"claude"` and `--start-on-boot` to `False` directly (rather than resolved at runtime).
- Changed: `mngr.toml` unknown-field errors for `[agent_types.<name>]` now include a missing-plugin hint and list currently disabled plugins.
- Changed: Internal trace log lines emitted during `mngr create` (`_setup_per_agent_config_dir`, `_write_generated_files`) demoted from INFO to DEBUG.
- Changed: `cel-python` minimum bumped to `>=0.5.0` to match the global `mngr` install and surface strict-typo warnings reliably.

### Fixed

- Fixed: `mngr list` / `mngr kanpan` no longer log per-agent CEL warnings for `--include` / `--exclude` filters that reference keys on tolerant schemaless fields (`labels`, `plugin`, `host.tags`, `host.plugin`); typos against strict fields still warn.
- Fixed: `mngr clone <agent> <new-name>@.<provider>` (and `mngr migrate` cross-host moves) now succeeds — plugin-state rsync runs on the source agent's host so the Claude transcript / session history / memory carries over.
- Fixed: `mngr create` defaults — encoded the real defaults for options that previously listed a help-text default but were stored as `None` and resolved at runtime, and corrected `--worktree-base-folder` help.

## [v0.2.7] - 2026-05-11

### Added

- Added: `DockerBuildTimeoutError` raised (with config-knob hint) when `docker build` exceeds the per-provider `build_timeout_seconds`.
- Added: `mngr create -vv` emits a `Transferring agent files` log span (with `count=0` for the no-transfer path) around the per-file `write_file` loop.
- Added: New `MalformedJsonlLineError` (`imbue.mngr.errors`).
- Added: Spec `specs/expose-outer-host/concise.md` for exposing each host's outer machine via a new `OuterHost` base class and `mngr exec --outer` flag.

### Changed

- Changed: Default `docker build` timeout bumped from 5 to 10 minutes; configurable per provider via `build_timeout_seconds`.
- Changed: JSONL parsers (`MalformedJsonLineWarner.parse`, `parse_event_line`, `parse_discovery_event_line`, `parse_agents_from_mngr_output`, vps_docker's `_parse_batched_json_files`) now raise on malformed input instead of silently returning `None`.
- Changed: `mngr` accepts `host.provider` qualifier consistently anywhere a host identifier is taken (e.g. `mngr create --from @m1.modal:/path`, `mngr limit --host m1.modal`, `mngr snapshot create --host m1.modal`).
- Changed: Default transfer mode for same-host remote git sources is now `git-worktree` instead of `git-mirror`; same-host `--from` short-circuits to a single in-host `git push` / local rsync.

### Fixed

- Fixed: `mngr stop` no longer leaves orphaned grandchildren (e.g. `playwright-mcp`, `node`) alive on Linux when an agent's pane process was killed abruptly; `Host.stop_agents` now also enumerates processes by inherited `MNGR_AGENT_ID` via `/proc/<pid>/environ`.
- Fixed: `mngr message <agent> -m /clear` (and `/compact`) no longer hangs for 90 s — the `mngr-submit-<session>` tmux signal is now also fired from the SessionStart hook for `clear` / `compact` sources.
- Fixed: `mngr gc` no longer crashes on hosts whose SSH host key is missing from `known_hosts`; raises `HostAuthenticationError` (a trust failure) instead of generic `HostConnectionError`.
- Fixed: Spurious "Duplicate host name '127.0.0.1' found on provider 'docker'" warning on `mngr list` when multiple Docker containers run against a local daemon — `Host.get_name()` now returns the mngr-assigned host name; use `get_connector_host_name()` for the connector address.
- Fixed: `mngr plugin add` no longer warns about unknown config fields and unknown provider backends when the local config references plugins that haven't been installed yet.
- Fixed: `mngr create --from` against a remote source now works for any provider (the git-author / origin-URL lookups now route through the host interface); same-host `--transfer=git-worktree` is allowed.
- Fixed: `mngr create` now provisions credentials correctly inside nested sandboxes (e.g. Linux lima VM on macOS host) — `get_user_claude_config_dir()` falls back to `$CLAUDE_CONFIG_DIR` when `$ORIGINAL_CLAUDE_CONFIG_DIR` doesn't exist inside the VM.
- Fixed: One-event lag in remote (online-host) `events.jsonl` follow-mode tailing — the remote read now wraps `cat` with a sentinel so an absent trailing newline is detectable.
- Fixed: Local-host shell commands issued from worker threads no longer crash on Linux with `TypeError: child watchers are only available on the default loop` — `Host._run_shell_command` bypasses pyinfra's gevent `LocalConnector` for local hosts.
- Fixed: `resolve_provider_names_for_identifiers` no longer silently returns partial results when an identifier is unknown — returns `None` to signal a full discovery scan.
