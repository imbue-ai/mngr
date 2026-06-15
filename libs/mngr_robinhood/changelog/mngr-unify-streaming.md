# Stream-json producers use the shared typed envelope

Both robinhood stream-json producers now build their events through the shared
`imbue.mngr_claude.stream_json` typed boundary instead of hand-rolled dicts:

- The CLI token stream (`output_modes.py`'s `emit_partial_text`) emits its
  `content_block_delta` / `text_delta` via the shared builder. The wire output is byte-identical.
- The Agent-SDK synthesizer (`_agent_sdk/stream_events.py`) builds its full framing sequence
  (`message_start` ... `message_stop`) via the shared builders, and the `assistant` summary's inner
  message is now constructed through `anthropic.types.Message`.

Because the framing events and the assistant message are now dumped from the `anthropic` Python
models, they carry that SDK's optional, null-valued fields (e.g. `content_block.citations: null`,
`tool_use.caller: null`, the `usage` cache/detail nulls). The token stream itself is unchanged and
stays byte-identical to the real `claude` binary. The other events are shape-compatible but not
byte-identical: the Python and TypeScript SDKs carry different optional fields, so the real binary
omits `citations` and emits a populated `caller`, among other differences. These departures are
cosmetic (consumers validate leniently) and documented in the `imbue.mngr_claude.stream_json`
module docstring.
