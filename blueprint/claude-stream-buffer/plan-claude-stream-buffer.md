# Plan: Approximate streaming of Claude responses via the mngr tmux session

## Overview

- Add basic, *approximate* streaming of Claude's assistant text by watching its tmux pane, rather than by hooking into Claude's API/transcript deltas.
- A new on-host watcher polls `tmux capture-pane` every N seconds, reverse-maps the terminal-rendered markdown back into source markdown, and writes the current in-progress assistant text to a `stream_buffer` file in the claude plugin data directory.
- The watcher is a self-contained Python script in `mngr_claude/resources/` (mirroring `sync_keychain_credentials.py`), supervised/restarted by the existing `claude_background_tasks.sh`, and gated by a new `streaming_snapshot_interval_seconds` config on the claude agent type (`<= 0` disables it entirely; default `0.0`).
- `stream_buffer` is stateful and strict-append within a message: each poll captures only the visible pane and stitches newly-revealed lines onto what was already accumulated, so fast-enough polling reconstructs the full message as it scrolls. Its first line carries the id of the last *complete* assistant message so consumers can tell a fresh in-progress message apart from one that just finished.
- `mngr_robinhood` both enables the watcher (forcing sonnet) and consumes `stream_buffer`, exposing it as claude-native `text_delta` partials (`stream-json` + `--include-partial-messages`) and as incremental plain text (new `--stream-plain-text` flag), making the feature easy to verify end-to-end.

## Expected behavior

- With `streaming_snapshot_interval_seconds > 0` on a claude agent, a background watcher runs while the tmux session is alive and continuously maintains `$MNGR_AGENT_STATE_DIR/plugin/claude/stream_buffer`.
- With `streaming_snapshot_interval_seconds <= 0` (the default), no watcher is provisioned or launched, and no `stream_buffer` is produced — existing behavior is unchanged.
- `stream_buffer` layout: line 1 is the `uuid` of the last complete assistant message (empty string if none yet); lines 2+ are the in-progress assistant text, reverse-mapped to markdown.
- Only assistant *text* blocks are streamed. A block is the content after a `●` whose marker glyph is uncolored (default terminal foreground); `●` markers that are colored (tool calls) are ignored. Reasoning/"thinking" blocks are not streamed.
- A block spans the `●` line plus following lines indented by two spaces; blank lines continue the block; the first non-empty line that is not two-space-indented (a footer) ends it and is excluded.
- The streamed markdown reconstructs bold/italic, headings, bullet/numbered lists, blockquotes, links (`[text](url)`), tables (box-drawing back to pipe syntax), and code blocks/inline code. Fidelity is best-effort and approximate.
- The stream is strict-append within a message: partial constructs (half-typed link, unclosed code fence) are written as-is and never auto-closed or rewritten; later polls only append.
- Tables are a deferred-resolution exception: when content *could* be a table but there is not yet enough rendered to be sure, the watcher withholds that ambiguous region from `stream_buffer` (rather than appending raw box-drawing that would later need reinterpreting). It appends the region only once it is definitely a table (emitted as pipe-syntax markdown) or definitely not (emitted as its literal text). If the stream ends while the region is still ambiguous, it is treated as not-a-table and appended as literal text. This preserves strict-append for consumers — ambiguous content is delayed, never rewritten.
- Each poll captures the visible pane and overlap-stitches it onto the existing buffer (longest suffix/prefix line match). If a poll finds no overlap (a chunk scrolled past unseen, e.g. stream much faster than the interval), it is treated as a new message and the body resets — accepting that earlier text may be dropped rather than rewritten.
- The buffer holds only the latest assistant-text block; it resets when a new, non-continuing `●` text block appears. Line 1 is refreshed to the current last-complete id on every write.
- When the turn ends (the `active` file disappears / Stop hook has fired), the watcher empties the body and refreshes line 1, leaving an idle buffer that is just the id line.
- On watcher startup the buffer is cleared/initialized so a resumed or restarted agent never serves stale content.
- `stream_buffer` is always written atomically (temp file + `mv`), so a concurrent reader never sees a torn write.
- `mngr robinhood`:
  - Forces sonnet (so fast mode is off and streaming is observable) and auto-sets `streaming_snapshot_interval_seconds` to `0.25` when streaming is requested.
  - `--output-format stream-json --include-partial-messages` (flag now accepted instead of rejected): emits claude-native `text_delta` partial events as the buffer grows, then still emits the final authoritative `assistant` message from the transcript.
  - `--output-format text --stream-plain-text` (new flag): streams the assistant text incrementally to stdout and suppresses the final duplicate full dump.
  - Without these flags, robinhood output is unchanged.
