# Changelog - mngr_antigravity

A concise, human-friendly summary of changes for the `mngr_antigravity` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `gemini` agent type plugin (`imbue-mngr-gemini`) wiring Google's Gemini CLI into mngr.
- Added: `gemini_config.py` foundation (read/write helpers for `~/.gemini/settings.json`, env-var interpolation, hook builders) plus a `SessionStart` readiness sentinel wired into `GeminiAgent.wait_for_ready_signal`.
- Added: Opt-in `auto_allow_permissions` flag on `GeminiAgentConfig` that installs a `BeforeTool` wildcard hook auto-approving every tool call.
- Added: Gemini agents now emit a common transcript readable by `mngr transcript`; raw gemini session JSONL is captured into `logs/gemini_transcript/events.jsonl`. Opt out with `emit_common_transcript = false`.

### Changed

- Changed: `GeminiAgent` now implements the new `HasTranscriptMixin` / `HasCommonTranscriptMixin` mixins.
