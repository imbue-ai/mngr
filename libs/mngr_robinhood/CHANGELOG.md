# Changelog - mngr_robinhood

A concise, human-friendly summary of changes for the `mngr_robinhood` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.1.6] - 2026-06-18

### Changed

- Changed: Streaming consumers (`_StreamBufferConsumer` and `StreamEventSynthesizer`) now delegate snapshot-diff bookkeeping to the agent's shared `LiveOutputReader` (`agent.make_live_output_reader()`/`agent.get_live_output_path()`) instead of each tracking `emitted_body` / `last_content` themselves. The `stream_buffer` module (the `compute_stream_delta` diff helpers) moved to `mngr_claude`, where the snapshot format lives; this package imports them from there. No user-visible behavior change.

## [v0.1.5] - 2026-06-16

### Changed

- Changed: Best-effort agent teardown (`stop_agent` / `destroy_agent`) and the SDK restart-with-resume flow now also swallow the `CleanupFailedGroup` that `Host.stop_agents` / `Host.destroy_agent` raise when cleanup leaves a resource behind, matching the existing intent of logging and continuing rather than letting a teardown failure abort the run (or, for restart, abort the relaunch).

## [v0.1.4] - 2026-06-16

## [v0.1.3] - 2026-06-15

### Changed

- Changed: robinhood's stream-json producers (CLI token stream and the Agent-SDK synthesizer) now build their events through the shared `imbue.mngr_claude.stream_json` typed boundary, and the `assistant` summary's inner message is constructed through `anthropic.types.Message`. The CLI token stream stays byte-identical to the real `claude` binary; the framing events (`message_start` ... `message_stop`) and the `assistant` summary now carry the `anthropic` Python SDK's optional null-valued fields, so they are shape-compatible but not byte-identical (consumers validate leniently).

## [v0.1.2] - 2026-06-13

## [v0.1.1] - 2026-06-08

### Added

- Added: `mngr robinhood` can now surface an approximate, live view of the response as it is produced, sourced from the spawned agent's tmux-based `stream_buffer` (see `imbue-mngr-claude`). `--include-partial-messages` is now accepted (previously rejected) — with `--output-format stream-json` it emits claude-native streaming events as the response streams, followed by the authoritative `assistant` message from the transcript. A new `--stream-plain-text` flag, with the default text output, streams response text to stdout incrementally and suppresses the trailing full-text dump so streamed content is not duplicated. The `result` envelope and final `assistant` message remain the source of truth.
- Added: mngr-backed Claude Agent SDK at `imbue.mngr_robinhood.agent_sdk`, importable as a drop-in replacement for `claude_agent_sdk` (`from imbue.mngr_robinhood.agent_sdk import query, ClaudeAgentOptions, ClaudeSDKClient`). Re-exports every SDK type verbatim and re-implements the behavioral entry points on top of mngr: each session is a `robinhood-`-prefixed mngr claude agent driven through the in-process mngr API and read back from its native transcript. Supports `query()` (string and streaming-input prompts), the full `ClaudeSDKClient` lifecycle, the observable `ClaudeAgentOptions` subset, built-in tool use end-to-end, directory-keyed session functions, and resume / continue. `can_use_tool` and `hooks` callbacks fire in-process via a local HTTP bridge; `interrupt()` ends an in-flight turn; `set_model` / `set_permission_mode` restart on the resumed session; `ResultMessage.total_cost_usd` is computed from per-turn token usage and a per-model price table; `include_partial_messages` yields `StreamEvent`s by watching the agent's tmux pane.
- Added: `mngr robinhood` tmux window-sizing flags `--tmux-width`, `--tmux-height`, and `--tmux-window-size` (`manual|latest|largest|smallest`). The spawned agent's tmux window now defaults to a large, pinned size (`2048` columns x `256` rows, `manual`) so the live-streamed response is no longer chopped into hard line wraps at a narrow pane width. All three flags are consumed by the wrapper (not forwarded to claude); invalid values exit with code 2.
- Added: Auto-discovered as a publishable package by the release tooling — the previously-documented `uv tool install imbue-mngr-robinhood` instruction will stop 404-ing; will be offered for first publication to PyPI on the next release.

