# Changelog - mngr_uncapped_claude

A concise, human-friendly summary of changes for the `mngr_uncapped_claude` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `mngr uncapped-claude`, a new top-level mngr command that acts as a drop-in replacement for `claude -p`. Every claude flag is forwarded verbatim to a fresh, ephemeral mngr claude agent run in-place in the current directory; the response is harvested from the agent's transcript, and the agent is destroyed on exit. Supports `--input-format` (text / stream-json) and `--output-format` (text / json / stream-json); `--fallback-model`, `--max-budget-usd`, `--no-session-persistence`, `--include-hook-events`, `--include-partial-messages`, `-c`/`--continue`, `-r`/`--resume`, and `--session-id` are rejected in v1.

### Changed

- Changed: `uncapped-claude` now forces `--quiet` and `--headless` regardless of whether the user passed them, matching `claude -p`'s "stdout/stderr contains only the response" contract; mngr's progress lines no longer leak into stderr.
- Changed: Per-agent `MNGR_*` and `LLM_USER_PATH` env vars are deliberately not forwarded from the parent process so the spawned agent's readiness hook, background-tasks script, and common-transcript writer see the correct values.

### Fixed

- Fixed: Empty-`result` bug for short turns — end-of-turn detection now polls the transcript directly for an `assistant_message` with a terminal `stop_reason` (`end_turn` / `max_tokens` / `stop_sequence`) instead of relying on mngr's lifecycle `WAITING` state. Lifecycle state is consulted only as a fallback for agent death; a 10-minute no-progress safety timeout guards against `stream_transcript.sh` dying.
