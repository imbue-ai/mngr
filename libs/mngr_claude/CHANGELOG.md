# Changelog - mngr_claude

A concise, human-friendly summary of changes for the `mngr_claude` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `update_policy` field on the claude agent type (`AUTO` / `ASK` / `NEVER`, default `NEVER`) governs Claude Code's background auto-updater via `DISABLE_AUTOUPDATER=1`. **Behavior change:** local claude agents now disable the auto-updater by default; previously the per-agent config inherited your `~/.claude.json` `autoUpdates` value. Set `update_policy = "AUTO"` to opt back in. Ignored in shared (`isolate_local_config_dir = false`) mode.
- Added: `version` field pins a specific Claude Code version.
- Added: When creating a local claude agent on macOS, if mngr detects you authenticate Claude Code with a claude.ai subscription (OAuth credentials) and config-dir isolation is enabled, it warns that the isolated agent's credentials will go stale and prints the exact command to disable isolation.

### Changed

- Changed: Renamed the claude agent type's `use_env_config_dir` option to `isolate_local_config_dir` and **flipped its meaning** — it now defaults to `true` (each local agent gets its own per-agent Claude config dir, so mngr never touches your default config). Set `isolate_local_config_dir = false` to share the user's `$CLAUDE_CONFIG_DIR`. The old key is still accepted as deprecated (emits a deprecation warning); setting both to contradictory values is an error. Only affects local agents; remote agents always use an isolated config dir.
- Changed: mngr's hooks now bake into the per-agent config-dir `settings.json` (`$MNGR_AGENT_STATE_DIR/plugin/claude/anthropic/settings.json`, the "user" settings layer Claude reads from `$CLAUDE_CONFIG_DIR`) instead of the project's `.claude/settings.local.json`. This keeps mngr-injected hooks out of the "normal" Claude config that plain (non-mngr) `claude` reads, fixing `mkdir: cannot create directory '/events': Permission denied`-style errors from hooks firing outside mngr. Note: existing leaked hooks in `settings.local.json` must be cleaned up manually.
- Changed: `settings_overrides` is now applied as a **config-consistent patch** folded onto the home `settings.json` (replacing the previous deep-merge-by-default). A bare key assigns with a recursive narrowing guard; merge intent (extend vs replace-without-guard) is declared in a Claude-compatible top-level `__mngr_merge` map (e.g. `{"permissions.allow" = "extend"}` / `"assign"`) that vanilla Claude ignores. Raw `__extend` / `__assign` leaf suffixes are rejected (they would have leaked into `settings.json` as junk keys); the narrowing error prints the exact `__mngr_merge` patch to add (the full nested patch: `extend` for a dict that would drop a sibling key, `assign` for a replaced list/value). A `__mngr_merge` key in the synced home settings base is stripped.
- Changed: `settings_overrides` accumulates across config scopes (user < project < local) and `parent_type` inheritance per-key, instead of a higher/child scope replacing the entire lower/parent value. Cross-scope narrowing surfaces through the standard flag-gated error.
- Changed: Shared Claude config mode (`isolate_local_config_dir = false`) now dismisses the cosmetic startup dialogs (trust, onboarding, effort callout, cost threshold) directly in your default Claude config and honors `auto_dismiss_dialogs`, so they no longer intercept automated input. Previously shared mode left the config untouched, so a fresh `~/.claude.json` re-triggered the trust/onboarding screens on every agent. mngr never accepts bypass-permissions mode via the global config.
- Changed: `mngr create` no longer requires `.claude/settings.local.json` to be gitignored across the board (now enforced only by the `claude_subagent_proxy` plugin when it actually rewrites the file).
- Changed: A user-supplied `--settings` in `cli_args` / `agent_args` now passes through to `claude` verbatim. In `use_env_config_dir` mode (which uses a private managed `--settings` file) a user-supplied `--settings` is now rejected at provision with a `UserInputError`.

### Fixed

- Fixed: In shared mode (`isolate_local_config_dir = false`), `CLAUDE_CONFIG_DIR` is now propagated only when your shell already exported it; left unset otherwise so claude resolves its real `~/.claude.json` (onboarding state, theme, trust, history). A prior fix had forced it to `~/.claude`, which pointed claude at an inner stub file lacking the onboarding state — so every new shared-mode agent re-showed the theme/onboarding screen.
- Fixed: In shared mode, mngr's hooks are now installed when shared mode is set via the current `isolate_local_config_dir = false` flag (previously they were only installed via the deprecated `use_env_config_dir = true` alias).

## [v0.2.17] - 2026-06-18

### Added

