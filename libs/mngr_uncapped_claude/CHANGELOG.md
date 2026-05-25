# Changelog - mngr_uncapped_claude

A concise, human-friendly summary of changes for the `mngr_uncapped_claude` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `mngr uncapped-claude`, a new top-level mngr command that acts as a drop-in replacement for `claude -p`. Every claude flag is forwarded verbatim to a fresh, ephemeral mngr claude agent run in-place in the current directory; the response is harvested from the agent's transcript, and the agent is destroyed on exit. Supports `--input-format` (text / stream-json) and `--output-format` (text / json / stream-json); `--fallback-model`, `--max-budget-usd`, `--no-session-persistence`, `--include-hook-events`, `--include-partial-messages`, `-c`/`--continue`, `-r`/`--resume`, and `--session-id` are rejected in v1.

### Changed

- Changed: `uncapped-claude` CLI now forces `--quiet` and `--headless` regardless of whether the user passed them, matching `claude -p`'s "stdout/stderr contains only the response" contract; mngr progress lines no longer leak into stderr.
- Changed: End-of-turn detection now polls the transcript directly for an `assistant_message` event with a terminal `stop_reason` (`end_turn` / `max_tokens` / `stop_sequence`), instead of relying on mngr's flickery lifecycle `WAITING` state. The lifecycle state is consulted only as a fallback to detect agent death.

### Fixed

- Fixed: `UNABRIDGED_CHANGELOG.md` intro now references the correct entries directory.
