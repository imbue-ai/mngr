# Changelog - mngr

A concise, human-friendly summary of changes for the `mngr` core library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `mngr config wizard` — interactive command that walks through common one-time user-scope configuration (e.g. whether to isolate the Claude config dir for local agents). Each step short-circuits when its setting is already configured, so it's safe to re-run.
- Added: `mngr create --format json` now includes `outer_host_public_key` and `container_host_public_key` when the provider exposes them (via a new `get_ssh_host_public_keys` host/provider method, defaulting to none), so tooling can pin a host's sshd key for strict host-key checking instead of trust-on-first-use.
- Added: Unified cross-actor host lock — state-changing operations (`create`, `start`, `gc`, ...) all acquire `Host.lock_cooperatively`, which holds a real `flock(2)` on the host's `host_lock` file (over a long-lived SSH exec channel on remote hosts). The previous separate `mngr start` lock is removed. `create` and `start` block indefinitely until the lock is acquired (a contended `create` now waits rather than failing); `gc` keeps a bounded wait. `flock` (`util-linux`) is now a required host dependency, bootstrapped alongside `git`/`tmux`/`jq`/`sshd`.
- Added: `--post-host-create-outer-command` create option (and matching `post_host_create_outer_command` create-template / settings key) runs shell commands once on the host's outer machine after the host is created (e.g. to install a VM-level systemd unit). Skipped with a warning when the provider exposes no outer host.
- Added: `[tmux]` config section — `tmux.attach_args` (extra tmux client flags inserted before the `attach` subcommand; the motivating case is `["-CC"]` for iTerm2 control mode), `tmux.additional_config_path` (extra config file sourced into every mngr session — never overwritten, unlike auto-generated `~/.mngr/tmux.conf`), `tmux.primary_window_name` (default `agent`, mngr now names the agent's primary window and targets it by name instead of `:0`, so mngr works regardless of the user's tmux `base-index` setting). In-flight sessions are self-healed on first inspection so lifecycle detection, messaging, capture, attach, and ttyd keep working across the upgrade.
- Added: Shared `AgentUpdatePolicy` (`AUTO` / `ASK` / `NEVER`) used by agent plugins to govern an agent CLI's self-updater. Defaults to `NEVER` for unattended agents and `ASK` for attended agents that implement an interactive update flow.
- Added: Shared `verify_pinned_cli_version` installation helper for agent plugins to verify an installed CLI matches a pinned version. It matches the user's pinned string verbatim against the `--version` banner (no version scheme assumed, so pre-release / four-component pins work).
- Added: Shared operator-command output helpers in `mngr.cli.output_helpers` (`emit_operator_result`, `OperatorResultPart.shown`, `shown_if`, `write_event_line`), used by the `mngr aws` / `mngr azure` / `mngr gcp` prepare/cleanup commands to render JSON / JSONL / human output in one place.

### Changed