- Added: `ClaudeAgent` declares the new capability mixins (`HasStreamingSnapshotMixin` → `SupportsLiveOutputMixin`, `HasUnattendedModeMixin`, `HasVersionManagementMixin`, `CliBackedAgentMixin`, `HasSessionAdoptionMixin`), so these capabilities are code-detectable in the agent capability matrix. Session-adoption logic moved from `on_after_provisioning` into an `adopt_session` method.
- Added: `mngr create --yes` now dismisses claude's first-run dialogs (onboarding, effort callout, work-dir trust) in the per-agent config. Previously these were only auto-dismissed for remote/unattended agents or explicit `auto_dismiss_dialogs`. `--yes` deliberately does not accept bypass-permissions mode; tool auto-allow stays governed by `auto_allow_permissions`/unattended.

### Changed

- Changed: Split `ClaudeAgent` into a shared `ClaudeCoreAgent` base and an interactive `ClaudeAgent(ClaudeCoreAgent, InteractiveTuiAgent, ...)` subclass. `HeadlessClaude` now extends `ClaudeCoreAgent` directly, so the headless variant no longer structurally inherits interactive-only capabilities. User-visible: `--adopt-session` is now rejected for `headless_claude` with a clear error, instead of being silently accepted and never resumed (headless runs `claude --print`, not `--resume`).
- Changed: The session-adoption CLI option moved out of the claude plugin into core. The option is now `--adopt` (with `--adopt-session` kept as an accepted alias) and is shared by every interactive agent. Claude reads the adopted session ids from the first-class `CreateAgentOptions.adopt_session` field.
- Changed: `ClaudeCoreAgent` now installs claude through the shared `ensure_cli_installed` helper (consent-gated locally, config-gated remotely; pinned to the configured version), then calls `reconcile_installed_version` to verify the present binary matches any pinned `version`. Install / version-mismatch failures now raise `AgentInstallationError` rather than `PluginMngrError`.
- Changed: Adapted to the unified live-output contract. `ClaudeAgent` inherits `SupportsLiveOutputMixin` directly, exposes its streaming snapshot via `get_live_output_path()`, and supplies a `SnapshotDeltaReader` from `make_live_output_reader()`. The snapshot parsing/diffing (`compute_stream_delta` and friends, previously in `mngr_robinhood`) moves into the new `imbue.mngr_claude.stream_buffer` module. `HeadlessClaude` keeps streaming `claude --print` stream-json via the shared tail loop in mngr; the agent supplies a `StreamJsonReader`. No user-visible behavior change.
- Changed: The claude common-transcript converter now emits `finish_reason` instead of `stop_reason` (aligning with the OpenTelemetry GenAI vocabulary) and an ordered `parts[]` array on assistant records preserving the source interleaving of text and tool-use blocks.

### Fixed

- Fixed: Resuming a Claude agent and immediately sending a message no longer drops keystrokes into a still-replaying transcript. The TUI-ready indicator is now the input-prompt glyph (`❯`) instead of the "Claude Code" welcome banner (which only renders on a fresh start, not on resume).
- Fixed: `--adopt A --from X` (combined explicit-adopt plus clone) no longer refuses on a whole-directory clobber. The explicit session is copied into the destination's encoded project dir first, and the clone's rekey merges the source-encoded subdir's files into it non-destructively; a genuine per-file collision still raises `AgentStartError`.
- Fixed: A `--from` clone whose source has no resumable session now warns and adopts nothing (rather than raising) — `--from` is a workspace clone, so carrying the source's conversation forward is a bonus. Explicit `--adopt` failures and the per-file merge collision remain hard errors.

## [v0.2.16] - 2026-06-16

### Changed

- Changed: Common-transcript converter's event-conversion logic moved out of the inline `python3` heredoc in `common_transcript.sh` into a standalone `common_transcript_convert.py` (provisioned alongside the shell script), so it is type-checked, linted, and unit-tested directly. Malformed raw-transcript lines, unreadable existing-output lines, and transcript lines whose `message` is `null` are dropped silently rather than aborting the conversion run.
- Changed: Common-transcript watcher no longer echoes converter errors to the agent's pane — a genuine conversion error is recorded in the structured log only.
- Changed: Session-preservation-on-destroy now uses the shared `preserve_agent_state` / `preserve_host_agents_on_destroy` helpers in mngr core instead of an inline copy. The preserved file set, the `preserve_sessions_on_destroy` config option, and the online/offline behavior are unchanged. The offline host-destroy path now also filters discovered agents by agent type.

### Fixed

- Fixed: Synchronous transcript flush at turn end now runs on every turn-end path — not just `wait_for_stop_hook.sh`'s `run_post_completion`, which is skipped on the no-`/proc` fast path (macOS / local agents) and on the SIGTERM/SIGINT handler. The flush moved into `mark_inactive`, which every path calls before clearing the `active` marker, so a WAITING-signal consumer can no longer outrun the converter on those paths. The flush's lock-acquire wait is bounded per call (2s for SIGTERM/SIGINT, 30s for normal turn-end) via the portable `MNGR_CONVERT_LOCK_TIMEOUT` rather than `timeout(1)` (macOS lacks it).

