# Changelog - mngr_antigravity

A concise, human-friendly summary of changes for the `mngr_antigravity` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.1.0] - 2026-05-28

### Added

- Added: `gemini` agent type plugin (`imbue-mngr-gemini`) wiring Google's Gemini CLI into mngr.
- Added: `gemini_config.py` foundation (read/write helpers for `~/.gemini/settings.json`, env-var interpolation, hook builders) plus a `SessionStart` readiness sentinel wired into `GeminiAgent.wait_for_ready_signal`.
- Added: Opt-in `auto_allow_permissions` flag on `GeminiAgentConfig` that installs a `BeforeTool` wildcard hook auto-approving every tool call.
- Added: Gemini agents now emit a common transcript readable by `mngr transcript`; raw gemini session JSONL is captured into `logs/gemini_transcript/events.jsonl`. Opt out with `emit_common_transcript = false`.
- Added: Renamed `mngr_gemini` to `mngr_antigravity`; agent type `gemini` is replaced by `antigravity`. The plugin now targets Google's Antigravity CLI (`agy`), with hook event names that mirror Claude's (`SessionStart`, `PreToolUse`, `PostToolUse`, `SessionEnd`, `Stop`, `Notification`) and `--dangerously-skip-permissions` as the auto-allow flag. Includes Claude-style first-launch trust-folder dismissal via `auto_dismiss_dialogs` (default `False`), and common-transcript scoping per-agent by grepping agy's own log file for `Created conversation <uuid>`.

### Changed

- Changed: `GeminiAgent` now implements the new `HasTranscriptMixin` / `HasCommonTranscriptMixin` mixins.
- Changed: `AntigravityAgentConfig.merge_with` follows mngr's new assign-by-default semantics — an override's `cli_args` replaces (rather than concatenates) the base's. Use `cli_args__extend = [...]` for additive layering.
- Changed: Plugin uses the structured `TmuxWindowTarget` type for tmux pane targeting; `_send_enter_and_validate` now takes `tmux_target: TmuxWindowTarget` instead of a bare string.

### Fixed

- Fixed: `antigravity_background_tasks.sh` now uses the `=` exact-match prefix in its `tmux has-session` polling loop so it no longer leaks the transcript streamer and common-transcript converter for stopped agents when a sibling-prefix session is still alive.