- Changed: Unauthenticated providers handled consistently across `mngr list`. A new shared `ProviderNotAuthorizedError` (subclass of `ProviderUnavailableError`) carries structured `short_reason` / `short_remediation` fields plus verbose help text. `mngr list --on-error continue` runs the rest of the listing, then reports every failing provider in one consistent block: a single glanceable line per provider on stderr for human output (`<provider>: <reason> — <remediation> (disable: mngr config set ...)`), and structured entries in the `errors` array (with `exception_type`, `help_text`, and an `is_provider_inaccessible` flag) for `--format json` / `jsonl`. The default `--on-error abort` still fails immediately on the first provider error. `mngr list` now exits with the granular provider-inaccessible code (6) when every error is a provider that could not be reached or authenticated, and 1 otherwise.
- Changed: `settings_overrides` (claude / antigravity / etc.) now expresses merge intent with a Claude-compatible top-level `__mngr_merge` map (e.g. `{"permissions.allow" = "extend"}` / `"assign"`) instead of `__extend` / `__assign` key suffixes, which leaked into the generated `settings.json` as junk keys the external CLI did not recognize. Raw suffix keys in `settings_overrides` are now a hard error pointing to `__mngr_merge`. A bare key still assigns with the narrowing guard; the narrowing error prints the exact `__mngr_merge` patch to add (the full nested patch in a single error). mngr's own (non-`settings_overrides`) config is unchanged and still uses the suffixes.
- Changed: A settings *patch* field (a claude agent type's `settings_overrides`) now **accumulates** across config scopes (user < project < local) and `parent_type` inheritance instead of a higher/child scope replacing the whole value: non-overlapping keys from every scope survive and same-key `__extend`s combine, with the narrowing guard threaded recursively. Cross-scope narrowing is now surfaced at config-load.
- Changed: `mngr config assign <key> <value>` command (mirroring `mngr config extend`) writes a `key__assign` entry (replace without the narrowing guard) — or on a `settings_overrides` path, a `__mngr_merge` `assign` directive. `mngr config get` resolves the `__assign` form.
- Changed: `mngr config set` / `extend` / `assign` now render configuration errors through the central CLI error handler.
- Changed: `mngr config` is now exempt from the settings-narrowing guard, so a config that would otherwise narrow can still be loaded in order to *edit* it (`mngr config set` / `unset` previously failed with the narrowing error themselves — a catch-22).
- Changed: Hardened agent process-tree teardown so a long-lived daemon launched under an agent can no longer outlive the agent and collide with a relaunch. New `reap_agent_process_tree(agent)` helper kills the agent's tmux-session pane descendants and any `MNGR_AGENT_ID`-tagged orphans that reparented to PID 1. `start_agents` reaps any stale process tree for an agent id before launching, but only when that agent isn't already running.
- Changed: Bumped the offload version baked into the host Dockerfile from `0.9.7` to `0.9.9` to track the CI pin.

### Fixed

- Fixed: `mngr event --follow` no longer polls the persisted files of an offline (stopped-but-not-destroyed) agent every second. For the Docker provider each such read is a separate `docker exec` into the shared state container, so a handful of stopped agents could push the engine to ~90% CPU. Each per-source tail thread now parks while the target is offline and resumes when it returns to RUNNING; existing event-id deduplication ensures nothing is emitted twice. Affects every consumer of `mngr event --follow` (the CLI, `mngr forward`, and the minds desktop app).
- Fixed: `build_add_authorized_keys_command` (the shared SSH-key seeding helper used by every provider's host/container setup) is now idempotent — each entry is appended only if it is not already present (a `grep -qxF` guard).
- Fixed: Config-merge bug where a partial override of a nested sub-model field (`logging`, `retry`, or a provider's `security_group`) silently reverted other sub-fields to their defaults. For example, project scope `[logging] file_level = "ERROR"` plus local scope `[logging] console_level = "TRACE"` previously dropped `file_level` back to its default (and spuriously raised the narrowing error); now `file_level` carries through.
- Fixed: Latent crash in `MngrConfig.merge_with` (`AttributeError`) when the base config's `retry` / `logging` was `None` and the override set them.
- Fixed: `mngr plugin list` mislabeling opt-in plugins (e.g. `claude_subagent_proxy`) as `enabled=true` when they were actually blocked. `config.disabled_plugins` now faithfully includes opt-in plugins that are disabled by default, so every consumer of that field sees the correct effective disabled set.
- Fixed: `mngr transcript` failing with "Unknown agent type" for config-defined agent subtypes (a custom `[agent_types.X]` with `parent_type = "claude"`). The command now resolves the type through its parent chain.
- Fixed: An expected (`MngrError`) error raised during agent provisioning (e.g. a settings-narrowing `ConfigParseError`) now surfaces as a clean one-line error instead of "Unexpected error" with a full traceback. `provision_agent` runs inside a concurrency group; the `create` flow now unwraps a single expected error from that group's `ConcurrencyExceptionGroup` before it reaches the CLI.

### Removed

- Removed: `TagLimitExceededError` (it existed only to flag the EC2 50-tag ceiling for the AWS provider's offline tag mirror, which is now replaced by the S3 state bucket).

## [v0.2.17] - 2026-06-18

### Added

- Added: Code-derived agent capability registry. Capability marker mixins in `imbue.mngr.interfaces.agent` (`CliBackedAgentMixin`, `InteractiveAgentMixin`, `HasSessionAdoptionMixin`, `SupportsLiveOutputMixin`, `HasUnattendedModeMixin`, `HasPermissionPolicyMixin`, `HasVersionManagementMixin`, `HasSessionPreservationMixin`, `HasAutoInstallMixin`) declare each agent's capabilities, detected via `issubclass`, `waiting_reason` field generators, plugin hookimpls, or a sibling usage plugin. A generated doc (`libs/mngr/docs/concepts/agent_capabilities.md`) renders the matrix; a drift guard fails if it disagrees with the code. Regenerate with `just regenerate-agent-capabilities-doc`.
- Added: Auto-install as a base capability via `HasAutoInstallMixin` and the shared `ensure_cli_installed` helper — checks for the binary at provision time and installs it if missing (gated by consent locally, `is_remote_agent_installation_allowed` remotely). Antigravity, opencode, and codex gain auto-install they previously lacked. Install failures now raise `AgentInstallationError`.
- Added: Session adoption is now a first-class create capability. The CLI option is `--adopt` (with `--adopt-session` kept as an accepted alias), declared like every other create option in `cli/create.py`. The adopted session id(s)/path(s) ride a typed `adopt_session` field on `CreateAgentOptions`. All five adoption-capable plugins (claude, antigravity, codex, opencode, pi-coding) read it. Agent-agnostic validation (type must support adoption; mutually exclusive with cloning via `--from`) runs in `imbue.mngr.api.create` for every create path. `--adopt` may be combined with `--from`: every named conversation plus the clone's is made available, and the clone's is the one resumed.

### Changed

- Changed: `send_message` is no longer an abstract method on `AgentInterface`. The send-keys delivery moved onto a new `SendKeysAgent(InteractiveAgentMixin, BaseAgent)`, which `InteractiveTuiAgent` and the bare `command` runner extend; opencode/pi declare `InteractiveAgentMixin` directly (they deliver via their own server/extension APIs). A new `require_interactive_agent` helper narrows an agent to `InteractiveAgentMixin`. User-visible: `mngr message <headless-agent>` now fails with a clear "agent type ... does not accept interactive messages" error.
- Changed: Unified the TUI streaming-snapshot and headless incremental-output surfaces onto one `live_output` capability via `SupportsLiveOutputMixin`, which declares `get_live_output_path()` and `make_live_output_reader()`. The shared poll-read-extract tail loop lives in `imbue.mngr.agents.live_output_tail.tail_live_output()`; the new `imbue.mngr.interfaces.live_output` module holds the `LiveOutputReader` contract and a `RawTextReader` implementation. The former `HasStreamingSnapshotMixin` is removed. No user-visible behavior change.
- Changed: `HasVersionManagementMixin` is now a functional contract. The descriptive `get_version_policy()` label is replaced by `reconcile_installed_version(host, mngr_ctx)`, which a version-managing agent calls during provisioning — a pinning agent verifies the installed version and raises on mismatch; an update-policy agent runs its update check.
- Changed: `mngr rsync`, `mngr git push`/`pull`, and `mngr create --from` now skip host/agent discovery for a bare local path, and narrow discovery to the named provider when the address pins one (e.g. `@host.modal:/work`). Fully-qualified-address behavior is unchanged. New `imbue.mngr.api.find.resolve_host_location` helper consolidates the local-path shortcut and the discover-then-resolve flow.
- Changed: The plugin install wizard (`mngr extras plugins` / `mngr plugin install-wizard`) now recommends and pre-selects the AWS, GCP, Azure, and Lima provider plugins when their CLI is detected (`aws`, `gcloud`, `az`, `limactl`), mirroring how the Claude and Modal plugins are detected.
- Changed: Agent tmux sessions now apply mngr's host tmux config even when a tmux server is already running. Previously the config was passed only via `tmux -f <config> new-session`, which tmux honors solely when it starts a new server; any session on an already-running server silently inherited tmux defaults, dropping the widened `status-left-length` (so `[mngr-<agent>]` was clipped) and the `Ctrl-q`/`Ctrl-t` destroy/stop hotkeys. Sessions now run `tmux source-file <config>` right after `new-session`. The host config also enables `set-titles` so the agent's session name/pane title are forwarded to the outer terminal's tab (e.g. iTerm2) instead of falling back to `<profile>(tmux)`.
- Changed: mngr's generated `~/.mngr/tmux.conf` no longer sources the user's `~/.tmux.conf`, and the agent's tmux server is no longer started with `-f` pointing at the mngr config. tmux loads `~/.tmux.conf` itself when the server starts; mngr's config (sourced at agent creation) now contains only mngr's own settings. Re-sourcing `~/.tmux.conf` on every agent creation could re-run non-idempotent user config (e.g. `set -ag`, plugin `run-shell`) and corrupt the user's setup.
- Changed: Aligned the common-transcript schema with OpenTelemetry GenAI semantic conventions — the assistant record's `stop_reason` field is now `finish_reason`, and every assistant record now carries an ordered `parts[]` array (text and tool_call segments) preserving the intra-turn interleaving of text and tool calls. A `parts_ordered` flag marks whether the order is faithful (true for claude, pi-coding, opencode, codex; false for antigravity, whose native format does not record relative ordering). The flat `text` + `tool_calls` fields are kept as a baseline.

### Fixed

- Fixed: Sending a message to a resumed interactive TUI agent could time out. The TUI-ready wait now runs inside `send_message` (not only at first creation), so every send path — including the resume message and on-demand restart — waits for the TUI to finish rendering before pasting. This prevents keystrokes from being dropped into a session still replaying its restored transcript.

## [v0.2.16] - 2026-06-16

### Added

- Added: New `azure` provider backend wiring. `azure` joins the remote-backend list and the install-wizard plugin catalog; `mngr azure prepare` / `mngr azure cleanup` now take `--provider` (and the standard common options), so they read defaults from the selected `[providers.NAME]` block.
- Added: `mngr gc` gained a provider garbage-collection hook (`ProviderInstanceInterface.gc_provider_resources`, no-op by default). Reclaimed resources are reported in the gc summary (human / JSON / JSONL) under "Provider resources" and honor `--dry-run`. The Azure provider uses it to reap NIC / public-IP orphans from failed VM creates.
- Added: Shared agent-preservation wiring (`build_transcript_preserved_items`, `preserve_agent_state`, `preserve_host_agents_on_destroy`, `flag_gated_items`) so any plugin can mirror the claude preserve-on-destroy behavior with minimal code. The shared release lifecycle now asserts the agent's raw and common transcripts actually landed in `<local_host_dir>/preserved/<agent-name>--<agent-id>/` on destroy, so a swallowed preservation failure can no longer pass silently. Profiles can also declare `native_session_preserved_relpaths` so the lifecycle asserts the native resumable session store was preserved.
- Added: Plugin install wizard (`mngr plugin install-wizard`, `mngr extras -i`) recommends the base `imbue-mngr-usage` plugin in phase 1 and offers each per-agent usage provider (claude/codex/opencode/pi-coding) in phase 2 only when both its agent plugin and the base usage plugin are present. Driven by a new per-entry `gate` field on catalog entries (`SignalGate` or `RequiredPackagesGate`) that replaces the previous separate `signal` / `requires_packages` fields.

### Changed

- Changed: `mngr stop`, `mngr destroy`, `mngr cleanup`, and `mngr gc` now aggregate and classify cleanup failures. Each cleanup step is bounded and classified as benign ("already gone", exit 0) or real failure; real failures aggregate across steps/agents/hosts, each tagged with a cause category (timeout, processes-remain, local-state-remain, host-resource-remain, provider-inaccessible), and surface via cause-specific exit codes (`2`/`3`/`4`/`5`/`6`, most severe wins; `1` other, `0` for clean/benign). `Host.stop_agents`, `Host.destroy_agent`, and `ProviderInstance.destroy_host` raise `CleanupFailedGroup` when a resource is left behind. Structured output (`--format json`) reports a `failures` list and an `exit_code` field, replacing the old `errors` string list. See `specs/cleanup-error-aggregation.md`.
- Changed: `mngr git push`, `mngr git pull`, and `mngr rsync` now run the underlying `git` / `rsync` binary as a plain subprocess with the user's stdout/stderr (no redirection), so progress, errors, and pager output flow directly to the terminal; stdin is `/dev/null` so the binary cannot block waiting for input (credential / host-key prompts). The `_complete` JSONL terminating events and the trailing "Rsync complete" human line are gone; a non-zero exit raises `GitSyncError` / `MngrError`. `RsyncResult`, `imbue.mngr.utils.rsync_utils.parse_rsync_output`, and the whole `rsync_utils` module are removed.
- Changed: `mngr_common_transcript_flush` takes an optional lock-acquire timeout (seconds), exported as `MNGR_CONVERT_LOCK_TIMEOUT` to each synchronous converter pass — lets a latency-sensitive caller (e.g. a SIGTERM/SIGINT handler) cap how long the flush blocks on the convert lock. Default 30s preserves existing behavior.
- Changed: `ProviderUnavailableError` accepts an optional `user_help_text` override. The default still tells the user to start Docker / disable the provider, but cloud providers (whose "unavailable" cause is a credential / subscription problem) can pass curated guidance instead — so a cloud auth failure no longer advises "start Docker".
- Changed: Concurrent SSH keypair creation is now race-free. `load_or_create_ssh_keypair` serializes first-time creation behind an exclusive file lock, and `save_ssh_keypair` writes both key files atomically (temp file + `os.replace`, via the shared `atomic_write` helper) before applying their permissions. Fixes intermittent `ValueError: Not enough fields for public blob` aborting `mngr create` during parallel host-discovery fan-out (observed on OVH and Vultr release tests). Paramiko's bare key-probe `ValueError` is now surfaced as a structured `HostConnectionError` so best-effort discovery treats it as an ordinary per-host failure.

## [v0.2.15] - 2026-06-16

### Added

- Added: `mngr create --format json` (and `jsonl`) now also reports the created host's name, its agent SSH connection (`ssh_user` / `ssh_host` / `ssh_port` / `ssh_key_path`), and an `outer_ssh_port` when the provider exposes a separate outer/management sshd (e.g. an OVH-slice's box-forwarded VM-root port). Backed by a new default-`None` `HostInterface.get_outer_ssh_port` hook.
- Added: `mngr capture --window` (`-w`) option for capturing a non-primary tmux window in the agent's session, by index (`--window 1`) or name.
- Added: Shared `mngr_common_transcript_lib.sh` library, provisioned to every agent's `commands/` dir alongside `mngr_log.sh` and `mngr_transcript_lib.sh`. Centralizes the convert-lock mutex (serializes the converter's read-modify-write across the 5s daemon and on-demand `--single-pass` flushes) and the turn-end synchronous flush previously duplicated per agent plugin.
- Added: `VpsDockerProvider.record_outer_host_key` pins an outer (VPS-root) sshd host key in the provider's known_hosts, so a provider operating on a VPS it did not order itself (e.g. the imbue_cloud rebuild on a leased host) passes strict host-key checking.

### Changed

- Changed: Centralized the Claude Code CLI presence check — `mngr extras` status and the `is_claude_installed` test helper now both defer to `CLAUDE.is_available()` instead of re-implementing `shutil.which("claude")` inline. `extras.py` now imports the shared `SUBPROCESS_ERRORS` from `imbue.mngr.utils.deps` rather than defining its own copy.
- Changed: Regenerated the bundled CLI reference docs to include the new `mngr imbue_cloud admin server` command group (including `admin server pricing`).

## [v0.2.14] - 2026-06-15

### Changed

- Changed: User-facing CLI errors (`MngrError` and its subclasses) now render their `Error:` line in bold red on a color-capable terminal, matching the existing colored `ERROR:` prefix. Suppressed when stderr is not a TTY or `NO_COLOR` is set; exit semantics are unchanged.

### Fixed

- Fixed: `mngr clone <agent> <name> --provider local` no longer fails with "destination path ... already exists and is not an empty directory"; the remote-source to local-target path now performs a mirror-style `git fetch` into the existing bare repo instead of `git clone --mirror`.
- Fixed: `mngr clone` (and any agent creation) transferring a git repo between two remote hosts no longer fails with "Identity file ... not accessible" / "Host key verification failed"; the transfer now relays through a local bare mirror so source and target SSH credentials are used on the local orchestrator where those files exist.
- Fixed: `mngr create`'s "branch already checked out" error now suggests `--transfer=none` instead of the removed `--in-place` flag.

## [v0.2.13] - 2026-06-13

### Added

- Added: Cross-plugin file-preservation API letting plugins declare a single list of paths to preserve, executed uniformly against either an online host or a stopped host's volume.
- Added: SSH provider documentation page covering host config (`address`/`port`/`user`/`key_file`/`known_hosts_file`), the `NAME@HOST.PROVIDER` form for running an agent on a configured host, and the provider's limitations (no host creation/snapshots/tags).
- Added: `mngr usage --preserved` / `--no-preserved` flags documented on `mngr usage` and `mngr usage wait` in the regenerated CLI reference (behavior implemented in the `mngr_usage` plugin).
- Added: Agent-type aliases via a new `register_agent_aliases` plugin hook -- a short alternate name (e.g. `agy` for `antigravity`) is accepted anywhere its canonical type is and resolves to the same agent; the persisted agent records the canonical type. Aliases are tab-completable for `--type` but do not appear in `mngr plugin list --kind agent-type`. A custom `[agent_types.X]` block whose name matches a built-in alias takes precedence (the alias is dropped with a warning).
- Added: Disabled-plugin errors now name the real owning plugin -- mngr records which plugin registered each agent type, so the "plugin is disabled, enable it with `mngr plugin enable ...`" message points at the actual entry-point (e.g. the `pi_coding` plugin for the `pi-coding` type) instead of assuming the plugin name equals the agent-type name.
- Added: Tab completion for `-S` / `--setting` config overrides on every command. The KEY is drilled one dotted segment at a time (`mngr create -S head<TAB>` -> `headless=`); constrained value sets (booleans, enum log levels, provider/agent-type names) list values after `=`; `agent_types.*` keys complete for built-in/registered types too (`agent_types.claude.*`, `agent_types.codex.*`), with constrained fields completing their values. Works in zsh and bash; short value-taking options (`-S`, `-m`, `-b`, `-l`, `-n`, `-t`, `-o`, `-i`, `-s`, `-w`) are now recognized as consuming their value so a later positional argument isn't suppressed.
- Added: Shell completion installs as a small, stable shim in your rc that sources a mngr-managed completion file (`~/.mngr/completions/mngr.{zsh,bash}`); completion improvements apply automatically when you upgrade mngr -- no rc edit needed. The managed files refresh in the background (e.g. on `mngr list`) and via `mngr extras completion`. `mngr extras completion` recognizes an outdated install (rather than treating any existing completion as configured), installs the up-to-date shim, and removes an old self-contained completion block when it byte-for-byte matches a mngr-generated form (a hand-edited block is left untouched). After install, it prints the exact `source` command to activate completion in the current shell.
- Added: `mngr list --schema`, a machine- and human-readable catalog of every field referenceable in `--include`/`--exclude`, `--sort`, and `--fields`/`--format`. Each entry lists type, description, and contexts (`cel` for filters/sorts, `template` for `--fields`/`--format`). Derived live from `AgentDetails`/`HostDetails`, including nested fields like `host.resource.cpu.count` and dynamic patterns like `labels.$KEY`. Composes with `--format json`/`jsonl`. The `mngr list` help "Available Fields" section and the generated `docs/commands/primary/list.md` are now rendered from the same catalog.
- Added: `project` field is now usable in CEL filters and sorts (`--include 'project == "mngr"'`, `--sort project`), mirroring the existing `host.provider` alias and the `--project` flag; previously only `--fields`/`--format` templates supported it.
- Added: `ScalarTuple` marker type (and the `ScalarStrTuple` annotated alias) for tuple-typed settings semantically meant as a single scalar value -- a higher-precedence config layer that sets one replaces the whole value rather than tripping the settings-narrowing guard. `StringDerivedTuple` (string-shaped TOML values like `cli_args = "..."`) is now a specialization. Applied by `model_validate` (via the after-validator).
- Added: `bootstrap_backend_for_host_creation(provider_name, mngr_ctx)` public helper (formerly the private host-creation bootstrap in `mngr/api/create.py`), so other entry points (e.g. `mngr_tmr`'s snapshot path) can trigger the same one-time bootstrap before `get_provider_instance`. Paired with a new `resolve_backend_and_config(provider_name, mngr_ctx)` helper on the providers registry that replaces the "configured-instance vs. bare-backend-name fallback" duplication between `get_provider_instance` and the `mngr create` bootstrap path.
- Added: Reusable gitignore-status helpers in `mngr.api.git` -- `check_path_gitignore_status` (returns `SKIP` / `IGNORED` / `NOT_IGNORED`) and `check_path_repo_gitignore_status` (treats global-only ignores as `ONLY_GLOBAL`, for preflight checks whose result must hold on a remote host / fresh clone too). Both resolve symlinks along the path first so `git check-ignore` does not choke with "beyond a symbolic link".
- Added: Canonical schema for the agent-agnostic common-transcript envelope (`imbue.mngr.agents.common_transcript_records`) -- the single source of truth for `user_message` / `assistant_message` / `tool_result` records, with a conformance test asserting all five emitters (claude, antigravity, opencode, pi-coding, codex) validate against it. A meta-test discovers every registered common-transcript-emitting agent type and fails if any lacks a conformance test.
- Added: Shared agent release-lifecycle harness (`imbue.mngr.agents.agent_release_testing`) drives create → WAITING → message → transcript → stop/start resume → destroy with per-agent profiles, so each plugin's release test is a thin profile.
- Added: `mngr create --adopt-session <session-id>` now also resolves bare session IDs against every live local mngr agent's per-agent config dir and against preserved-session files of destroyed agents (in addition to the current and user-scope Claude config dirs). Docs regenerated.
- Added: `OPT_IN_PLUGINS` set in the config pre-reader for plugins that are disabled by default and must be explicitly enabled with `[plugins.<name>] enabled = true`. Inverts the normal default (plugins load unless disabled) for the listed plugins; `claude_subagent_proxy` is the first.
- Added: Typed `DockerRuntimeNotRegisteredError` (with `user_help_text` pointing at the fix) when the configured `docker_runtime` (e.g. `runsc`) is not registered with the Docker daemon, replacing Docker's raw exit-125 `ProcessError`. Because it is an `MngrError`, `mngr create --format jsonl` emits `error_class: "DockerRuntimeNotRegisteredError"`.

### Changed

- Changed: `ProviderBackendInterface` -- removed the Modal-specific `is_for_host_creation` flag (which every other backend was `del`-ing) and replaced it with a default-no-op `bootstrap_for_host_creation(name, config, mngr_ctx)` method that Modal overrides and `mngr create` invokes before `build_provider_instance`. The Docker backend's state-container preservation behavior (read-only commands like `mngr list`, `mngr gc`, `mngr cleanup`, and cross-provider discovery do not lazily create the singleton state container; only `mngr create` does) is carried into the new shape: Docker's `build_provider_instance` unconditionally raises `ProviderEmptyError` when the state container is missing, and the Docker backend's `bootstrap_for_host_creation` override creates it on the `mngr create` path. Behavior for existing Docker hosts is unchanged.
- Changed: `aws` added to the remote-backend list and `mngr` plugin catalog; `mngr create` CLI markdown docs regenerated to include the AWS provider's build-args help.
- Changed: `_is_transient_ssh_error` (in both `hosts/host.py` and `hosts/outer_host.py`) now treats Python's built-in `TimeoutError` as transient -- pyinfra's `read_output_buffers` raises a bare `TimeoutError` on per-command read timeout (e.g. when the remote sshd is reloaded mid-read during cloud-init bootstrap), so the retry loop now picks it up rather than letting it escape host creation. The streaming/file ops on `OuterHost` translate post-retry timeouts into structured `HostConnectionError` via a single `_translate_ssh_errors(...)` context manager that de-duplicates the per-op error-translation chain (`TimeoutError` / `OSError "Socket is closed"` / `EOFError,SSHException -> HostConnectionError`).
- Changed: Resizing the terminal while attached to a remote agent (AWS or any SSH-backed provider) now reflows correctly instead of showing a field of padding dots once the window grew past the attach-time size. The post-attach step now sends only `SIGWINCH` to pane processes; previously it ran `tmux resize-window -A`, which has a documented side effect of switching the window's `window-size` option to `manual`.
- Changed: Codex agent type moved out of `mngr` core into the external `imbue-mngr-codex` plugin (added to the plugin catalog as a recommended INDEPENDENT-tier); removed the in-core `codex_agent` stub and its direct registration.
- Changed: `pi-coding` plugin (`imbue-mngr-pi-coding`) marked recommended in the plugin catalog now that it has real lifecycle support, so `mngr extras` offers it by default alongside claude/opencode/antigravity.
- Changed: Regenerated `mngr schedule` CLI reference docs to include the new `--timezone` option on `schedule add` (added in the mngr-schedule plugin).
- Changed: Replaced direct built-in exception raises (ValueError/RuntimeError) in config key resolution, docker provider config validation, and agent discovery with dedicated custom exception types.
- Changed: `mngr gc --provider <name>` now exits non-zero when an explicitly-named provider is unavailable; other selected providers still run to completion and the unavailable provider is reported in the summary. Empty providers (e.g. a fresh Modal per-user environment) remain silently skipped, and the automatic post-destroy gc path (which uses `--all-providers` and tolerates skips) is unaffected.
- Changed: Event journals are now read through a unified host file-read interface instead of shelling out `find`/`cat` over SSH; host reads are byte-exact, removing the prior trailing-newline workaround.
- Changed: Sending a message to a Claude agent now confirms submission as soon as it's accepted into the agent's queue (watching a fresh `enqueue` event in the transcript log concurrently with the existing `tmux wait-for` hook signal, in a single remote command) and returns as soon as either lands. Previously the call always blocked on the `UserPromptSubmit` hook, which only fires when the prompt reaches the model, so `mngr message` (and any caller of the send path, e.g. an HTTP front-end) to a busy agent could block up to the full submission timeout and exceed a front-door proxy timeout even though the message was already queued. TUIs that don't supply an acceptance-marker command keep the original hook-only wait.
- Changed: Multi-provider discovery now fails loudly when a provider's backend is unreachable instead of silently skipping it -- commands that scan every provider (`mngr message`, `limit`, `snapshot`, `create`) propagate `ProviderUnavailableError` rather than quietly omitting agents on the down provider. Targeted commands (`mngr rsync`, `mngr git push`/`pull`, `mngr event <host>` / `@host[.provider]`) scope discovery to only the provider(s) that could hold the target (via the `.PROVIDER` qualifier and/or the agent name), so an unrelated down provider can't fail them.

### Removed

- Removed: `-a` / `--all` / `--all-agents` flag on `mngr message` (alias `mngr msg`). Use the explicit `mngr list --ids | mngr msg -` pattern (optionally with `--include` / `--exclude` to scope the broadcast); the tutorial and CLI examples have been updated.
- Removed: `--provider` flag on `mngr message` (alias `msg`); `--agent` / `--host` flags on `mngr snapshot create` / `list` / `destroy`. `mngr msg` now derives the providers to query from the agent addresses themselves (union of named providers, or full scan if any address omits its provider) -- previously `mngr msg agent@host.provider_a --provider provider_b` queried the wrong provider. `mngr snapshot` takes agent and host targets as a single positional list (`agent`, `agent@host[.provider]`, `@host[.provider]`, or bare `host-...`).

### Fixed

- Fixed: Environment-variable forwarding for remote streaming SSH commands (`OuterHost.execute_streaming_command(env=...)`) now uses `export KEY=VAL && command` instead of a bare `KEY=VAL command` prefix, so vars persist across compound commands (`install && tool ...`) -- this is what made remote `depot build` fail with "missing API token" even though `DEPOT_TOKEN` was supplied via `env`. Extracted the prefixing into a pure `_prepend_env_exports` helper.
- Fixed: Provider-config parsing now uses `model_validate` instead of `model_construct`, so TOML scalars are coerced to typed fields -- e.g. `builder = "DEPOT"` becomes the `DockerBuilder.DEPOT` enum (identity checks work, fixing a silent fallback to the non-depot path) and tuple-shaped fields are coerced from lists. Eliminates pydantic-serializer warnings on `model_dump`. Subsumes the dedicated `model_construct` coercion helper for nested-model provider fields (e.g. SSH `hosts` tables to `SSHHostConfig`).
- Fixed: SSH provider was silently disabling strict host-key checking for statically-configured hosts that set both `key_file` and `known_hosts_file`; `known_hosts_file` is now preserved.
- Fixed: Statically-configured SSH hosts under `[providers.<pool>.hosts.<host>]` previously crashed every host-enumerating command (`mngr list`, `mngr connect`, `mngr create <agent>@<host>.<pool>`, ...); they now load and resolve correctly, with a malformed host entry producing a clear config error instead of a late crash.
- Fixed: `mngr create --template` `setting` / `setting__extend` entries (e.g. `setting__extend = ["providers.docker.docker_runtime=runsc"]`) are no longer silently dropped. A template `setting` that targets `commands.*` or `create_templates.*` now raises a clear error (those sections are resolved before template settings are applied, so the value could never take effect) instead of being silently ignored; direct CLI `-S` still wins over a template-provided setting for the same key.
- Fixed: `mngr config get` and `mngr config list --all` now surface provider-subclass fields (e.g. `docker_runtime` on a docker provider) instead of reporting "Key not found".
- Fixed: `ProviderInstanceConfig.merge_with` docstring updated — the stale `is_host_in_docker` example was replaced with `is_run_as_root` after the Lima provider's docker-in-VM mode was removed.
- Fixed: `mngr create --reuse` now scopes the existing-agent lookup to the host named in the agent address (e.g. `babatest` in `system-services@babatest.docker`), not just the provider. Previously a create targeting a new host raised "Multiple agents found with name '<name>'. Use address syntax ..." as soon as two or more same-named agents were discoverable, even though the address already specified the host; this broke callers that deliberately share one agent name across many hosts (e.g. the minds desktop client). Genuinely ambiguous reuse (a shared name with no host in the address) still raises the disambiguation error.
- Fixed: `find_git_worktree_root`, `is_git_repository`, and `find_git_common_dir` no longer silently swallow unexpected `git` failures. Previously any `ProcessError` (non-zero exit, timeout, or failure to spawn the subprocess) was turned into a "not in a git repository" answer, so a transient or environmental git problem would silently drop the project-scope config layer (e.g. disabled plugins would not be blocked) or otherwise misreport repository state; only git's own "not a git repository" result now maps to that sentinel, and the detection calls force a C locale so a localized git cannot defeat the check.
- Fixed: Regenerated CLI reference docs — `file` / `tmr` See-Also sections link to `mngr rsync` instead of the removed `push` / `pull` commands (fixing broken `[mngr help push](mngr help push)` / `[mngr help pull](mngr help pull)` markdown links), the `mngr kanpan` doc describes the new `--format json` / `--format jsonl` output, and the `mngr forward` doc reflects the dynamic-port behavior. `scripts/make_cli_docs.py --check` now fails on any See-Also reference that resolves to neither a known command nor a help topic.
- Fixed: Removed a stale duplicate `type = "claude"` line in the e2e fixture's seeded `settings.local.toml` that was causing every release-tier e2e/tutorial test to fail with "Cannot overwrite a value".
- Fixed: Docker GC could wipe per-host data when the Docker daemon was briefly unavailable. The Docker provider's `discover_hosts` now raises `ProviderUnavailableError` on a connection/timeout error instead of returning `[]`, so a briefly unreachable daemon can no longer be mistaken for "zero hosts" and trigger GC to delete every still-live volume; GC skips unavailable providers at its own boundary.
- Fixed: DESCRIPTION (and other prose) sections of `mngr <command> --help` now indent to the man-page depth of seven spaces in interactive terminals (pager/rich path); previously they rendered flush-left, unlike the piped/plain output.

## [v0.2.12] - 2026-06-08

### Added

- Added: Public `set_command(command)` setter on `AgentInterface`, letting callers update the command an agent re-runs on its next start/restart.
- Added: Per-agent tmux window sizing and resize policy. `mngr create` accepts `--tmux-width`, `--tmux-height`, and `--tmux-window-size` (`manual|latest|largest|smallest`). Options are persisted on the agent and applied on every (re)start (provider-agnostic); defaults are unchanged (200x50, tmux default resize-on-attach). `mngr connect` skips its post-attach resize for a `manual`-window agent so pinned dimensions survive an interactive attach.
- Added: `mngr extras claude-plugin` now offers both `imbue-code-guardian` and `imbue-mngr-skills` (the `message-agent`, `wait-for-agent`, `find-agent`, and `mngr-help` skills, published from `imbue-ai/mngr-claude-skills`). Interactive runs show a checkbox picker of not-yet-installed plugins; `-y` auto-installs every not-yet-installed plugin. `mngr extras` status output now reports each plugin's installed/not-installed state individually.
- Added: New `docker_runtime` option on the docker provider config — when set (e.g. `docker_runtime = "runsc"`), mngr passes `--runtime=<value>` to `docker run`, letting hosts run agent containers under an alternative runtime such as gVisor. Defaults to unset (Docker's default). Override per-environment with `MNGR__PROVIDERS__<NAME>__DOCKER_RUNTIME`.
- Added: Shell tab-completion for the positional arguments of `mngr plugin add` (suggests installable plugin package names from the catalog) and `mngr plugin remove` (suggests currently-installed plugin packages from the uv-tool receipt). Both support prefix filtering and repeat completion for each package when operating on several at once.
- Added: `--dry-run` flag on `mngr destroy`, `mngr stop`, `mngr start`, and `mngr snapshot destroy`. Each reports which agents (or hosts, for `mngr stop --stop-host`) or snapshots would be acted on — honoring the same `--format`, JSON, and JSONL output options as the real command — and exits without touching anything. Matches the existing `--dry-run` on `mngr archive`, `mngr cleanup`, and `mngr gc`, and makes the documented stdin examples (e.g. `mngr list --ids | mngr stop - --dry-run`) work.
- Added: `mngr message -a`/`--all` broadcasts a message to every agent (alias `mngr msg -a -m "..."`). Previously documented in the tutorial but rejected by the CLI with "No such option: -a"; `--all` cannot be combined with explicit agent names.
- Added: `mngr connect` honors a custom `connect_command` (via the new `--connect-command` flag or the `connect_command` config), running it instead of the builtin tmux attach — matching how `mngr create` and `mngr start` already behave. A re-entrancy guard (`MNGR_CONNECT_COMMAND_ACTIVE`) prevents infinite recursion.
- Added: `mngr list --fields` accepts `project` as a short alias for `labels.project` (mirroring the existing `--project` filter flag and the `host.provider` field alias). Previously `--fields "name,project,state"` rendered an empty PROJECT column.

### Changed

- Changed: Regenerated `docs/commands/secondary/robinhood.md` CLI help doc to document the new `--include-partial-messages` and `--stream-plain-text` streaming flags (implemented in `imbue-mngr-robinhood`).
- Changed: Regenerated CLI reference docs to include the new `mngr imbue_cloud admin pool create --no-recycle` flag and the new `mngr connect --connect-command`, `mngr destroy --dry-run`, and updates to `start`/`stop`/`message`/`snapshot`.
- Changed: `mngr create --new-host` now tears down a freshly-created host on *any* failure up to and including the initial-message send, closing a gap where failures could leak the host. The existing `MNGR_DEBUG_RETAIN_LOCK_FOR_FAILED_HOSTS_DURING_CREATE=1` escape hatch still retains the failed host for debugging.
- Changed: A single source of truth now controls which packages are deliberately not published to PyPI, consulted by both the install wizard and the release tooling.
- Changed: Shared mngr plugin test fixtures are now single-sourced rather than duplicated across plugins (internal; no user-visible change).
- Changed: Tutorial-tied e2e tests moved into a `tutorial/` subdirectory (internal; no user-visible change).

### Fixed

- Fixed: Shell-quoting bug in agent launch where extra agent args passed after `--` on `mngr create` were spliced into the launch command unquoted, so a value like `--model "Gemini 3.5 Flash (Medium)"` failed the shell. Each arg is now properly quoted, fixing every agent type including `antigravity`/`agy`.
- Fixed: Merging provider config layers now only overrides the fields the higher-precedence layer actually set. Previously a higher layer touching the provider block at all would silently reset other fields to defaults; concretely, applying `providers.lima.is_enabled=true` reset the Lima provider into direct-in-VM mode instead of docker-in-VM mode.
- Fixed: `mngr create --provider modal` (and any other backend with one-time per-user bootstrap) no longer fails against a brand-new Modal environment with "Provider 'modal' has no state yet". The teardown-guard provider resolution had been read-only; it now uses `is_for_host_creation=True`, allowing the per-user Modal environment to be created on first use. Also fixes the same regression for `mngr create NAME@.modal` and the snapshot path.
- Fixed: `mngr gc --provider <name>` against an explicitly selected provider that reports no state yet (e.g. a fresh Modal per-user environment) now skips that provider instead of failing the whole command, matching `mngr list --provider modal`.

## [v0.2.11] - 2026-06-05

### Added

- Added: New `mngr stop --stop-host` flag that stops an agent's whole host (every agent on it) instead of just the named agent. Rejected on providers that don't support stopping hosts; cannot be combined with `--archive`. Idempotent on already-offline hosts, and works even when the host's container is running but sshd is unreachable. Multiple targets are stopped concurrently.
- Added: Node.js installed in the shared mngr image, pinned to minds' required Node version. It's a runtime dependency of the latchkey gateway and some minds tests, which now run on offload instead of being skipped.
- Added: `get_local_host(mngr_ctx)` API as the canonical way to obtain the local host, consolidating previously duplicated plugin helpers.
- Added: Bulk file upload that transfers many files to a host in a single rsync instead of one SSH round-trip per file, used by agent provisioning so file transfer scales (github issue 1825).
- Added: A discovery-event tailing helper that emits the latest cached snapshot then follows the event log without polling providers, backing `mngr forward --observe-via-file`.
- Added: Shared helper for resolving the per-agent source-repo trust path, replacing duplicated logic in the `antigravity` and `claude` plugins.
- Added: One-round-trip host helpers for symlinking or copying a path on the host, centralizing the symlink-vs-copy credential/cache pattern.

### Changed

- Changed: **Breaking** — `mngr dependencies` flags reworked. The old `-c`/`-a`/`-i` flags are removed; "which dependencies count" and "whether to install" are now two orthogonal options: `--scope core|all` (default `all`) controls which dependencies determine the exit code, and `--install none|interactive|auto` (default `none`) controls install behavior. Old `-c` → `--scope core --install auto`, old `-a` → `--install auto`, old `-i` → `--install interactive`.
- Changed: `ssh` is reclassified from a core to an optional dependency; core dependencies are now `git`, `tmux`, and `jq`. mngr's remote-host connectivity runs through paramiko (pure-Python), so the `ssh` binary is only needed for `mngr connect` to a remote agent, `mngr git push`/`pull` and `mngr rsync`, and the git-mirror/rsync source transfer when creating a remote agent. Those paths now raise a clear `BinaryNotInstalledError` if `ssh` is missing instead of an opaque "ssh: command not found".
- Changed: **Breaking** — discovery snapshots are now authoritative only for providers that succeeded on a given poll. Agents/hosts whose provider failed are retained from prior state (surfaced as unknown/stale) rather than dropped, and are only removed on an explicit destroy event or a subsequent successful poll that omits them. No discovery-event schema change.
- Changed: `mngr create --format jsonl` (and every other command) now emits a structured error record when a command fails, e.g. `{"event": "error", "error_class": "FastPathUnavailableError", "message": "..."}`. The top-level CLI exception handler calls `emit_error_event(...)` for real errors (not Ctrl-C / `--help`) when the resolved output format is JSONL, attaching the exception's class name. `on_error` likewise includes `error_class` in its JSONL error event when given the exception.
- Changed: `mngr create --new-host` now tears down the host it just created if a later step fails (provisioning, agent start, etc.), so a failed create never leaks the host. Previously the only cleanup was removing the host lock, which never helped providers that disable idle shutdown. The teardown is gated by the existing `MNGR_DEBUG_RETAIN_LOCK_FOR_FAILED_HOSTS_DURING_CREATE=1`, which now retains the failed host (not just its lock) for debugging.
- Changed: **Breaking** — every mngr error now inherits from `MngrError`, completing the consolidation of the error hierarchy under a single user-facing parent class. Every mngr error is now a `ClickException`, so when one reaches the CLI it renders as a clean `Error: ...` message instead of a Python traceback.
- Changed: Internal JSON-line output helper refactor (no user-visible change).
- Changed: Tutorial-tied e2e tests moved into a `tutorial/` subdirectory (internal; no user-visible change).
- Changed: The `mngr observe` discovery event log always lives under the default host dir, and passing `--events-dir` together with `--discovery-only` is now a usage error rather than being silently ignored. `--events-dir` still relocates the full observer's agent-state events.

### Removed

- Removed: `BaseMngrError` base class has been removed entirely. `MngrError` now inherits directly from `click.ClickException`, and every mngr error inherits from `MngrError`. There is no longer a separate non-user-facing error tier: all mngr errors render as a clean `Error: ...` message at the CLI rather than a traceback. This is a no-op for users — prior commits had already moved every error class under `MngrError`; removing `BaseMngrError` finalizes the consolidation.

### Fixed

- Fixed: `mngr list --format json` no longer crashes on `ProviderErrorInfo` when no agents were returned. With `--on-error continue` and a per-provider failure, the empty-agents path passed raw `ErrorInfo` pydantic models to `json.dumps`, which crashed. The empty-agents path now goes through the same `_emit_json_output` serializer as the non-empty path, so `mngr list --on-error continue --format json` produces a clean `{"agents": [], "errors": [...]}` payload.
- Fixed: Indefinite hang in git-push / rsync over SSH on macOS hosts where `SSH_AUTH_SOCK` routes to 1Password's biometric SSH agent. The shared `build_ssh_transport_command` (used by git push and rsync) now pins authentication to the explicit `-i` key via `-o IdentitiesOnly=yes -o IdentityAgent=none`. Without these flags, OpenSSH consults `SSH_AUTH_SOCK` first; in BatchMode the biometric prompt can never fire and ssh blocks forever on the agent reply.
- Fixed: `mngr rsync` (and any other command resolving a host-location address) now resolves a *relative* `:PATH` on an agent endpoint against that agent's workdir rather than the ambient working directory of the process running mngr. Previously `mngr rsync ./src/ my-agent:runtime/foo` passed the bare `runtime/foo` straight to rsync, which on a local-provider host silently targeted the *caller's* checkout. Absolute `:PATH` is still honored verbatim, and the by-name-only form (no `:PATH`) still resolves to the workdir itself.
- Fixed: Deterministic `mngr create` regression on Modal (`AgentNotFoundOnHostError` immediately after provisioning), caused by rsync replacing the host dir's symlink-into-volume with a real directory and stranding volume-backed state. Bulk uploads now preserve the symlink.

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
- Changed: Help-topic models relocated so plugins can reference them without importing the CLI; built-in topics are now registered through the same `register_help_topics` hook from an explicit registry (internal).
- Changed: Docker provider untags the per-host build image (`mngr-build-<host_id>`) on `destroy_host` (and again, defensively, on `delete_host`) so built images no longer pile up in `docker images`. Snapshot images keep their own layers.
- Changed: `mngr destroy` now actually destroys a host when its last agent is destroyed, regardless of `min_online_host_age_seconds`, releasing cloud-side resources immediately rather than waiting for the grace period.
- Changed: SSH host setup now creates the symlink target directory before symlinking `host_dir`, supporting the docker_vps unified-volume layout (internal).
- Changed: Regenerated the CLI reference docs to include the new `mngr imbue_cloud bucket` command group.
- Changed: Installed `restic` in the mngr Docker image (`libs/mngr/imbue/mngr/resources/Dockerfile`) so the offload test image exercises a real local restic repository, matching the minds app's new runtime dependency.

### Fixed

- Fixed: Host discovery now tolerates per-host SSH failures, falling back to an offline view for the broken host so the rest of the provider's hosts and agents still come through. Previously a single unreachable host blanked the entire provider's discovery, 503'ing every workspace and tripping minds' recovery page for healthy workspaces.
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
- Changed: Restored Modal compatibility for the standard mngr Dockerfile by moving source-dependent setup out of the image build (internal).
- Changed: `mngr create` no longer hard-codes `claude` as the default agent type — it must come from a positional argument, `--type`, or `[commands.create] type` in user settings; the error lists registered types and points at `mngr config set`.
- Changed: `scripts/install.sh` no longer carries custom shell logic for the default agent type — that prompt now runs inside `mngr extras -i` and is re-runnable via `mngr extras config`.
- Changed: Bumped pinned Claude Code CLI version from `2.1.116` to `2.1.141` in the Dockerfile.
- Changed: Renamed the mngr-side "workspace server" feature to "system interface"; HTTP endpoint `/api/agents/{id}/restart-workspace-server` → `/restart-system-interface` and SSE event `workspace_server_status` → `system_interface_status`.
- Changed: Renamed address-side `HostedLocation` to `HostLocationAddress` (internal symbol rename).
- Changed: Unified agent-address resolution across single-agent subcommands. `push` / `pull` / `provision` no longer require the agent to be running, and `push` / `pull` / `provision` / `rename` gain a `--start/--no-start` flag.
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
- Removed: BREAKING — `mngr push` and `mngr pull` are gone, replaced by three thin primitives that each wrap a single operation: `mngr rsync SOURCE DESTINATION`, `mngr git push TARGET [-- GIT_ARGS...]`, and `mngr git pull SOURCE [-- GIT_ARGS...]`. The old mngr-side branch/mirror/dry-run flags are gone; use the corresponding git flags directly. No compatibility shim.
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

- Changed: Address parsing refactored to typed values parsed once at the CLI boundary and threaded through the API layer; host names no longer permit dots (internal, with that one user-visible behavior change).
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
- Added: `mngr create -vv` emits a `Transferring agent files` log span around the agent file transfer.
- Added: New `MalformedJsonlLineError` (`imbue.mngr.errors`).

### Changed

- Changed: Default `docker build` timeout bumped from 5 to 10 minutes; configurable per provider via `build_timeout_seconds`.
- Changed: JSONL parsers now raise on malformed input instead of silently returning `None`.
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
