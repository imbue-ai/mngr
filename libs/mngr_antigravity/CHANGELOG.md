# Changelog - mngr_antigravity

A concise, human-friendly summary of changes for the `mngr_antigravity` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `gemini` agent type plugin (`imbue-mngr-gemini`) wiring Google's Gemini CLI into mngr.
- Added: `gemini_config.py` foundation (read/write helpers for `~/.gemini/settings.json`, env-var interpolation, hook builders) plus a `SessionStart` readiness sentinel wired into `GeminiAgent.wait_for_ready_signal`.
- Added: Opt-in `auto_allow_permissions` flag on `GeminiAgentConfig` that installs a `BeforeTool` wildcard hook auto-approving every tool call.
- Added: Gemini agents now emit a common transcript readable by `mngr transcript`; raw gemini session JSONL is captured into `logs/gemini_transcript/events.jsonl`. Opt out with `emit_common_transcript = false`.
- Added: Renamed `mngr_gemini` to `mngr_antigravity` to follow Google's CLI rename from Gemini CLI to Antigravity CLI (`agy`); the new CLI is architecturally closer to Claude Code (hook event names match Claude's; `--dangerously-skip-permissions` is the documented auto-allow flag). Optional `auto_dismiss_dialogs` opt-in (default `False`) handles the first-launch "Do you trust this folder?" dialog by appending the agent's `work_dir` to `~/.gemini/antigravity-cli/settings.json::trustedWorkspaces`; `emit_common_transcript=True` (default) wires `agy`'s JSONL transcripts into mngr's common-transcript schema scoped per-agent. The plugin was never released, so this is a destructive rename with no shim.

### Changed

- Changed: `GeminiAgent` now implements the new `HasTranscriptMixin` / `HasCommonTranscriptMixin` mixins.
- Changed: `AntigravityAgentConfig.merge_with` follows mngr's new assign-by-default semantics — an override's `cli_args` replaces the base's; use `cli_args__extend = [...]` for additive layering.
- Changed: Plugin now uses the structured `TmuxWindowTarget` type for tmux pane targeting; `_send_enter_and_validate` takes `tmux_target: TmuxWindowTarget` instead of a bare string.

### Fixed

- Fixed: `antigravity_background_tasks.sh` now uses the `=` exact-match prefix in its `tmux has-session` polling loop, so it no longer leaks the transcript streamer and common-transcript converter when a sibling session shares a name prefix.
