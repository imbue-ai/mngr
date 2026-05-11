# Plan: Turn extraction in `mngr transcript`

## Overview

- Bring `extract_turn` functionality into the existing `mngr transcript` CLI command so agents can slice their own transcripts by conversational turn without a separate script.
- Define a "turn" by `type: "user_message"` events in the common_transcript format. Stop-hook injections are already reclassified upstream as `tool_result` with `tool_name == "meta"`, so no extra filtering is required.
- Add four new flags — `--turn N`, `--last-completed-turn`, `--count-turns`, `--list-turns` — that compose with `--role` and `--format` but are mutually exclusive with `--head` / `--tail` and with each other.
- Make the `target` positional argument optional, falling back to the `MNGR_AGENT_ID` environment variable that mngr already exports into every agent's shell. This is the "auto-discovery" piece — an agent can run `mngr transcript --last-completed-turn --format jsonl` from a hook with no other context.
- Out of scope: porting forever-claude-template's `extract_turn.py` callers, removing the script, and porting marker-based slicing (`--start-marker`/`--end-marker`).

## Expected behavior

- Running `mngr transcript <target>` without any new flag continues to print the full transcript (no change to existing behavior).
- Running `mngr transcript` (no target) inside an agent's shell resolves the target from `$MNGR_AGENT_ID`.
- Running `mngr transcript` with neither a target nor `$MNGR_AGENT_ID` set exits 2 with: "No target given and `MNGR_AGENT_ID` is not set. Pass an agent name/ID, or run inside an agent context."
- `--turn N` (1-indexed from start) emits the events from `user_message` #N (inclusive) through the event immediately preceding `user_message` #N+1. If #N+1 doesn't exist, the slice runs to end-of-transcript.
- `--turn -1` emits the slice starting at the most recent `user_message` and running to end-of-transcript (the in-progress turn). `--turn -2` emits the previous completed turn. Negative indices are Python-style.
- `--turn 0` and out-of-range indices (`|N| > turn_count`) exit 2 with a clear "no such turn" error that reports the actual turn count.
- `--last-completed-turn` is sugar for `--turn -2` — the most recent turn that has a `user_message` after it. It is the dominant use case from extract_turn skill callers (matches `extract_turn --nth 1`). If there is no completed turn yet (fewer than 2 `user_message` events), it exits 2 with "no completed turn yet (only N user message(s) in transcript)".
- `--count-turns` prints just the integer count of `user_message` events to stdout and exits 0. Ignores `--role` and `--format`.
- `--list-turns` prints a summary of each turn boundary, respecting `--format`:
  - `human` (default): a table with columns `#`, `timestamp`, and `content_preview` (first ~80 chars of the user_message content with newlines collapsed).
  - `json`: a pretty-printed JSON array of `{turn, timestamp, event_id, content_preview}` objects.
  - `jsonl`: one such object per line.
- `--turn` and `--last-completed-turn` compose with `--role`: the slice is computed first, then role-filtered before output.
- `--turn`, `--last-completed-turn`, `--count-turns`, `--list-turns` are mutually exclusive with each other and with `--head` / `--tail`. Combining any two raises `UserInputError` at CLI parsing time with a message naming both offending flags.
- An empty transcript or a transcript with zero `user_message` events: `--count-turns` prints `0`; `--list-turns` prints an empty table/array/(empty output for jsonl); `--turn`/`--last-completed-turn` exit 2 with a clear error.

## Changes

- **CLI options (`libs/mngr/imbue/mngr/cli/transcript.py`)**
  - Make the `target` positional argument optional.
  - Add four new options to `TranscriptCliOptions`: `turn: int | None`, `last_completed_turn: bool`, `count_turns: bool`, `list_turns: bool`.
  - Add a validation step that rejects (a) more than one of the four turn flags set, (b) any turn flag combined with `--head` or `--tail`.
- **Auto-discovery**
  - When `target` is empty/None, read `MNGR_AGENT_ID` from the environment and use it as the target identifier. If unset, raise `UserInputError` with the message above.
- **Turn computation**
  - Add helpers (kept inside `transcript.py` unless they need to be reused) to:
    - Compute the list of `(turn_index, event)` pairs for all `user_message` events in the parsed events.
    - Translate a `--turn N` value (positive or negative) into the inclusive slice `[events_after_turn_start, events_before_next_turn_start)`.
    - Translate `--last-completed-turn` to `--turn -2`.
  - Apply `--role` filter to the slice after it is computed.
- **Output paths**
  - `--turn` / `--last-completed-turn`: feed the resulting event list into the existing `_emit_transcript()` so all three formats keep working unchanged.
  - `--count-turns`: print the integer and skip `_emit_transcript()`.
  - `--list-turns`: build summary records and emit per `--format` (new small emission helper; reuse human-table style from existing formatter as much as is practical).
- **Tests (`libs/mngr/imbue/mngr/cli/transcript_test.py`)**
  - Turn boundary detection over a fixture with multiple `user_message`, `assistant_message`, `tool_result` events including a `tool_name == "meta"` stop-hook event (must not affect counts/boundaries).
  - `--turn` positive, negative, and out-of-range indices.
  - `--last-completed-turn` happy path and "no completed turn yet" error.
  - `--count-turns` on populated, empty, and meta-only transcripts.
  - `--list-turns` for all three formats.
  - Auto-discovery via `MNGR_AGENT_ID` env var, and the helpful error when both unset.
  - Mutual exclusivity errors for every relevant flag pairing.
  - `--role` composition with `--turn` (turn computed first, then filtered).
- **Changelog**
  - Add `changelog/gabriel-extract-turn.md` describing the new flags and auto-discovery in 2–4 user-facing bullets.
