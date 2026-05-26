# Changelog - mngr_claude_usage

A concise, human-friendly summary of changes for the `mngr_claude_usage` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Changed

- Changed: Statusline writer captures `rate_limits` + per-render `session_id` + `cost.*` from Claude Code's statusline JSON into `events/claude/usage/events.jsonl` (renamed from `events/claude/rate_limits/`); no longer skips emission when only `cost` is present, so cost tracking now works for direct `ANTHROPIC_API_KEY` users.
- Changed: Statusline shim and writer scripts now live at host-stable paths (`<host_dir>/commands/claude_statusline.sh` / `claude_usage_writer.sh`), so a work_dir's `settings.local.json` `statusLine.command` stays valid across agent lifecycles; the shim exits 0 silently when `MNGR_AGENT_STATE_DIR` is unset.
- Changed: Adopted the new per-project changelog layout.

### Fixed

- Fixed: Infinite-recursion bug when running successive claude agents in the same `work_dir` (as `mngr uncapped-claude` always does).
- Fixed: `UNABRIDGED_CHANGELOG.md` intro now references the correct entries directory (`<project_dir>/changelog/`).

## [v0.2.8] - 2026-05-13

### Added

- Added: New writer plugin (`mngr_claude_usage`) — a per-agent statusline shim that captures the JSON snapshot Claude Code feeds to its statusline command on every render, into `events/claude/rate_limits/events.jsonl`. The shim composes with any pre-existing user `statusLine.command`.
