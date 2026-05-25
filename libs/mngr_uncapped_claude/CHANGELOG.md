# Changelog - mngr_uncapped_claude

A concise, human-friendly summary of changes for the `mngr_uncapped_claude` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `mngr uncapped-claude`, a new top-level mngr command that acts as a drop-in replacement for `claude -p`. Every claude flag is forwarded verbatim to a fresh, ephemeral mngr claude agent run in-place in the current directory; the response is harvested from the agent's transcript, and the agent is destroyed on exit. Supports `--input-format` (text / stream-json) and `--output-format` (text / json / stream-json); `--fallback-model`, `--max-budget-usd`, `--no-session-persistence`, `--include-hook-events`, `--include-partial-messages`, `-c`/`--continue`, `-r`/`--resume`, and `--session-id` are rejected in v1.

### Changed

- Changed: `uncapped-claude` now forces `--quiet` and `--headless` regardless of whether the user passed them, matching `claude -p`'s contract that stdout/stderr contains only the response.

### Fixed

- Fixed: Empty-`result` bug on short turns — orchestrator now polls the transcript directly for an `assistant_message` event whose `stop_reason` is terminal (`end_turn` / `max_tokens` / `stop_sequence`), rather than keying on mngr's lifecycle `WAITING` state which flickered during tool-permission auto-approval and lagged the transcript writer.
