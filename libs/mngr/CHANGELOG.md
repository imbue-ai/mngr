# Changelog - mngr

A concise, human-friendly summary of changes for the `mngr` core library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: New `mngr stop --stop-host` flag stops an agent's whole host (every agent on it) instead of just the named agent. For container-backed providers it stops the container while the underlying machine keeps running; rejected on providers that don't support it; cannot be combined with `--archive`. Idempotent (an already-stopped host reports success). Targets multiple hosts concurrently rather than serially. Resolves the host without SSH (via the discovery event stream and the provider's SSH-free `get_host`) so `mngr stop --stop-host` works even when sshd is unreachable.

### Changed

- Changed: **Breaking** — `AgentError`, `HostError`, and all their subclasses (e.g. `NoCommandDefinedError`, `AgentNotFoundOnHostError`, `SendMessageError`, `AgentStartError`, `HostConnectionError`, `HostOfflineError`, `HostAuthenticationError`, `CommandTimeoutError`, `HostDataSchemaError`) now inherit from `MngrError` instead of `BaseMngrError`. `BaseMngrError` has been removed entirely; `MngrError` now inherits directly from `click.ClickException`. The remaining `BaseMngrError`-only error types (`PluginSpecifierError`, `DiscoverySchemaChangedError`, `MalformedJsonlLineError`, `TolerantPathError`, `IssueSearchError`) were moved the same way. Every mngr error is now a `ClickException`, so any escaped error at the CLI renders as a clean `Error: ...` message instead of a Python traceback. Redundant `MngrError` mix-ins on `AgentNotFoundError` / `DuplicateAgentNameError` were removed, and `except` clauses listing an error type already covered by another in the same clause were collapsed.
- Changed: Renamed `emit_final_json` to `write_json_line` in `imbue.mngr.cli.output_helpers` (sibling to the existing `write_human_line`). The old name was misleading — despite "final JSON" implying the single terminating object emitted in `--format=json` mode, it was also called from streaming JSONL callbacks (e.g. `mngr list`).
- Changed: Installed Node.js in the shared mngr Docker image (`resources/Dockerfile`), pinned to `apps/minds/package.json`'s `engines.node` (24.15.0). Node is a runtime dependency of the mngr_latchkey gateway's `.mjs` extensions and minds Python tests that evaluate `apps/minds/todesktop.js` via `node`.

### Fixed

- Fixed: `mngr list --format json` no longer crashes on `ProviderErrorInfo` when no agents were returned. With `--on-error continue` and a per-provider failure, the empty-agents path was passing raw `ErrorInfo` pydantic models to `json.dumps`, raising `TypeError: Object of type ProviderErrorInfo is not JSON serializable`. The empty-agents path now goes through the same `_emit_json_output` serializer as the non-empty path, producing a clean `{"agents": [], "errors": [...]}` payload.
- Fixed: Indefinite hang in git-push / rsync over SSH on macOS hosts where `SSH_AUTH_SOCK` routes to 1Password's biometric SSH agent. The shared `build_ssh_transport_command` now pins authentication to the explicit `-i` key via `-o IdentitiesOnly=yes -o IdentityAgent=none`. Without these flags, OpenSSH consults `SSH_AUTH_SOCK` first; in BatchMode (no TTY) the biometric prompt can never fire and ssh blocks forever on the agent reply.

## [v0.2.10] - 2026-06-01

### Added

- Added: New repeatable `--post-host-create-command` flag on `mngr create` that runs shell commands inside a newly-created host synchronously after the host is online but before any agent work_dir is touched. Stackable from `create_templates.<name>` via `post_host_create_command__extend = [...]`. Replaces the FCT-specific `use_image_default_cmd` opt-out and the defensive `--workdir /` exec override.
- Added: New `register_help_topics` plugin hook so installed plugins can contribute standalone `mngr help` topic pages. Each topic is a `TopicHelpPage` with explicit metadata (key, description, aliases, see-also) whose body is either `InlineContent(markdown=...)` or `DocFile(path=...)`. Plugin topics that collide with a built-in topic key or alias are skipped.
- Added: New `offline_agent_field_generators` plugin hook (mirror of `agent_field_generators` for offline/unreachable hosts). `mngr list` threads it through `get_host_and_agent_details` so offline plugin fields are usable in columns and CEL filters; discovery snapshots now preserve plugin fields via `discovered_agent_from_agent_details`.
- Added: Tab completion now suggests every command and help topic as an argument to `mngr help` (e.g. `mngr help <TAB>`).
- Added: `DocFile.source_url` field that resolves relative and anchor links inside doc-backed help topics against a canonical GitHub blob URL pinned to the installed release tag (falling back to `main` when the version can't be read), so the rendered terminal hyperlinks open the right GitHub page/section.

### Changed

- Changed: **Breaking** — `CreateAgentOptions.agent_type` is now a required field (previously `AgentTypeName | None` defaulting to `None`). The residual `agent_type or AgentTypeName("claude")` fallbacks in `api.create.create` and `Host.create_agent_state` are removed, and the now-dead `if options.agent_type is not None:` guard around agent-type provisioning merging in `Host.provision_agent` is dropped.
- Changed: **Breaking** — `on_before_create` and `on_before_host_create` plugin hooks now receive `MngrContext` as a parameter, giving plugins access to config, the plugin manager, and the concurrency group. Plugins implementing these hooks must add an `mngr_ctx` parameter to their signatures.
- Changed: `mngr help <topic>` now renders markdown nicely in an interactive terminal (headings, bold, code, links, tables) via `rich`, with paragraphs wrapped to the terminal width; the same rendering is applied to command `--help` description and sections. Non-interactive output (pipes, scripts) stays plain. `rich` is imported lazily so it does not affect CLI startup time.
- Changed: Built-in topic docs are now shipped inside the wheel via `force-include` of the topic doc dirs, fixing a bug where `mngr help <topic>` showed no doc-based topics in a PyPI/wheel install.
- Changed: The plugin-facing `TopicHelpPage` model now lives in `imbue.mngr.interfaces.help_topic` (so the plugin hookspec can reference it without the plugins layer importing the CLI); the runtime topic registry lives in `imbue.mngr.cli.help_topics`. mngr's own built-in topics are registered through `register_help_topics` as a built-in plugin from an explicit registry — no directory scanning or heading parsing.
- Changed: Docker provider untags the per-host build image (`mngr-build-<host_id>`) on `destroy_host` (and again, defensively, on `delete_host`) so built images no longer pile up in `docker images`. Snapshot images keep their own layers.
- Changed: `mngr destroy` now actually destroys a host when its last agent is destroyed, regardless of `min_online_host_age_seconds`. Ghost-only matches (agents returned by discover but absent from the host's own `get_agents()`) escalate to `provider.destroy_host` instead of silently dropping the match, and a post-loop sweep re-checks `host.get_agents()` and calls `destroy_host` directly when empty. Cloud-side resources are released immediately rather than waiting for the destroyed-host grace period.
- Changed: `build_check_and_install_packages_command` (`providers/ssh_host_setup.py`) now `mkdir -p`s the symlink target before creating the `host_dir` symlink, so the docker_vps unified-volume layout can seed `<volume>/host_dir` before pointing `/mngr` at it.
- Changed: Regenerated the CLI reference docs to include the new `mngr imbue_cloud bucket` command group.
- Changed: Installed `restic` in the mngr Docker image (`libs/mngr/imbue/mngr/resources/Dockerfile`) so the offload test image exercises a real local restic repository, matching the minds app's new runtime dependency.

### Fixed

- Fixed: The default `discover_hosts_and_agents` now tolerates per-host SSH failures. `HostConnectionError` / `HostAuthenticationError` / `HostOfflineError` are caught per host, `on_connection_error(host_id)` is invoked so providers can drop wedged cache entries, and broken hosts fall back to `to_offline_host(host_id).discover_agents()` so the rest of the provider's hosts (and their agents) come through normally. Previously a single unreachable host blanked the entire provider's discovery, which 503'd every workspace through `mngr_forward` and tripped minds' recovery page for healthy workspaces.
- Fixed: `mngr help <topic>` now produces clickable terminal hyperlinks for relative and anchor links inside doc-backed topics by resolving them against the topic's `source_url` (previously a link like `[Idle Detection](idle_detection.md)` rendered as a dead terminal hyperlink whose relative target meant nothing to a terminal or browser).

## [v0.2.9] - 2026-05-28

### Added

- Added: New `Volume.path_exists(path)` method across all providers (local, Docker, Modal) and `ScopedVolume`.
- Added: `mngr extras config` subcommand walking user-scope config gaps (today: default agent type for `mngr create`).
- Added: `mngr plugin list --kind {agent-type,provider}` filter projecting to canonical agent-type or provider backend names.
- Added: urwid single-select picker for `mngr extras` Install/Skip prompts (completion, claude-plugin) and the default agent type step inside `mngr extras -i`.
- Added: New `UNKNOWN` value on `AgentLifecycleState` and `HostState`, defined as "the provider that owns this agent/host could not be accessed during the most recent discovery attempt." `AgentObserver` emits sticky UNKNOWN entries for previously-observed agents whose provider just failed.
- Added: `FullDiscoverySnapshotEvent` carries new `providers` and `error_by_provider_name` fields; `mngr observe --discovery-only` now emits a snapshot on every poll even when zero providers succeed.
- Added: `--restart` and `--no-resume` flags on `mngr start` for restarting a running agent and skipping the resume message.
- Added: New `HasTranscriptMixin` / `HasCommonTranscriptMixin` on `AgentInterface` formalising the raw-capture contract for `mngr transcript`.
- Added: `pre_baked_agent_id` field on `HostInterface` so `mngr create --reuse` honors the imbue_cloud lease-adopt scenario when the duplicate-name check would otherwise fire.
- Added: New `is_for_host_creation: bool = False` parameter on `ProviderBackendInterface.build_provider_instance` — only `mngr create` may bootstrap host-creation state; read flows leave the default.
- Added: Unified config override mechanism — every field is settable via `MNGR__X__Y__Z` env vars (double underscore maps to the dotted path `x.y.z`), `--setting x.y.z=value`, or `mngr config set x.y.z value`, all routed through one resolver.
- Added: `__extend` operator suffix on a leaf key (e.g. `cli_args__extend`) opts into additive merging (append for lists/tuples, key-merge for dicts, union for sets); `mngr config extend KEY VALUE` writes the `__extend` form.
- Added: `mngr config schema` lists every settable key with its type and effective value; `mngr config list --all` now includes default-valued fields.
- Added: New top-level `allow_settings_key_assignment_narrowing` setting (default `false`) — assigning over a non-empty list/tuple/dict/set from a lower-precedence layer raises `ConfigParseError` unless opted in or expressed via `__extend`.

### Changed

- Changed: `mngr list --format json` no longer emits the redundant `address` field; same value remains on parsed `AgentDetails.address` / `HostDetails.address`.
- Changed: Restored Modal compatibility for the standard mngr Dockerfile (single-stage `python:3.12-slim`); source-dependent setup moved to `scripts/post-source-setup.sh` and reused via offload's `post_patch_cmd`. Bumped offload pin from 0.9.2 to 0.9.5.
- Changed: `mngr create` no longer hard-codes `claude` as the default agent type — it must come from a positional argument, `--type`, or `[commands.create] type` in user settings; the error lists registered types and points at `mngr config set`.
- Changed: `scripts/install.sh` no longer carries custom shell logic for the default agent type — that prompt now runs inside `mngr extras -i` and is re-runnable via `mngr extras config`.
- Changed: Bumped pinned Claude Code CLI version from `2.1.116` to `2.1.141` in the Dockerfile.
- Changed: Renamed the mngr-side "workspace server" feature to "system interface"; HTTP endpoint `/api/agents/{id}/restart-workspace-server` → `/restart-system-interface` and SSE event `workspace_server_status` → `system_interface_status`.
- Changed: Renamed address-side `HostedLocation` → `HostLocationAddress` (with cascading renames to `parse_*`, `resolve_*`, `ResolvedHostedLocation`, `HostedLocationParamType`, `HOSTED_LOCATION` → `HOST_LOCATION_ADDRESS`, and the click param-type display name `host_location_address`).
- Changed: Unified agent-address resolution in single-agent subcommands — `push` / `pull` / `provision` / `rename` use `resolve_to_started_host_and_agent`; `connect` / `capture` use `resolve_to_started_host_and_running_agent`. `push` / `pull` / `provision` no longer require the agent to be running; all four gain a `--start/--no-start` flag.
- Changed: `mngr connect` no longer falls back to "most recently created agent" when run non-interactively without an explicit agent.
- Changed: `mngr list` no longer aborts with "Provider 'modal' is not available" when the Modal env hasn't been created yet — the listing pipeline silently skips providers raising `ProviderEmptyError`.
- Changed: `mngr list` no longer skips its side-effect snapshot when some providers failed; the snapshot now includes the per-provider error info.
- Changed: `ProviderError` now carries `provider_name` on the base class; every subclass requires it as the first constructor argument.
- Changed: `mngr rename` defaults to `--no-start` and works against offline hosts by writing into the provider's persisted agent data.
- Changed: `mngr create --provider lima` help text now shows `--memory=N` / `--disk=N` (plain integers).
- Changed: mngr's generated `~/.mngr/tmux.conf` widens `status-left-length` to 20 so a full `mngr-…` session name shows in the status bar.
- Changed: Discovery polling no longer retries failures at the top level — providers retry their own transient failures before raising.
- Changed: Replaced the `gemini` entry in `PLUGIN_CATALOG` with `antigravity`; `AntigravitySignalCheck` detects the new CLI via `agy --version`.
- Changed: Breaking — config layer merging is now assign-by-default for every aggregate (list, tuple, dict, set); configs relying on implicit cross-scope concatenation (e.g. accumulating `cli_args`) must use `field__extend` to stay additive, and agent-type parent-type inheritance likewise no longer auto-concatenates. CLI tuple/list flags (`--env`, `--label`, `--extra-window`, …) are a deliberate carve-out: they still extend the settings-file value (config-supplied entries first, CLI values appended), preserving the prior "settings → CLI" layering. No compatibility shim is provided; the major-version bump is the migration signal.
- Changed: The `__extend` operator and the assignment-narrowing guard apply uniformly to nested `agent_types.<name>`, `providers.<name>`, `create_templates.<name>`, and `plugins.<name>` fields; create templates now assign-by-default with pipeline order `config_defaults → templates → CLI`.
- Changed: Config field names may no longer contain `__` (reserved as the env-var path separator and `__extend` suffix); renamed env var `MNGR_RETAIN_LOCK_FOR_FAILED_HOSTS_DURING_CREATE` → `MNGR_DEBUG_RETAIN_LOCK_FOR_FAILED_HOSTS_DURING_CREATE`.
- Changed: BREAKING — `is_allowed_in_pytest` config field now defaults to `False` (was `True`). During a pytest run, `load_config` checks every config layer (user/project/local) individually and refuses to run if any picked-up config file doesn't opt in, so a real config can't ride in under a test config that does. If no config file is picked up, mngr runs normally.
- Changed: "Settings narrowing detected" error now names **both** implicated layers for each offending key — the file doing the assignment and the lower-precedence file whose value would be dropped — each with its resolved file path and the matching `mngr config set --scope <user|project|local>` flag; the `MNGR__*` env-var layer is named explicitly (it has no `config set` scope).
- Changed: Corrected the `is_error_reporting_enabled` config field description — it now matches actual behavior (suggesting a diagnostic agent on an unexpected interactive error) instead of describing the long-removed prompt-to-file-a-GitHub-issue flow.
- Changed: Bumped the offload version baked into `libs/mngr/imbue/mngr/resources/Dockerfile` from `0.9.5` to `0.9.7` to track the CI pin; v0.9.6 adds `offload run --override-image-id <ID>` (Modal-only) for skipping image setup.

### Removed

- Removed: `mngr provision` (aka `mngr prov`) subcommand and its docs; provisioning still runs automatically during `mngr create`.
- Removed: The agent-`permissions` concept from `mngr` core — `Permission`, `AgentPermissionsOptions`, `NoPermissionsAgentMixin`, `get_permissions`/`set_permissions`, the `--grant`/`--revoke` flags on `mngr limit`, and `--grant` on `mngr create` are gone. Higher-level libraries (latchkey, minds) keep their own permission concepts.
- Removed: The buggy `--on-error continue` flag the outer `mngr observe` was passing to its inner `mngr observe --discovery-only` child.
- Removed: The `MNGR_COMMANDS_<CMD>_<PARAM>`, `MNGR_ENABLE_PARAMIKO_LOGGING`, and `MNGR_AGENT_READY_TIMEOUT` env vars; the latter two are promoted to config fields `logging.enable_paramiko_logging` and `agent_ready_timeout` (settable via `MNGR__*`).
- Removed: BREAKING — `mngr push` and `mngr pull` (with `--sync-mode={files,git}` / `--rsync-only`) are gone, replaced by three thin primitives that each wrap a single operation: `mngr rsync SOURCE DESTINATION`, `mngr git push TARGET [-- GIT_ARGS...]`, and `mngr git pull SOURCE [-- GIT_ARGS...]`. The mngr-side `--source-branch` / `--target-branch` / `--mirror` / `--uncommitted-changes` / `--dry-run` flags are gone; use the corresponding git flags directly (`feature:main` refspec syntax, `--force --tags refs/heads/*:refs/heads/*` for a mirror push, `--dry-run`, `--rebase`, etc.). No compatibility shim. API: `pull_files`/`push_files`/`pull_git`/`push_git` in `imbue.mngr.api.sync` are replaced by `rsync_from_remote`, `rsync_to_remote`, `git_pull`, `git_push`, and a top-level two-endpoint `rsync(...)`; `git_push`/`git_pull` take `extra_args: Sequence[str]` and raise `GitSyncError` on failure (no structured return value); `SyncMode`, `GitSyncResult`, `NotAGitRepositoryError` are gone; `SyncFilesResult` renamed to `RsyncResult`.
- Removed: BREAKING — `MNGR_ALLOW_PYTEST=1` env-var escape hatch is gone; the pytest config guard is now per-config (`is_allowed_in_pytest = true` in each config file that should opt in).

### Fixed

- Fixed: `tmux send-keys -l` and `tmux rename-session` now use the `--` end-of-options separator, so agent commands/messages and rename targets starting with `-` (e.g. `--model gemma`) are no longer misparsed by tmux.
- Fixed: `mngr create --type X` now fails fast with `UnknownAgentTypeError` when `X` doesn't resolve to a registered agent class, instead of silently resolving to a generic `BaseAgent`.
- Fixed: `Host._get_all_descendant_pids` no longer hits `RecursionError` on a PID-reuse cycle in the process tree, unsticking `host.stop_agents` on long-lived agents' cleanup paths.
- Fixed: `mngr config` help text and docs example use `--scope user` instead of the nonexistent `--user`.
- Fixed: tmux `-t` targets now use the exact-match `=` prefix, so commands no longer misroute to the wrong agent under session-name prefix collision (e.g. `gemini` vs `gemini-to-antigravity`) — fixing wrong-session kill/send/capture and stopped-agent state misreports.
- Fixed: `--setting allow_settings_key_assignment_narrowing=...` is now rejected with a clear error pointing to the `settings.toml` / `MNGR__ALLOW_SETTINGS_KEY_ASSIGNMENT_NARROWING=true` form. The narrowing guard runs before `--setting` is applied, so the flag could never take effect there — previously the narrowing error misleadingly suggested `--setting` as the remedy and a `--setting` value was silently accepted without affecting the guard.

## [v0.2.8] - 2026-05-13

### Added

- Added: `mngr push` / `mngr pull` now accept the `@HOST[.PROVIDER]:PATH` syntax via the shared `HostedLocation` parser.

### Changed

- Changed: Address parsing refactored — typed `HostAddress` / `AgentAddress` / `NewAgentLocation` / `HostedLocation` parsed once at the Click boundary and threaded through the API layer; `HostName` no longer permits dots; many `parse_*` helpers and `api/agent_addr.py` deleted.
- Changed: `mngr create` — positional `AGENT_TYPE` now wins over a config-supplied `type`; `--type` defaults to `"claude"` and `--start-on-boot` to `False` directly (rather than resolved at runtime).
- Changed: `mngr.toml` unknown-field errors for `[agent_types.<name>]` now include a missing-plugin hint and list currently disabled plugins.
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