## [v0.2.15] - 2026-06-16

### Changed

- Changed: `wait_for_stop_hook.sh` now flushes the transcript pipeline (synchronous `--single-pass` of the raw streamer and common-transcript converter) before clearing the `active` marker, so consumers reading the common transcript on a WAITING transition cannot outrun the converter. The flush and the convert lock come from the shared `mngr_common_transcript_lib.sh` rather than being duplicated per agent.
- Changed: `waiting_reason` field in `mngr list` is now gated on the agent's `active` (in-turn) marker — a stranded `permissions_waiting` file that outlived its turn reports `END_OF_TURN` instead of wrongly showing `PERMISSIONS`.

## [v0.2.14] - 2026-06-15

### Added

- Added: `imbue.mngr_claude.stream_json`, a shared typed boundary for the Claude partial-message stream-json envelope (defined against the `anthropic` SDK's `RawMessageStreamEvent` union and `anthropic.types.Message`). `mngr ask`'s headless reader now parses partial-message events through it; an event variant newer than the installed `anthropic` package degrades gracefully (skipped, or falls back to a lenient text scan) rather than dropping the response. Adds `anthropic` as a new dependency (unpinned; imported for its typed models only -- mngr still drives the `claude` CLI and makes no API calls).

### Changed

- Changed: `mngr create --adopt-session` now validates the session ID up front (before any host or worktree is created), so an unknown or ambiguous ID fails fast with a clean `Error:` message instead of crashing mid-provisioning with an "Unexpected error" traceback. The "session not found" message no longer enumerates every searched directory.

## [v0.2.13] - 2026-06-13

### Added

- Added: `claude_process_started` marker file (touched by the `SessionStart` hook) whose mtime gives consumers a restart boundary — any transcript event older than it belongs to a turn the current process did not run.
- Added: `mngr create --adopt-session <session-id>` now also resolves a bare session ID against every live local mngr agent's per-agent config dir and against preserved-session files of destroyed agents (previously only the current / user-scope Claude config dirs). `--adopt-session` is repeatable; every named session is made available, while only the last is resumed on startup. Only the local host dir is scanned.
- Added: Conformance test asserting claude's emitted common-transcript records validate against the canonical envelope schema (`imbue.mngr.agents.common_transcript_records`).

### Changed

- Changed: Claude session preservation rewritten onto core mngr's shared `preserve_agent_data` machinery, working against either an online host or a volume-backed offline host. Sessions, the raw and common transcripts, and the session-id history are still preserved before the agent state directory is deleted, but preserved files now mirror the agent state directory verbatim under `<local_host_dir>/preserved/<agent-name>--<agent-id>/` instead of the old `preserved_sessions` location — a switch-forward change (previously preserved sessions are left in place).
- Changed: `ClaudeAgent` now supplies its own "message accepted" probe (a shell command that reads the latest `enqueue` event from the Claude transcript event log and prints its ISO-8601 timestamp) to mngr's shared submission-confirm path. This is the Claude-specific knowledge that previously lived hardcoded in the shared `tui_utils` module; moving it into the plugin keeps `tui_utils` agent-neutral while preserving the fast-confirm-on-enqueue behavior for Claude agents.
- Changed: A skill-provisioned agent's primary skill (e.g. `code-guardian`, `fixme-fairy`) is now installed into the agent's per-agent config dir (`$CLAUDE_CONFIG_DIR/skills/<name>/SKILL.md`) instead of leaking through global `~/.claude/skills/` into every local agent. `skills/` is now synced via child-level symlinks (mirroring `plugins/`) so the per-agent skill can live as a real file alongside symlinked user skills. The local install is now silent (matching the always-silent remote install).
- Changed: `.claude/settings.local.json` gitignore preflight/provisioning check delegates to the shared `check_path_gitignore_status` helper in `mngr.api.git`. No user-visible behavior change.

### Fixed

- Fixed: A Claude agent restarted or resumed mid-turn no longer stays stuck at the `RUNNING` lifecycle state. The `active` marker (set on `UserPromptSubmit`, cleared by `Stop` / idle `Notification`) used to outlive a turn abandoned by an abnormal exit (container restart, OOM, crash); the `SessionStart` hook now clears the `active` / `permissions_waiting` markers on `startup`/`resume` (a fresh, not-mid-turn process), so the lifecycle state self-heals on the next (re)start. `compact` is excluded because auto-compaction fires mid-turn while Claude is genuinely active.
- Fixed: Provisioning a local Claude agent no longer creates self-referential symlink loops inside the user's shared `~/.claude/` (e.g. `~/.claude/skills/skills -> ~/.claude/skills`). `_sync_user_resources` used plain `ln -sf` as idempotent, but the second-and-later provision dereferenced the existing destination symlink and nested a new link inside the shared source; all sync symlinks now use `ln -sfn` (`--no-dereference`), which replaces the destination symlink instead of following it.

## [v0.2.12] - 2026-06-08

### Added

- Added: Approximate response streaming for Claude agents, driven by watching the agent's tmux pane. A new `streaming_snapshot_interval_seconds` (float, default `0.0`) on the `claude` agent type config enables a background watcher that writes the in-progress assistant text to `$MNGR_AGENT_STATE_DIR/plugin/claude/stream_buffer` every N seconds; `<= 0` (the default) leaves existing behavior unchanged. The buffer carries the id of the last complete assistant message plus the in-progress text reverse-mapped from the terminal rendering back into markdown, and is emptied when the agent goes idle.
- Added: `resources/stream_snapshot.py` watcher script (stdlib-only) that captures the tmux pane, identifies the latest assistant-text block, and reverse-maps markdown formatting (bold/italic, inline code, links, blockquotes, lists, code blocks, tables) from the rendered pane. Provisioning fails fast if streaming is enabled but the host lacks `python3`.
- Added: `ClaudeAgent.get_stream_buffer_path()` so other code (e.g. `mngr robinhood`) can locate and read the buffer.

## [v0.2.11] - 2026-06-05

### Fixed

- Fixed: Aligned the workspace's `imbue-mngr*==` pin stragglers in `pyproject.toml` with the satellites bumped in main's release commit. Previously the workspace constraint graph was unsatisfiable, which would have broken the `apps/minds` ToDesktop bundle build at `uv lock` time (day-to-day dev hides this because `[tool.uv.sources]` redirects every `imbue-mngr-*` to its workspace path, bypassing the `==` pin).

## [v0.2.10] - 2026-06-01

### Changed

- Changed: `on_before_create` hook implementation (used for `--adopt-session` validation) updated to accept the new `mngr_ctx` parameter now passed by mngr; simplified to require the agent type to be `claude` (no longer special-cases an unset type, since `CreateAgentOptions.agent_type` is now always set).

### Fixed

- Fixed: `--adopt-session` no longer rejects valid Claude agent subtypes. It now accepts any agent type that resolves to a Claude agent (including config-defined templates like `write-plus` whose `parent_type` chain reaches `claude`), instead of only the literal `claude` type name. The check routes through the centralized `resolve_agent_type` registry rather than a string comparison.

## [v0.2.9] - 2026-05-28

### Added

- Added: `use_env_config_dir` option on the `claude` agent type config so local Claude agents share `$CLAUDE_CONFIG_DIR` instead of provisioning a per-agent dir.

### Changed

- Changed: `ClaudeAgent` now implements the new `HasTranscriptMixin` / `HasCommonTranscriptMixin` mixins; user-visible behavior of `mngr transcript <claude-agent>` is unchanged.
- Changed: `resolve_shared_claude_config_dir()` falls back to `~/.claude/` when `$CLAUDE_CONFIG_DIR` is unset (matches Claude's own default) instead of raising; `mngr robinhood` no longer keeps `ORIGINAL_CLAUDE_CONFIG_DIR` in the agent env so credential sync reads from the live `$CLAUDE_CONFIG_DIR`.
- Changed: `ClaudeAgentConfig.merge_with` follows mngr's new assign-by-default semantics — an override's `cli_args` replaces (rather than concatenates) the base's. Use `cli_args__extend = [...]` for additive layering.
- Changed: Plugin uses the structured `TmuxWindowTarget` type for tmux pane targeting; `_send_enter_and_validate` and `_preflight_send_message` now take `tmux_target: TmuxWindowTarget` instead of a bare string.

### Fixed

- Fixed: Cloned claude agent now actually resumes the source agent's conversation — `_adopt_cloned_session` renames the project subdir to the destination's realpath-resolved encoding, drops the stale `sessions-index.json`, writes the real `claude_session_id`, and carries forward `claude_session_id_history`.
- Fixed: `claude_background_tasks.sh` now uses the `=` exact-match prefix in its `tmux has-session` polling loop so it no longer leaks the transcript streamer and common-transcript converter for stopped agents when a sibling-prefix session is still alive.

## [v0.2.7] - 2026-05-11

### Fixed

- Fixed: `claude plugin update` SessionStart hook no longer hangs Modal-launched agents at the `ssh` TOFU prompt — `scripts/claude_update_plugin.sh` now uses `GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes'`.
