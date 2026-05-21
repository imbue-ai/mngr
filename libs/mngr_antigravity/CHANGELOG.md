# Changelog - mngr_antigravity

A concise, human-friendly summary of changes for the `mngr_antigravity` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `antigravity` agent type plugin (`imbue-mngr-antigravity`) wiring Google's Antigravity CLI (`agy`) into mngr.
- Added: `pre_trust_workspace` flag (default `True`) on `AntigravityAgentConfig` that appends the agent's `work_dir` to `~/.gemini/antigravity-cli/settings.json::trustedWorkspaces` during provisioning to suppress agy's first-launch trust dialog.
- Added: Opt-in `auto_allow_permissions` flag on `AntigravityAgentConfig` that launches agy with `--dangerously-skip-permissions` so every tool call is auto-approved.
- Added: Antigravity agents now emit a common transcript readable by `mngr transcript`; raw agy session JSONL is captured into `logs/antigravity_transcript/events.jsonl`. Opt out with `emit_common_transcript = false`.

### Changed

- Changed: Renamed from `mngr_gemini` to `mngr_antigravity` to follow Google's 2026-05-19 deprecation of Gemini CLI in favor of Antigravity CLI. The legacy `gemini` agent type and `imbue-mngr-gemini` package no longer exist.
