# Shared, typed Claude stream-json envelope

Added `imbue.mngr_claude.stream_json`, a single typed boundary for the Claude partial-message
stream-json envelope (`message_start` / `content_block_start` / `content_block_delta` /
`text_delta` / `content_block_stop` / `message_delta` / `message_stop`, plus the `assistant`
summary's inner message). It is defined against the `anthropic` SDK's discriminated
`RawMessageStreamEvent` union and `anthropic.types.Message`, so the protocol vocabulary is owned
upstream instead of hand-rolled as bare string literals. The consume side validates into the union
and dispatches with an exhaustive `assert_never` match, so a future `anthropic` release that adds an
event variant fails the type check and names exactly what we must handle.

- `mngr ask`'s headless reader (`headless_claude_agent.py`) now parses partial-message events and
  the `assistant` summary through this boundary. Behavior is unchanged for well-formed `claude`
  output; an event variant or content-block type newer than the installed `anthropic` package
  degrades gracefully (it is skipped / falls back to a lenient text scan rather than dropping the
  response).
- Added `anthropic` as a dependency (kept unpinned; imported for its typed models only -- mngr
  still drives the `claude` CLI and makes no API calls).
