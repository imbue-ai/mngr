# Stream-json producers use the shared typed envelope

Both robinhood stream-json producers now build their events through the shared
`imbue.mngr_claude.stream_json` typed boundary instead of hand-rolled dicts:

- The CLI token stream (`output_modes.py`'s `emit_partial_text`) emits its
  `content_block_delta` / `text_delta` via the shared builder. The wire output is byte-identical.
- The Agent-SDK synthesizer (`_agent_sdk/stream_events.py`) builds its full framing sequence
  (`message_start` ... `message_stop`) via the shared builders, and the `assistant` summary's inner
  message is now constructed through `anthropic.types.Message`.

Because the framing events and the assistant message are now dumped from anthropic's own models,
they additionally carry the API's optional, semantically-null metadata fields (e.g.
`content_block.citations: null`, the `usage` cache/detail nulls). These are the API's native shapes
and tolerated by permissive consumers; the token stream itself is unchanged.
