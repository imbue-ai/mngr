# Changelog - mngr_robinhood

A concise, human-friendly summary of changes for the `mngr_robinhood` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `mngr robinhood` can now surface an approximate, live view of the response as it is produced, sourced from the spawned agent's tmux-based `stream_buffer` (see `imbue-mngr-claude`). `--include-partial-messages` is now accepted (previously rejected) — with `--output-format stream-json` it emits claude-native `stream_event` / `content_block_delta` / `text_delta` events as the response streams, followed by the authoritative `assistant` message from the transcript (matching claude's native partial-message ordering). A new `--stream-plain-text` flag, with the default text output, streams response text to stdout incrementally and suppresses the trailing full-text dump so streamed content is not duplicated. The orchestrator reads `stream_buffer` over the host inside its existing end-of-turn poll loop, diffing the cumulative body against what it last emitted (prefix-extension → append delta; reset → new message) so deltas are pure appends; the `result` envelope and final `assistant` message remain the source of truth.

### Changed

- Changed: When either streaming flag is set, robinhood enables the streaming watcher on the spawned agent (`streaming_snapshot_interval_seconds = 0.25`) and defaults the model to sonnet (so fast mode is off and streaming is observable); a user-passed `--model` still takes precedence. Both flags are consumed by the wrapper and not forwarded to the spawned claude. `--include-partial-messages` requires `--output-format stream-json`, and `--stream-plain-text` requires the default text output; mismatches exit with code 2.

## [v0.1.0] - 2026-06-05

### Added

- Added: `mngr robinhood`, a new top-level mngr command that acts as a drop-in replacement for `claude -p`. Every claude flag is forwarded verbatim to a fresh, ephemeral mngr claude agent run in-place in the current directory; the response is harvested from the agent's transcript, and the agent is destroyed on exit. Supports `--input-format` (text / stream-json) and `--output-format` (text / json / stream-json); `--fallback-model`, `--max-budget-usd`, `--no-session-persistence`, `--include-hook-events`, `--include-partial-messages`, `-c`/`--continue`, `-r`/`--resume`, and `--session-id` are rejected in v1.

### Changed

- Changed: `robinhood` CLI now forces `--quiet` and `--headless` regardless of whether the user passed them, matching `claude -p`'s "stdout/stderr contains only the response" contract; mngr progress lines no longer leak into stderr.
- Changed: End-of-turn detection now polls the transcript directly for an `assistant_message` event with a terminal `stop_reason` (`end_turn` / `max_tokens` / `stop_sequence`), instead of relying on mngr's flickery lifecycle `WAITING` state. The lifecycle state is consulted only as a fallback to detect agent death.
- Changed: **Breaking** — Plugin renamed from `mngr_uncapped_claude` to `mngr_robinhood`. The PyPI package is now `imbue-mngr-robinhood`, the importable package is `imbue.mngr_robinhood`, and the CLI command is `mngr robinhood` (previously `mngr uncapped-claude`). Spawned agents now use the `robinhood-` name prefix and a `created-by=robinhood` label; error classes (`RobinhoodError`) and CLI option types are renamed accordingly. Behavior is otherwise unchanged.
- Changed: Added to the release tooling's publish graph (`scripts/utils.py`); will be offered for first publication to PyPI on the next release. Stale `imbue-mngr==0.2.8` / `imbue-mngr-claude==0.2.8` pins in `pyproject.toml` are realigned to the current `0.2.10`. No runtime change.
