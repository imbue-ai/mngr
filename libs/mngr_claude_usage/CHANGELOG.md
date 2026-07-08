# Changelog - mngr_claude_usage

A concise, human-friendly summary of changes for the `mngr_claude_usage` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.1.6] - 2026-06-18

## [v0.1.5] - 2026-06-16

### Changed

- Changed: Claude usage events now aggregate through the new `aggregate_usage_source` reader hookimpl rather than being special-cased inside `mngr_usage` itself. This plugin claims the `claude` source and aggregates with the process-cumulative strategy (Claude reports cost cumulatively across a process, so a `/clear` that rotates `session_id` must not double-count). Output is identical.

## [v0.1.4] - 2026-06-16

## [v0.1.3] - 2026-06-15

## [v0.1.2] - 2026-06-13

## [v0.1.1] - 2026-06-08

### Added

- Added: Auto-discovered as a publishable package by the release tooling (the writer half of the usage split; pairs with `mngr_usage`); will be offered for first publication to PyPI on the next release.

## [v0.1.0] - 2026-06-05

### Changed

- Changed: Statusline writer captures `rate_limits` + per-render `session_id` + `cost.*` from Claude Code's statusline JSON into `events/claude/usage/events.jsonl` (renamed from `events/claude/rate_limits/`); no longer skips emission when only `cost` is present, so cost tracking now works for direct `ANTHROPIC_API_KEY` users.
- Changed: Statusline shim and writer scripts now live at host-stable paths (`<host_dir>/commands/claude_statusline.sh` / `claude_usage_writer.sh`), so a work_dir's `settings.local.json` `statusLine.command` stays valid across agent lifecycles; the shim exits 0 silently when `MNGR_AGENT_STATE_DIR` is unset.
- Changed: Added to the release tooling's publish graph (`scripts/utils.py`); will be offered for first publication to PyPI on the next release. Stale `imbue-mngr==0.2.6` / `imbue-mngr-claude==0.2.6` pins in `pyproject.toml` are realigned to the current `0.2.10`. No runtime change.

### Fixed

- Fixed: Infinite-recursion bug when running successive claude agents in the same `work_dir` (as `mngr robinhood` always does).

## [v0.2.8] - 2026-05-13

### Added

- Added: New writer plugin (`mngr_claude_usage`) — a per-agent statusline shim that captures the JSON snapshot Claude Code feeds to its statusline command on every render, into `events/claude/rate_limits/events.jsonl`. The shim composes with any pre-existing user `statusLine.command`.
