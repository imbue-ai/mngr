Unified the two live-output surfaces (a TUI agent's streaming-snapshot buffer and a headless agent's captured stdout) onto one shared shape, so `SupportsLiveOutputMixin` is no longer a bare marker.

It now declares `get_live_output_path()` (the host file the agent publishes live output to) and `make_live_output_reader()` (a `LiveOutputReader` that turns successive reads of that file into text deltas), and carries the shared `stream_live_output()` poll-read-extract tail loop that both surfaces build on. The former `HasStreamingSnapshotMixin` is removed -- a TUI agent now inherits `SupportsLiveOutputMixin` directly and supplies a snapshot-diff reader, while a headless agent supplies a raw-text or stream-json reader. The new `imbue.mngr.interfaces.live_output` module holds the `LiveOutputReader` contract and the `RawTextReader` implementation.

No user-visible behavior change: `mngr ask` / `mngr create --stream` and the robinhood streaming paths emit the same output as before.
