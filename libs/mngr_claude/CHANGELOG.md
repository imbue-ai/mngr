# Changelog - mngr_claude

A concise, human-friendly summary of changes for the `mngr_claude` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

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
