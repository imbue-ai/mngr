Adapted the Claude agents to the unified live-output contract.

`ClaudeAgent` (TUI) now inherits `SupportsLiveOutputMixin` directly (instead of the removed `HasStreamingSnapshotMixin`), exposes its streaming snapshot via `get_live_output_path()`, and supplies a `SnapshotDeltaReader` from `make_live_output_reader()`. The stream_buffer snapshot parsing/diffing (`compute_stream_delta` and friends, previously in `mngr_robinhood`) moves into the new `imbue.mngr_claude.stream_buffer` module alongside that reader, since it is the Claude watcher's format.

`HeadlessClaude` keeps streaming `claude --print` stream-json output via `stream_output()`, but the tail loop is now the shared one in mngr; the agent only supplies a `StreamJsonReader` plus its startup-grace "finished" check and stderr-augmented error reporting. No user-visible behavior change.
