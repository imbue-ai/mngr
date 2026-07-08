# Plan: Partial-message streaming for the mngr-backed Agent SDK

## Overview

- Bring the approximate Claude response streaming that landed on `main` (PR #2011: a tmux-pane
  watcher that writes a `stream_buffer`) into this branch, then make the mngr-backed Agent SDK emit
  `StreamEvent`s when `include_partial_messages=True` — so the two streaming tests currently gated to
  `real_sdk` pass for the `mngr_sdk` target too.
- The mngr transport never sees claude's real token-level deltas; it only has the watcher's
  approximate, pane-reconstructed text. So the SDK *synthesizes* a full claude-native streaming-event
  sequence around those text deltas: the event *shapes* conform exactly to the real SDK, only the
  text content is approximate (the same approximation already shipped for the CLI).
- The CLI's `stream_buffer` diffing logic is factored into a shared module so the SDK driver and the
  CLI orchestrator share one implementation rather than duplicating it.
- Streaming stays fully off by default: the watcher is only enabled (and `StreamEvent`s only emitted)
  when the caller passes `include_partial_messages=True`. Tool calls are unaffected — streaming is the
  assistant-text channel only.
- The merge brings `origin/main` (80 commits ahead of the PR base) into the branch; the interim PR
  diff growing is accepted.

## Expected behavior

- `query(...)` / `ClaudeSDKClient` with `options.include_partial_messages=True` against the mngr
  target yields `StreamEvent` objects interleaved during the turn, then the authoritative
  `AssistantMessage` and `ResultMessage` from the transcript.
- Each `StreamEvent` matches the documented contract: non-empty `uuid` (str), non-empty `session_id`
  (str), and an `event` dict whose shape mirrors claude's native stream (`message_start`,
  `content_block_start`, `content_block_delta` with a `text_delta`, `content_block_stop`,
  `message_delta`, `message_stop`).
- The synthesized envelopes carry zeroed/empty `usage`; the authoritative token usage and
  `total_cost_usd` remain transcript-derived in the final `ResultMessage` (unchanged).
- With `include_partial_messages` unset (the default), no `StreamEvent`s appear and behavior is
  byte-for-byte unchanged — the watcher is never provisioned.
- On a normal turn completion the synthesized sequence is well-formed (it ends with
  `content_block_stop` → `message_delta` → `message_stop`). On `interrupt()` the sequence is left
  unterminated wherever the transport stopped (no fabricated close).
- The caller's requested model is honored (the SDK does not force sonnet); the live streaming tests
  use a long-enough prompt that at least one partial is reliably observed at the watcher's interval.
- Tool-use turns stream only their assistant-text portions; `tool_use` blocks still arrive as
  authoritative transcript content, not as `StreamEvent`s.
- The CLI streaming behavior (`mngr robinhood --include-partial-messages` / `--stream-plain-text`)
  is unchanged by the shared-module refactor.

## Changes

### Merge

- Merge `origin/main` into `mngr/finish-agent-sdk`. Resolve conflicts: regenerate `uv.lock` via
  `uv lock`; for `orchestrator.py` / `orchestrator_test.py`, keep main's streaming additions and
  re-apply this branch's changes on top. Run the full suite afterward to confirm the merge is clean.

### Shared stream-buffer module (`libs/mngr_robinhood`)

- Extract the pure `stream_buffer` parsing/diffing pieces from `orchestrator.py` (the body parser and
  `compute_stream_delta`, plus the id-line message-boundary handling) into a new
  `imbue/mngr_robinhood/stream_buffer.py`.
- Update `orchestrator.py` to import them from the new module; behavior of the CLI path is unchanged.

### SDK streaming (`libs/mngr_robinhood/imbue/mngr_robinhood/_agent_sdk`)

- When the session's options have `include_partial_messages=True`, enable the watcher on the SDK's
  claude agent by setting `streaming_snapshot_interval_seconds > 0` (cleanest available mechanism at
  implementation time), reusing main's provisioning + python3 preflight. Do not forward
  `--include-partial-messages` to the interactive claude CLI.
- In the driver's drain loop, when streaming is enabled, read the agent's `stream_buffer` over the
  host each tick, diff it with the shared module, and push synthesized `StreamEvent`s to the same
  sink the messages use — interleaved before the authoritative final messages.
- Add a synthesis helper that turns the ordered text deltas (and message-boundary transitions) into
  the full claude-native event sequence wrapped as `StreamEvent`s: open framing
  (`message_start` + `content_block_start`) on the first delta of a message, `content_block_delta`
  per appended chunk, and close framing (`content_block_stop` → `message_delta` → `message_stop`) on
  normal message/turn completion; leave unterminated on interrupt. Envelopes use synthesized ids,
  the resolved model, and zeroed/empty `usage`; `session_id` comes from the session.
- Streaming state is reset per turn so cross-turn messages stream cleanly.

### Tests (`libs/mngr_robinhood`)

- Un-gate `test_include_partial_messages_yields_stream_events` and
  `test_stream_event_has_documented_fields` (remove `requires_native_sdk`) so they run for both
  targets; lengthen their prompts so partials reliably appear at the watcher interval.
  `test_without_partial_messages_no_stream_events` already runs for both and must stay green.
- Add offline unit tests for the synthesis helper: happy-path (buffer deltas → ordered
  `StreamEvent` sequence with correct framing and event-dict shapes) plus edge cases — streaming-off
  default (no events), empty/idle buffer, message-boundary transition (id line advances → close then
  reopen), no-overlap reset, and final-flush-before-authoritative-message ordering.
- Keep the shared module's existing CLI unit tests passing; add coverage for any newly extracted
  surface.

### Docs / changelog

- Update the `mngr_robinhood` README: the Agent SDK now supports `include_partial_messages` →
  `StreamEvent` (approximate, pane-reconstructed), removing it from the real-SDK-only limitations
  list (partial-message streaming is no longer a documented gap; the hermeticity note remains).
- Add a `libs/mngr_robinhood/changelog/<branch>.md` entry describing the SDK streaming support.