### Changed

- Changed: When either streaming flag is set, robinhood enables the streaming watcher on the spawned agent (`streaming_snapshot_interval_seconds = 0.25`) and defaults the model to sonnet (so fast mode is off and streaming is observable); a user-passed `--model` still takes precedence. Both flags are consumed by the wrapper and not forwarded to the spawned claude. `--include-partial-messages` requires `--output-format stream-json`, and `--stream-plain-text` requires the default text output; mismatches exit with code 2.
- Changed: Shared agent-runtime helpers extracted from `orchestrator.py` into `agent_runtime.py` (used by both the robinhood CLI and the SDK); env forwarding hardened to drop shell-unsafe values that could corrupt the agent's env file.

### Fixed

- Fixed: `mngr robinhood` (and the Agent SDK) no longer forward the caller's tmux/terminal session variables (`TMUX`, `TMUX_PANE`, `KITTY_*`) into the spawned agent's environment. When `mngr robinhood` was run from inside a tmux/mngr session, forwarding `TMUX` pointed the new headless agent's tmux machinery (readiness detection, transcript capture) at the *parent's* pane, so the agent never signalled readiness and the command hung.
- Fixed: Duplicated paragraphs in `mngr robinhood`'s live streaming output (`--stream-plain-text` and `--include-partial-messages`). When Claude's TUI reflowed already-rendered text as later text streamed in — most visibly collapsing a blank line around a markdown horizontal rule (`---`) — the delta computation re-emitted everything past the divergence point. `compute_stream_delta`'s divergence branch now recognizes already-emitted content across whitespace reflow (treating whitespace runs as equivalent and absorbing collapsed/added blank lines), so only genuinely new content is emitted.

## [v0.1.0] - 2026-06-05

### Added

- Added: `mngr robinhood`, a new top-level mngr command that acts as a drop-in replacement for `claude -p`. Every claude flag is forwarded verbatim to a fresh, ephemeral mngr claude agent run in-place in the current directory; the response is harvested from the agent's transcript, and the agent is destroyed on exit. Supports `--input-format` (text / stream-json) and `--output-format` (text / json / stream-json); `--fallback-model`, `--max-budget-usd`, `--no-session-persistence`, `--include-hook-events`, `--include-partial-messages`, `-c`/`--continue`, `-r`/`--resume`, and `--session-id` are rejected in v1.

### Changed

- Changed: `robinhood` CLI now forces `--quiet` and `--headless` regardless of whether the user passed them, matching `claude -p`'s "stdout/stderr contains only the response" contract; mngr progress lines no longer leak into stderr.
- Changed: End-of-turn detection now polls the transcript directly for an `assistant_message` event with a terminal `stop_reason` (`end_turn` / `max_tokens` / `stop_sequence`), instead of relying on mngr's flickery lifecycle `WAITING` state. The lifecycle state is consulted only as a fallback to detect agent death.
- Changed: **Breaking** — Plugin renamed from `mngr_uncapped_claude` to `mngr_robinhood`. The PyPI package is now `imbue-mngr-robinhood`, the importable package is `imbue.mngr_robinhood`, and the CLI command is `mngr robinhood` (previously `mngr uncapped-claude`). Spawned agents now use the `robinhood-` name prefix and a `created-by=robinhood` label; error classes (`RobinhoodError`) and CLI option types are renamed accordingly. Behavior is otherwise unchanged.
- Changed: Added to the release tooling's publish graph (`scripts/utils.py`); will be offered for first publication to PyPI on the next release. Stale `imbue-mngr==0.2.8` / `imbue-mngr-claude==0.2.8` pins in `pyproject.toml` are realigned to the current `0.2.10`. No runtime change.