- If `streaming_snapshot_interval_seconds > 0` but the host lacks `python3`, provisioning/preflight fails fast with a clear error rather than silently skipping streaming.

## Changes

### `libs/mngr_claude`

- Add `streaming_snapshot_interval_seconds: float` (default `0.0`) to `ClaudeAgentConfig`, documented as "poll interval for tmux-based response streaming; `<= 0` disables it."
- Add a self-contained watcher script under `mngr_claude/resources/` (e.g. `stream_snapshot.py`):
  - Pure reverse-mapping functions (terminal-with-ANSI → markdown, block extraction, uncolored-`●` detection, overlap-stitch, deferred table resolution) at module top level so unit tests import them directly; runnable behavior behind `if __name__ == "__main__"`.
  - Holds an ambiguous potential-table region in internal state (not yet appended to `stream_buffer`) until it resolves to a markdown table or to literal text; resolves to literal text if the stream ends while still ambiguous.
  - Runs its own `tmux capture-pane -e -J` (ANSI codes preserved, soft-wraps rejoined).
  - Reads the last-complete-assistant-message `uuid` from `logs/claude_transcript/events.jsonl`.
  - Writes `stream_buffer` atomically; clears it on startup; empties body on idle.
  - Pidfile guard against duplicate instances; tolerates capture failures by skipping that poll.
- Provision the watcher script only when `streaming_snapshot_interval_seconds > 0` (gate at provision time, like `common_transcript.sh`), alongside the always-on scripts.
- Expose the interval to the host via a new env var (e.g. `MNGR_CLAUDE_STREAM_SNAPSHOT_INTERVAL`) set in `modify_env_vars`.
- Add a provision/preflight check that errors when the interval is `> 0` but `python3` is unavailable on the host.
- Update `claude_background_tasks.sh` to launch and restart the watcher when the script is present (passing the session name and interval).
- Document `streaming_snapshot_interval_seconds` in the `mngr_claude` README; add a changelog entry.

### `libs/mngr_robinhood`

- Accept `--include-partial-messages` (currently rejected) and add a new `--stream-plain-text` flag; parse both into the arg partition / output config.
- When streaming is requested, force sonnet and inject `streaming_snapshot_interval_seconds = 0.25` via the existing unattended-settings mechanism.
- In the orchestrator tick loop, read the agent's `stream_buffer` over the host (same mechanism as the transcript read), diff the cumulative body against what was last emitted (using the id line for message boundaries), and feed appended text to the writer.
- Extend `StreamingOutputWriter`:
  - `stream-json`: emit claude-native `text_delta` partial events for appended text; still emit the final authoritative `assistant` message.
  - `text` + `--stream-plain-text`: write appended text incrementally and suppress the final full dump.
- Document the two flags in the `mngr_robinhood` README; add a changelog entry.

### Tests

- Unit tests for the pure parser: real `tmux capture-pane -e -J` fixtures (raw terminal bytes) paired with expected markdown, covering each construct plus partial/transient states; tests for uncolored-`●` detection, block boundaries (blank lines, footers), overlap-stitch, no-overlap reset, and deferred table resolution (ambiguous → table, ambiguous → literal, and stream-ends-while-ambiguous → literal).
- A robinhood-with-sonnet end-to-end acceptance/release test exercising the streaming flags (a live API key in `.env` is available but robinhood should work without it).

### Non-goals (recorded for scope)

- No perfect markdown fidelity (approximate, best-effort).
- No streaming of reasoning/"thinking" blocks.
- No streaming for non-claude agent types.
- No new top-level `mngr` CLI command to read the buffer.
