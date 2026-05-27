# Changelog - mngr_claude

A concise, human-friendly summary of changes for the `mngr_claude` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `use_env_config_dir` option on the `claude` agent type config so local Claude agents share `$CLAUDE_CONFIG_DIR` instead of provisioning a per-agent dir.

### Changed

- Changed: `ClaudeAgent` now implements the new `HasTranscriptMixin` / `HasCommonTranscriptMixin` mixins; user-visible behavior of `mngr transcript <claude-agent>` is unchanged.
- Changed: `resolve_shared_claude_config_dir()` falls back to `~/.claude/` when `$CLAUDE_CONFIG_DIR` is unset (matches Claude's own default) instead of raising; `mngr uncapped-claude` no longer keeps `ORIGINAL_CLAUDE_CONFIG_DIR` in the agent env so credential sync reads from the live `$CLAUDE_CONFIG_DIR`.

### Fixed

- Fixed: Cloned claude agent now actually resumes the source agent's conversation — `_adopt_cloned_session` renames the project subdir to the destination's realpath-resolved encoding, drops the stale `sessions-index.json`, writes the real `claude_session_id`, and carries forward `claude_session_id_history`.

## [v0.2.7] - 2026-05-11

### Fixed

- Fixed: `claude plugin update` SessionStart hook no longer hangs Modal-launched agents at the `ssh` TOFU prompt — `scripts/claude_update_plugin.sh` now uses `GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes'`.
